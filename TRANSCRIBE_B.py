from __future__ import annotations

import argparse
import concurrent.futures
import gc
import importlib.metadata
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, TextIO


DEVICE = "cuda"
COMPUTE_TYPE = "float16"
SAMPLE_RATE = 16000
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_BATCH_SIZE = 8
DEFAULT_OPENING_AUDIT_SECONDS = 210.0
DEFAULT_MAX_REPAIR_WINDOWS = 1
OPENING_AUDIT_PADDING_SECONDS = 2.5
SECTION_AUDIT_PADDING_SECONDS = 2.0
SECTION_CHUNK_SECONDS = 20.0
SECTION_CHUNK_OVERLAP_SECONDS = 10.0
SECTION_EVENT_CLUSTER_SECONDS = 6.0
SECTION_CROSS_WINDOW_MIN_SUPPORT = 2
REPAIR_PADDING_SECONDS = 4.0
V15_PATCH_APPLIED = True
V16_PATCH_APPLIED = True
V17_PATCH_APPLIED = True
V18_PATCH_APPLIED = True
V19_PATCH_APPLIED = True
JST = timezone(timedelta(hours=9), name="JST")

RUNTIME_PACKAGES = (
    "faster-whisper",
    "ctranslate2",
    "huggingface-hub",
)

PRIMARY_MODEL_REPOSITORY = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
PRIMARY_MODEL_DIRECTORY_NAME = "faster-whisper-large-v3-turbo"
REPAIR_MODEL_REPOSITORY = "Systran/faster-whisper-large-v3"
REPAIR_MODEL_DIRECTORY_NAME = "faster-whisper-large-v3"

MODEL_ALLOW_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.json",
    "vocabulary.*",
)

MODEL_REQUIRED_FILES = (
    "config.json",
    "model.bin",
    "tokenizer.json",
)

TRANSCRIPTION_GLOSSARY = (
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish, lesson, dialogue, key sentence, example, "
    "pronunciation, Suppose, Imagine, ask for advice, 仮定法, 目的格, 不定詞, "
    "意味上の主語, 現在進行形, "
    "説明型オーバーラッピング, come out, not exactly, what if, for us"
)

STRICT_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Grammar and Vocabulary": (
        "Grammar and Vocabulary",
        "文法と語彙",
    ),
    "Essential Expressions": (
        "Essential Expressions",
        "エッセンシャル・エクスプレッションズ",
        "重要表現",
    ),
    "Practical Usage": (
        "Practical Usage",
        "プラクティカル・ユーセージ",
        "プラクティカルユーセージ",
    ),
    "Pronunciation Polish": (
        "Pronunciation Polish",
        "プロナンシエーション・ポリッシュ",
        "発音練習",
    ),
}


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutputPaths:
    run_id: str
    transcript: Path
    log: Path
    metrics: Path


@dataclass
class SegmentRecord:
    start: float
    end: float
    text: str
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float
    source: str


@dataclass
class OpeningAuditDecision:
    accepted: bool
    reason: str
    core_end: float
    baseline_text: str
    audit_text: str
    coverage_ratio: float
    token_recall: float
    baseline_score: float
    audit_score: float
    baseline_anchor_hits: int
    audit_anchor_hits: int
    baseline_section_hits: int
    audit_section_hits: int


@dataclass
class SectionAuditDecision:
    accepted: bool
    reason: str
    section_name: str
    core_start: float
    core_end: float
    baseline_text: str
    audit_text: str
    coverage_ratio: float
    token_recall: float
    baseline_score: float
    audit_score: float
    baseline_anchor_hits: int
    audit_anchor_hits: int
    baseline_section_hits: int
    audit_section_hits: int
    baseline_english_candidates: int
    audit_english_candidates: int


@dataclass
class SuspicionEvent:
    start: float
    end: float
    severity: float
    reason: str
    segment_indexes: tuple[int, ...]


@dataclass
class RepairWindow:
    index: int
    core_start: float
    core_end: float
    slice_start: float
    slice_end: float
    reasons: tuple[str, ...]
    segment_indexes: tuple[int, ...]


@dataclass
class RepairDecision:
    window_index: int
    accepted: bool
    reason: str
    baseline_text: str
    repair_text: str
    coverage_ratio: float
    token_recall: float
    baseline_score: float
    repair_score: float
    baseline_suspicious_events: int
    repair_suspicious_events: int
    core_start: float
    core_end: float
    slice_start: float
    slice_end: float
    reasons: tuple[str, ...]


class StepTimer:
    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.steps: list[dict[str, object]] = []
        self.current_name: str | None = None
        self.current_started_at: float | None = None

    def start(self, name: str) -> None:
        if self.current_name is not None:
            self.finish("PASS")
        self.current_name = name
        self.current_started_at = time.perf_counter()

    def finish(self, result: str = "PASS") -> None:
        if self.current_name is None or self.current_started_at is None:
            return
        elapsed = time.perf_counter() - self.current_started_at
        self.steps.append(
            {
                "name": self.current_name,
                "elapsed_seconds": round(elapsed, 6),
                "result": result,
            }
        )
        self.current_name = None
        self.current_started_at = None

    def fail_current(self) -> None:
        self.finish("FAILED")

    @property
    def total_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def seconds_for(self, name: str) -> float:
        for step in self.steps:
            if step["name"] == name:
                return float(step["elapsed_seconds"])
        return 0.0

    def print_summary(self, overall_result: str) -> None:
        if self.current_name is not None:
            self.finish("FAILED" if overall_result == "FAILED" else overall_result)
        print()
        print("PROCESS_TIME_SUMMARY")
        for index, step in enumerate(self.steps, start=1):
            print(
                f"STEP_{index:02d}={step['name']} | RESULT={step['result']} | "
                f"ELAPSED_SECONDS={float(step['elapsed_seconds']):.3f}"
            )
        print(f"TOTAL_ELAPSED_SECONDS={self.total_seconds:.3f}")
        print(f"OVERALL_RESULT={overall_result}")


class TeeStream:
    def __init__(self, terminal: TextIO, log_file: TextIO) -> None:
        self.terminal = terminal
        self.log_file = log_file

    @property
    def encoding(self) -> str:
        return getattr(self.terminal, "encoding", "utf-8") or "utf-8"

    def write(self, data: str) -> int:
        self.terminal.write(data)
        self.log_file.write(data)
        self.log_file.flush()
        return len(data)

    def flush(self) -> None:
        self.terminal.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return False


class TeeContext:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_handle: TextIO | None = None
        self.original_stdout: TextIO | None = None
        self.original_stderr: TextIO | None = None

    def __enter__(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("w", encoding="utf-8", newline="")
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        sys.stdout = TeeStream(self.original_stdout, self.log_handle)
        sys.stderr = TeeStream(self.original_stderr, self.log_handle)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.original_stdout is not None:
            sys.stdout = self.original_stdout
        if self.original_stderr is not None:
            sys.stderr = self.original_stderr
        if self.log_handle is not None:
            self.log_handle.flush()
            self.log_handle.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a faithful full transcript using a fast batched Turbo pass, "
            "one guarded opening audit, and rare large-v3 anomaly repair."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--opening-audit-seconds",
        type=float,
        default=DEFAULT_OPENING_AUDIT_SECONDS,
    )
    parser.add_argument(
        "--max-repair-windows",
        type=int,
        default=DEFAULT_MAX_REPAIR_WINDOWS,
    )
    parser.add_argument("--reference", default="")
    return parser.parse_args()


def resolve_path(script_directory: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (script_directory / path).resolve()


def make_output_paths(output_directory: Path, run_id: str) -> OutputPaths:
    output_directory.mkdir(parents=True, exist_ok=True)
    return OutputPaths(
        run_id=run_id,
        transcript=output_directory / f"{run_id}.txt",
        log=output_directory / f"{run_id}.log",
        metrics=output_directory / f"{run_id}.metrics.json",
    )


def ensure_stt_virtual_environment(script_directory: Path) -> None:
    expected_python = script_directory / ".venv-stt" / "Scripts" / "python.exe"
    current_python = Path(sys.executable).resolve()
    if not expected_python.is_file():
        raise FileNotFoundError(
            f"The transcription virtual environment was not found: {expected_python}"
        )
    if current_python == expected_python.resolve():
        return

    run_id = os.environ.get("TRANSCRIBE_RUN_ID") or datetime.now(JST).strftime(
        "%Y%m%d_%H%M%S"
    )
    child_environment = dict(os.environ)
    child_environment["TRANSCRIBE_RUN_ID"] = run_id
    child_environment["TRANSCRIBE_BOOTSTRAP_FROM"] = str(current_python)
    child_environment["TRANSCRIBE_BOOTSTRAP_TO"] = str(expected_python)

    print("PYTHON_ENVIRONMENT_SWITCH")
    print(f"FROM={current_python}")
    print(f"TO={expected_python}")
    completed = subprocess.run(
        [str(expected_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(script_directory),
        env=child_environment,
        check=False,
    )
    raise SystemExit(completed.returncode)


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exception:
        raise TranscriptionError(
            f"Command timed out after {timeout_seconds} seconds: {' '.join(command)}"
        ) from exception


def installed_package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in RUNTIME_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def fetch_latest_pypi_version(package: str) -> str:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{package}/json",
        headers={"User-Agent": "radio-transcription-version-check/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except Exception as exception:
        raise TranscriptionError(
            f"The latest software version could not be checked for {package}. "
            f"{type(exception).__name__}: {exception}"
        ) from exception
    version = str(payload.get("info", {}).get("version", "")).strip()
    if not version:
        raise TranscriptionError(
            f"PyPI returned no current version for {package}."
        )
    return version


def version_is_newer(latest: str, installed: str | None) -> bool:
    try:
        from packaging.version import Version
    except ImportError:
        from pip._vendor.packaging.version import Version
    if installed is None:
        return True
    return Version(latest) > Version(installed)


def check_and_update_runtime_packages(script_directory: Path) -> dict[str, object]:
    print("SOFTWARE_VERSION_CHECK_START")
    print("SOFTWARE_CHECK_METHOD=PYPI_JSON_THEN_PIP_ONLY_IF_UPDATE_REQUIRED")
    before = installed_package_versions()
    print(
        "SOFTWARE_VERSIONS_BEFORE="
        + json.dumps(before, ensure_ascii=False, sort_keys=True)
    )

    latest: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(RUNTIME_PACKAGES)
    ) as executor:
        futures = {
            executor.submit(fetch_latest_pypi_version, package): package
            for package in RUNTIME_PACKAGES
        }
        for future in concurrent.futures.as_completed(futures):
            package = futures[future]
            latest[package] = future.result()

    latest = {package: latest[package] for package in RUNTIME_PACKAGES}
    outdated = [
        package
        for package in RUNTIME_PACKAGES
        if version_is_newer(latest[package], before.get(package))
    ]
    print(
        "SOFTWARE_LATEST_VERSIONS="
        + json.dumps(latest, ensure_ascii=False, sort_keys=True)
    )
    print(
        "SOFTWARE_UPDATE_REQUIRED="
        + (",".join(outdated) if outdated else "NONE")
    )

    if outdated:
        completed = run_command(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--upgrade-strategy",
                "only-if-needed",
                "--disable-pip-version-check",
                "--timeout",
                "30",
                *outdated,
            ],
            cwd=script_directory,
            timeout_seconds=900,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise TranscriptionError(
                "Newer transcription software was found but could not be installed. "
                + details
            )

    after = installed_package_versions()
    changed = {
        package: {"before": before.get(package), "after": after.get(package)}
        for package in RUNTIME_PACKAGES
        if before.get(package) != after.get(package)
    }
    print(
        "SOFTWARE_VERSIONS_AFTER="
        + json.dumps(after, ensure_ascii=False, sort_keys=True)
    )
    print(
        "SOFTWARE_UPDATED="
        + (
            json.dumps(changed, ensure_ascii=False, sort_keys=True)
            if changed
            else "NONE"
        )
    )
    print("SOFTWARE_VERSION_CHECK_RESULT=PASS")
    return {
        "check_method": "pypi_json_then_pip_only_if_update_required",
        "before": before,
        "latest": latest,
        "update_required": outdated,
        "after": after,
        "updated": changed,
    }


def model_files_are_complete(model_directory: Path) -> bool:
    return all((model_directory / name).is_file() for name in MODEL_REQUIRED_FILES)


def get_model_revision(repository: str) -> str:
    from huggingface_hub import HfApi

    try:
        information = HfApi().model_info(
            repo_id=repository,
            revision="main",
            timeout=30,
        )
    except Exception as exception:
        raise TranscriptionError(
            f"The latest model revision could not be checked: {repository}. "
            f"{type(exception).__name__}: {exception}"
        ) from exception
    revision = str(information.sha or "").strip()
    if not revision:
        raise TranscriptionError(
            f"The model service returned no revision for {repository}."
        )
    return revision


def check_model(
    script_directory: Path,
    *,
    repository: str,
    directory_name: str,
    label: str,
    download_now: bool,
) -> dict[str, object]:
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

    model_directory = script_directory / "models" / directory_name
    revision_file = model_directory / ".model_revision.txt"
    installed_revision = (
        revision_file.read_text(encoding="utf-8").strip()
        if revision_file.is_file()
        else ""
    )
    latest_revision = get_model_revision(repository)
    download_required = (
        installed_revision != latest_revision
        or not model_files_are_complete(model_directory)
    )

    print(f"{label}_MODEL_CHECK")
    print(f"MODEL_REPOSITORY={repository}")
    print(f"MODEL_DIRECTORY={model_directory}")
    print(f"MODEL_INSTALLED_REVISION={installed_revision or 'NONE'}")
    print(f"MODEL_LATEST_REVISION={latest_revision}")
    print(f"MODEL_DOWNLOAD_REQUIRED={'YES' if download_required else 'NO'}")

    updated = False
    if download_required and download_now:
        model_directory.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_download(
                repo_id=repository,
                revision=latest_revision,
                local_dir=str(model_directory),
                allow_patterns=list(MODEL_ALLOW_PATTERNS),
            )
        except Exception as exception:
            raise TranscriptionError(
                f"The latest model files could not be downloaded: {repository}. "
                f"{type(exception).__name__}: {exception}"
            ) from exception
        if not model_files_are_complete(model_directory):
            missing = [
                name
                for name in MODEL_REQUIRED_FILES
                if not (model_directory / name).is_file()
            ]
            raise TranscriptionError(
                f"The downloaded model is incomplete: {repository}. Missing: "
                + ", ".join(missing)
            )
        revision_file.write_text(latest_revision + "\n", encoding="utf-8")
        updated = True

    return {
        "repository": repository,
        "directory": str(model_directory),
        "revision": latest_revision,
        "installed_revision": installed_revision or None,
        "download_required": download_required,
        "updated": updated,
    }


def ensure_repair_model(
    script_directory: Path,
    model_information: dict[str, object],
) -> dict[str, object]:
    if not bool(model_information["download_required"]):
        return model_information
    return check_model(
        script_directory,
        repository=REPAIR_MODEL_REPOSITORY,
        directory_name=REPAIR_MODEL_DIRECTORY_NAME,
        label="REPAIR",
        download_now=True,
    )


def validate_environment(
    input_file: Path,
    batch_size: int,
    opening_audit_seconds: float,
    max_repair_windows: int,
    ctranslate2: object,
) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")
    if batch_size < 1 or batch_size > 32:
        raise ValueError("--batch-size must be between 1 and 32.")
    if opening_audit_seconds < 0 or opening_audit_seconds > 600:
        raise ValueError("--opening-audit-seconds must be between 0 and 600.")
    if max_repair_windows < 0 or max_repair_windows > 4:
        raise ValueError("--max-repair-windows must be between 0 and 4.")
    if ctranslate2.get_cuda_device_count() < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")
    supported = ctranslate2.get_supported_compute_types("cuda")
    if COMPUTE_TYPE not in supported:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is not supported. Supported types: {sorted(supported)}"
        )


def load_model(WhisperModel: object, model_path: Path, source: str) -> object:
    print("MODEL_LOAD_START")
    print(f"MODEL_SOURCE={source}")
    print(f"MODEL_PATH={model_path}")
    print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
    model = WhisperModel(
        str(model_path),
        device=DEVICE,
        device_index=0,
        compute_type=COMPUTE_TYPE,
        flash_attention=False,
        local_files_only=True,
    )
    print("FLASH_ATTENTION=False")
    print("MODEL_LOAD_COMPLETE")
    return model


def clean_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def segment_from_object(
    segment: object,
    *,
    source: str,
    offset: float = 0.0,
) -> SegmentRecord | None:
    text = clean_text(str(getattr(segment, "text", "")))
    if not text:
        return None
    start = max(0.0, float(getattr(segment, "start", 0.0)) + offset)
    end = max(start, float(getattr(segment, "end", start)) + offset)
    return SegmentRecord(
        start=start,
        end=end,
        text=text,
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
        source=source,
    )


def analyze_audio_signal(audio: object) -> dict[str, object]:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        raise TranscriptionError("The decoded audio contains no samples.")

    absolute = np.abs(samples)
    peak = float(np.max(absolute))
    rms = float(np.sqrt(np.mean(samples * samples)))
    clipped_ratio = float(np.mean(absolute >= 0.999))

    frame_size = max(1, int(SAMPLE_RATE * 0.03))
    frame_count = samples.size // frame_size
    if frame_count > 0:
        frames = samples[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-12))
        low_level_frame_ratio = float(np.mean(frame_dbfs < -50.0))
        p10_dbfs = float(np.percentile(frame_dbfs, 10))
        p50_dbfs = float(np.percentile(frame_dbfs, 50))
        p95_dbfs = float(np.percentile(frame_dbfs, 95))
    else:
        low_level_frame_ratio = 0.0
        p10_dbfs = p50_dbfs = p95_dbfs = -120.0

    peak_dbfs = 20.0 * math.log10(max(peak, 1e-12))
    rms_dbfs = 20.0 * math.log10(max(rms, 1e-12))
    flags: list[str] = []
    if clipped_ratio > 0.001:
        flags.append("clipping_detected")
    if rms_dbfs < -35.0:
        flags.append("low_recording_level")
    if low_level_frame_ratio > 0.70:
        flags.append("high_low_level_frame_ratio")
    if p95_dbfs - p10_dbfs < 8.0:
        flags.append("low_dynamic_range")

    return {
        "sample_rate": SAMPLE_RATE,
        "sample_count": int(samples.size),
        "peak_dbfs": round(peak_dbfs, 3),
        "rms_dbfs": round(rms_dbfs, 3),
        "clipped_sample_ratio": clipped_ratio,
        "low_level_frame_ratio": low_level_frame_ratio,
        "frame_dbfs_p10": round(p10_dbfs, 3),
        "frame_dbfs_p50": round(p50_dbfs, 3),
        "frame_dbfs_p95": round(p95_dbfs, 3),
        "dynamic_range_p95_minus_p10_db": round(p95_dbfs - p10_dbfs, 3),
        "quality_flags": flags,
        "quality_status": "PASS" if not flags else "REVIEW",
        "scope": "SIGNAL_LEVEL_DIAGNOSTIC_NOT_TRANSCRIPTION_ACCURACY",
    }


def normalize_for_comparison(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龯々]+", "", text.lower())


def english_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


def detect_decoder_loop(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    for unit_length in range(2, 17):
        if re.search(rf"(.{{{unit_length}}})\1{{8,}}", compact):
            return True
    return False


def strict_section_hits(text: str) -> dict[str, bool]:
    lower_text = text.lower()
    return {
        canonical: any(alias.lower() in lower_text for alias in aliases)
        for canonical, aliases in STRICT_SECTION_ALIASES.items()
    }


def split_english_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for piece in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        piece = clean_text(piece)
        if 5 <= len(english_words(piece)) <= 30 and latin_character_count(piece) >= 18:
            candidates.append(piece)
    return candidates


def discover_repeated_anchors(segments: Iterable[SegmentRecord]) -> list[str]:
    candidates: list[str] = []
    for segment in segments:
        candidates.extend(split_english_candidates(segment.text))

    clusters: list[list[str]] = []
    for candidate in candidates:
        normalized = normalize_for_comparison(candidate)
        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None,
                normalized,
                normalize_for_comparison(cluster[0]),
            ).ratio()
            if ratio > best_ratio:
                best_index = index
                best_ratio = ratio
        if best_index is not None and best_ratio >= 0.82:
            clusters[best_index].append(candidate)
        else:
            clusters.append([candidate])

    results: list[tuple[int, str]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        normalized_counts = Counter(
            normalize_for_comparison(value) for value in cluster
        )
        representative_normalized = max(
            normalized_counts,
            key=lambda value: (normalized_counts[value], len(value)),
        )
        matching = [
            value
            for value in cluster
            if normalize_for_comparison(value) == representative_normalized
        ]
        representative = max(matching, key=len)
        results.append((len(cluster), representative))
    results.sort(key=lambda item: (-item[0], -len(item[1])))
    return [text for _count, text in results[:10]]


def correct_repeated_english_variants(
    text: str,
) -> tuple[str, list[dict[str, object]], int]:
    candidates = split_english_candidates(text)
    clusters: list[list[str]] = []
    for candidate in candidates:
        normalized = normalize_for_comparison(candidate)
        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None,
                normalized,
                normalize_for_comparison(cluster[0]),
            ).ratio()
            if ratio > best_ratio:
                best_index = index
                best_ratio = ratio
        if best_index is not None and best_ratio >= 0.82:
            clusters[best_index].append(candidate)
        else:
            clusters.append([candidate])

    corrected = text
    corrections: list[dict[str, object]] = []
    unresolved = 0
    for cluster in clusters:
        exact_counts = Counter(clean_text(value) for value in cluster)
        if sum(exact_counts.values()) < 4 or len(exact_counts) < 2:
            continue
        canonical, canonical_count = max(
            exact_counts.items(),
            key=lambda item: (item[1], len(item[0])),
        )
        canonical_words = english_words(canonical)
        if canonical_count < 2 or len(canonical_words) < 5:
            continue
        cluster_changed = False
        for variant, variant_count in exact_counts.items():
            if variant == canonical:
                continue
            variant_words = english_words(variant)
            if not variant_words:
                continue
            if canonical_words[0].lower() != variant_words[0].lower():
                continue
            if canonical_words[-1].lower() != variant_words[-1].lower():
                continue
            if abs(len(canonical_words) - len(variant_words)) > 3:
                continue
            canonical_normalized = normalize_for_comparison(canonical)
            variant_normalized = normalize_for_comparison(variant)
            if canonical_normalized in variant_normalized or variant_normalized in canonical_normalized:
                continue
            similarity = SequenceMatcher(
                None,
                canonical_normalized,
                variant_normalized,
            ).ratio()
            if similarity < 0.82 or canonical_count <= variant_count:
                continue
            occurrences = corrected.count(variant)
            if occurrences == 0:
                continue
            corrected = corrected.replace(variant, canonical)
            corrections.append(
                {
                    'from': variant,
                    'to': canonical,
                    'occurrences': occurrences,
                    'canonical_count': canonical_count,
                    'variant_count': variant_count,
                    'similarity': round(similarity, 6),
                }
            )
            cluster_changed = True
        if not cluster_changed:
            unresolved += 1

    return corrected, corrections, unresolved


def recover_repeated_section_examples(
    baseline_text: str,
    audit_text: str,
) -> list[dict[str, object]]:
    baseline_candidates = split_english_candidates(baseline_text)
    audit_candidates = split_english_candidates(audit_text)
    clusters: list[list[tuple[int, str]]] = []

    for position, candidate in enumerate(audit_candidates):
        normalized = normalize_for_comparison(candidate)
        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None,
                normalized,
                normalize_for_comparison(cluster[0][1]),
            ).ratio()
            if ratio > best_ratio:
                best_index = index
                best_ratio = ratio
        if best_index is not None and best_ratio >= 0.90:
            clusters[best_index].append((position, candidate))
        else:
            clusters.append([(position, candidate)])

    recovered: list[dict[str, object]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        normalized_counts = Counter(
            normalize_for_comparison(candidate)
            for _position, candidate in cluster
        )
        canonical_normalized, occurrence_count = max(
            normalized_counts.items(),
            key=lambda item: (item[1], len(item[0])),
        )
        if occurrence_count < 2:
            continue
        matching = [
            (position, candidate)
            for position, candidate in cluster
            if normalize_for_comparison(candidate) == canonical_normalized
        ]
        first_position = min(position for position, _candidate in matching)
        canonical = max((candidate for _position, candidate in matching), key=len)
        words = english_words(canonical)
        compact = re.sub(r"\s+", "", canonical)
        if not 5 <= len(words) <= 28 or not compact:
            continue
        if latin_character_count(canonical) / len(compact) < 0.82:
            continue
        if any(section.lower() in canonical.lower() for section in STRICT_SECTION_ALIASES):
            continue
        if any(
            SequenceMatcher(
                None,
                canonical_normalized,
                normalize_for_comparison(existing),
            ).ratio()
            >= 0.84
            for existing in baseline_candidates
        ):
            continue
        recovered.append(
            {
                "text": canonical,
                "occurrences": occurrence_count,
                "first_position": first_position,
                "evidence": "repeated_in_forced_english_section_audit",
            }
        )

    recovered.sort(key=lambda item: int(item["first_position"]))
    return recovered


def recover_cross_window_section_examples(
    baseline_text: str,
    audit_segments: list[SegmentRecord],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    baseline_candidates = split_english_candidates(baseline_text)
    observations: list[dict[str, object]] = []
    for segment in audit_segments:
        for candidate in split_english_candidates(segment.text):
            words = english_words(candidate)
            compact = re.sub(r"\s+", "", candidate)
            if not 5 <= len(words) <= 24 or not compact:
                continue
            if latin_character_count(candidate) / len(compact) < 0.88:
                continue
            unique_ratio = len({word.lower() for word in words}) / len(words)
            if unique_ratio < 0.45:
                continue
            observations.append(
                {
                    "text": candidate,
                    "normalized": normalize_for_comparison(candidate),
                    "start": segment.start,
                    "end": segment.end,
                    "midpoint": (segment.start + segment.end) / 2.0,
                    "source": segment.source,
                    "avg_logprob": segment.avg_logprob,
                }
            )

    clusters: list[list[dict[str, object]]] = []
    for observation in observations:
        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None,
                str(observation["normalized"]),
                str(cluster[0]["normalized"]),
            ).ratio()
            if ratio > best_ratio:
                best_index = index
                best_ratio = ratio
        if best_index is not None and best_ratio >= 0.88:
            clusters[best_index].append(observation)
        else:
            clusters.append([observation])

    recovered: list[dict[str, object]] = []
    evidence_log: list[dict[str, object]] = []
    for cluster_index, cluster in enumerate(clusters):
        variant_counts = Counter(clean_text(str(item["text"])) for item in cluster)
        canonical = max(
            variant_counts,
            key=lambda value: (variant_counts[value], len(value)),
        )
        canonical_normalized = normalize_for_comparison(canonical)
        matching = [
            item
            for item in cluster
            if SequenceMatcher(
                None,
                canonical_normalized,
                str(item["normalized"]),
            ).ratio()
            >= 0.90
        ]
        matching.sort(key=lambda item: float(item["midpoint"]))

        temporal_events: list[list[dict[str, object]]] = []
        for item in matching:
            midpoint = float(item["midpoint"])
            if (
                not temporal_events
                or midpoint
                - sum(float(value["midpoint"]) for value in temporal_events[-1])
                / len(temporal_events[-1])
                > SECTION_EVENT_CLUSTER_SECONDS
            ):
                temporal_events.append([item])
            else:
                temporal_events[-1].append(item)

        distinct_sources = {str(item["source"]) for item in matching}
        supported_events = [
            event
            for event in temporal_events
            if len({str(item["source"]) for item in event})
            >= SECTION_CROSS_WINDOW_MIN_SUPPORT
        ]
        evidence_type = "cross_window_same_utterance"
        if not supported_events and len(temporal_events) >= 2 and len(distinct_sources) >= 2:
            supported_events = temporal_events
            evidence_type = "same_sentence_repeated_at_separate_times"

        average_probability = (
            sum(float(item["avg_logprob"]) for item in matching) / len(matching)
            if matching
            else -10.0
        )
        existing_similarity = max(
            (
                SequenceMatcher(
                    None,
                    canonical_normalized,
                    normalize_for_comparison(existing),
                ).ratio()
                for existing in baseline_candidates
            ),
            default=0.0,
        )
        accepted = bool(supported_events)
        reason = "accepted"
        if existing_similarity >= 0.84:
            accepted = False
            reason = "already_present_or_close_variant"
        elif average_probability < -0.65:
            accepted = False
            reason = "low_cross_window_confidence"
        elif not supported_events:
            reason = "insufficient_independent_support"

        event_records = []
        for event in supported_events:
            event_records.append(
                {
                    "start": min(float(item["start"]) for item in event),
                    "end": max(float(item["end"]) for item in event),
                    "window_support": len(
                        {str(item["source"]) for item in event}
                    ),
                    "sources": sorted(
                        {str(item["source"]) for item in event}
                    ),
                    "avg_logprob": (
                        sum(float(item["avg_logprob"]) for item in event)
                        / len(event)
                    ),
                }
            )

        evidence = {
            "cluster_index": cluster_index,
            "text": canonical,
            "observation_count": len(matching),
            "distinct_window_count": len(distinct_sources),
            "temporal_event_count": len(temporal_events),
            "supported_event_count": len(supported_events),
            "average_logprob": round(average_probability, 6),
            "existing_similarity": round(existing_similarity, 6),
            "accepted": accepted,
            "decision_reason": reason,
            "evidence_type": evidence_type,
            "events": event_records,
        }
        evidence_log.append(evidence)
        if not accepted:
            continue
        recovered.append(
            {
                "text": canonical,
                "occurrences": len(event_records),
                "first_position": int(min(event["start"] for event in event_records) * 1000),
                "evidence": evidence_type,
                "average_logprob": average_probability,
                "events": event_records,
            }
        )

    recovered.sort(key=lambda item: int(item["first_position"]))
    return recovered, evidence_log


def recovered_examples_to_segments(
    recovered_examples: list[dict[str, object]],
) -> list[SegmentRecord]:
    segments: list[SegmentRecord] = []
    for example_index, item in enumerate(recovered_examples):
        text = str(item["text"]).strip()
        for event_index, event in enumerate(item.get("events", [])):
            event_data = dict(event)
            segments.append(
                SegmentRecord(
                    start=float(event_data["start"]),
                    end=float(event_data["end"]),
                    text=text,
                    avg_logprob=float(event_data["avg_logprob"]),
                    compression_ratio=0.0,
                    no_speech_prob=0.0,
                    source=(
                        f"turbo_essential_dense_consensus_"
                        f"{example_index:02d}_{event_index:02d}"
                    ),
                )
            )
    return segments


def merge_recovered_examples(
    *groups: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for group in groups:
        for item in group:
            text = str(item.get("text", "")).strip()
            normalized = normalize_for_comparison(text)
            if not normalized:
                continue
            current = merged.get(normalized)
            if current is None:
                merged[normalized] = dict(item)
                continue
            current["occurrences"] = max(
                int(current.get("occurrences", 1)),
                int(item.get("occurrences", 1)),
            )
            current["first_position"] = min(
                int(current.get("first_position", 0)),
                int(item.get("first_position", 0)),
            )
            evidence = {
                str(current.get("evidence", "")),
                str(item.get("evidence", "")),
            }
            current["evidence"] = "+".join(sorted(value for value in evidence if value))
    results = list(merged.values())
    results.sort(
        key=lambda item: (
            int(item.get("first_position", 0)),
            str(item.get("text", "")),
        )
    )
    return results


def insert_recovered_examples(
    text: str,
    recovered_examples: list[dict[str, object]],
) -> str:
    if not recovered_examples:
        return text
    recovered_lines: list[str] = []
    for item in recovered_examples:
        sentence = str(item["text"]).strip()
        occurrences = max(1, int(item["occurrences"]))
        recovered_lines.extend(sentence for _index in range(occurrences))
    insertion = "\n".join(recovered_lines).strip()
    if not insertion:
        return text

    match = re.search(r"(?mi)^Practical Usage\s*$", text)
    if match is None:
        return text.rstrip() + "\n\n" + insertion + "\n"
    before = text[:match.start()].rstrip()
    after = text[match.start():].lstrip()
    return before + "\n\n" + insertion + "\n\n" + after


def anchor_exact_hits(text: str, anchors: Iterable[str]) -> int:
    normalized_text = normalize_for_comparison(text)
    return sum(
        normalized_text.count(normalize_for_comparison(anchor))
        for anchor in anchors
        if normalize_for_comparison(anchor)
    )


def deduplicate_segments(segments: Iterable[SegmentRecord]) -> list[SegmentRecord]:
    result: list[SegmentRecord] = []
    for candidate in sorted(segments, key=lambda item: (item.start, item.end)):
        duplicate_index: int | None = None
        for index in range(len(result) - 1, max(-1, len(result) - 5), -1):
            previous = result[index]
            if candidate.start - previous.end > 1.0:
                break
            overlap = min(previous.end, candidate.end) - max(previous.start, candidate.start)
            if overlap <= 0.05:
                continue
            left = normalize_for_comparison(previous.text)
            right = normalize_for_comparison(candidate.text)
            if not left or not right:
                continue
            similarity = SequenceMatcher(None, left, right).ratio()
            if similarity >= 0.90 or left in right or right in left:
                duplicate_index = index
                break
        if duplicate_index is None:
            result.append(candidate)
            continue
        previous = result[duplicate_index]
        previous_quality = previous.avg_logprob + min(len(previous.text), 180) / 1000.0
        candidate_quality = candidate.avg_logprob + min(len(candidate.text), 180) / 1000.0
        if candidate_quality > previous_quality:
            result[duplicate_index] = candidate
    return sorted(result, key=lambda item: (item.start, item.end))


def format_transcript(segments: Iterable[SegmentRecord]) -> str:
    lines: list[str] = []
    previous_end: float | None = None
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        text = clean_text(segment.text)
        if not text:
            continue
        if previous_end is not None and segment.start - previous_end >= 1.1:
            if lines and lines[-1] != "":
                lines.append("")
        lines.append(text)
        previous_end = segment.end

    compact: list[str] = []
    for line in lines:
        if line == "" and compact and compact[-1] == "":
            continue
        compact.append(line)
    transcript = "\n".join(compact).strip()
    if not transcript:
        raise TranscriptionError("The final transcript is empty.")
    return transcript + "\n"


def is_cuda_memory_error(exception: BaseException) -> bool:
    message = str(exception).lower()
    return any(
        term in message
        for term in (
            "out of memory",
            "cuda_error_out_of_memory",
            "failed to allocate",
            "memory allocation",
        )
    )


def batch_candidates(initial: int) -> list[int]:
    values: list[int] = []
    current = initial
    while current >= 1:
        if current not in values:
            values.append(current)
        if current == 1:
            break
        current = max(1, current // 2)
    return values


def transcribe_batched_baseline(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    language: str | None,
    requested_batch_size: int,
) -> tuple[list[SegmentRecord], int, str]:
    pipeline = BatchedInferencePipeline(model=model)
    last_exception: BaseException | None = None

    for batch_size in batch_candidates(requested_batch_size):
        print("PRIMARY_TRANSCRIPTION_ATTEMPT")
        print(f"BATCH_SIZE={batch_size}")
        try:
            iterator, information = pipeline.transcribe(
                audio,
                task="transcribe",
                language=language,
                multilingual=True,
                beam_size=5,
                best_of=5,
                patience=1.0,
                temperature=0.0,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 180,
                    "speech_pad_ms": 500,
                    "min_speech_duration_ms": 80,
                },
                condition_on_previous_text=False,
                without_timestamps=False,
                word_timestamps=False,
                hotwords=TRANSCRIPTION_GLOSSARY,
                batch_size=batch_size,
                log_progress=True,
            )
            records = [
                record
                for segment in iterator
                if (record := segment_from_object(segment, source="turbo_batched"))
                is not None
            ]
            if not records:
                raise TranscriptionError("The primary model returned no transcript segments.")
            detected_language = str(getattr(information, "language", language or "unknown"))
            return deduplicate_segments(records), batch_size, detected_language
        except Exception as exception:
            last_exception = exception
            if not is_cuda_memory_error(exception):
                raise
            print(f"GPU_MEMORY_RETRY=BATCH_SIZE_{batch_size}")
            gc.collect()

    raise TranscriptionError(
        "The primary transcription failed at every batch size."
    ) from last_exception


def transcribe_slice(
    model: object,
    audio: object,
    *,
    start: float,
    end: float,
    language: str | None,
    source: str,
    beam_size: int,
    multilingual: bool = True,
) -> list[SegmentRecord]:
    start_sample = int(max(0.0, start) * SAMPLE_RATE)
    end_sample = int(max(start, end) * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]
    iterator, _information = model.transcribe(
        audio_slice,
        task="transcribe",
        language=language,
        multilingual=multilingual,
        beam_size=beam_size,
        best_of=beam_size,
        patience=1.0,
        temperature=(0.0, 0.2, 0.4),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        hotwords=TRANSCRIPTION_GLOSSARY,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        log_progress=False,
    )
    records = [
        record
        for segment in iterator
        if (
            record := segment_from_object(
                segment,
                source=source,
                offset=start,
            )
        )
        is not None
    ]
    return deduplicate_segments(records)


def transcribe_chunked_english_section(
    model: object,
    audio: object,
    *,
    start: float,
    end: float,
) -> tuple[list[SegmentRecord], int]:
    if end <= start:
        return [], 0
    records: list[SegmentRecord] = []
    window_count = 0
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + SECTION_CHUNK_SECONDS)
        window_count += 1
        print(
            f"SECTION_DENSE_AUDIT_WINDOW={window_count:02d} "
            f"START={cursor:.3f} END={chunk_end:.3f}"
        )
        records.extend(
            transcribe_slice(
                model,
                audio,
                start=cursor,
                end=chunk_end,
                language="en",
                source=f"turbo_essential_dense_en_w{window_count:02d}",
                beam_size=3,
                multilingual=False,
            )
        )
        if chunk_end >= end:
            break
        next_cursor = chunk_end - SECTION_CHUNK_OVERLAP_SECONDS
        if next_cursor <= cursor:
            next_cursor = cursor + 1.0
        cursor = next_cursor
    return records, window_count


def average_logprob(segments: Iterable[SegmentRecord]) -> float:
    values = [segment.avg_logprob for segment in segments]
    return sum(values) / len(values) if values else -10.0


def quality_score(text: str, segments: list[SegmentRecord]) -> float:
    confidence = max(0.0, min(1.0, (average_logprob(segments) + 1.2) / 1.2))
    length_score = min(len(text) / 240.0, 1.0)
    penalty = 0.0
    if "�" in text:
        penalty += 0.5
    if detect_decoder_loop(text):
        penalty += 0.8
    return max(0.0, confidence * 0.78 + length_score * 0.22 - penalty)


def comparison_tokens(text: str) -> list[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[ぁ-んァ-ヶ一-龯々]", text)
    ]


def token_recall(baseline_text: str, candidate_text: str) -> float:
    baseline_tokens = comparison_tokens(baseline_text)
    available = Counter(comparison_tokens(candidate_text))
    if not baseline_tokens:
        return 1.0
    matched = 0
    for token in baseline_tokens:
        if available[token] > 0:
            matched += 1
            available[token] -= 1
    return matched / len(baseline_tokens)


def opening_core_end(
    segments: list[SegmentRecord],
    requested_seconds: float,
    audio_seconds: float,
) -> float:
    if requested_seconds <= 0:
        return 0.0
    limit = min(requested_seconds, audio_seconds)
    candidates = [segment.end for segment in segments if segment.end <= limit]
    if not candidates:
        return limit
    return max(candidates)


def segments_before(
    segments: Iterable[SegmentRecord],
    end_time: float,
) -> list[SegmentRecord]:
    return [
        segment
        for segment in segments
        if (segment.start + segment.end) / 2.0 <= end_time
    ]


def decide_opening_audit(
    baseline_segments: list[SegmentRecord],
    audit_segments: list[SegmentRecord],
    anchors: list[str],
    core_end: float,
) -> OpeningAuditDecision:
    baseline_text = format_transcript(baseline_segments).strip()
    audit_text = format_transcript(audit_segments).strip() if audit_segments else ""
    coverage_ratio = len(audit_text) / max(len(baseline_text), 1)
    recall = token_recall(baseline_text, audit_text)
    baseline_score = quality_score(baseline_text, baseline_segments)
    audit_score = quality_score(audit_text, audit_segments)
    baseline_anchor_hits = anchor_exact_hits(baseline_text, anchors)
    audit_anchor_hits = anchor_exact_hits(audit_text, anchors)
    baseline_section_hits = sum(strict_section_hits(baseline_text).values())
    audit_section_hits = sum(strict_section_hits(audit_text).values())

    accepted = False
    reason = "baseline_retained"
    if not audit_text:
        reason = "audit_empty"
    elif "�" in audit_text or detect_decoder_loop(audit_text):
        reason = "audit_anomaly_rejected"
    elif not 0.98 <= coverage_ratio <= 1.50:
        reason = "audit_coverage_guard_rejected"
    elif recall < 0.80:
        reason = "audit_content_recall_rejected"
    elif audit_anchor_hits < baseline_anchor_hits:
        reason = "audit_anchor_guard_rejected"
    elif audit_section_hits < baseline_section_hits:
        reason = "audit_section_guard_rejected"
    elif coverage_ratio >= 1.03 and audit_score >= baseline_score - 0.08:
        accepted = True
        reason = "opening_coverage_improved"
    elif audit_score >= baseline_score + 0.12 and coverage_ratio >= 0.99:
        accepted = True
        reason = "opening_confidence_improved"

    return OpeningAuditDecision(
        accepted=accepted,
        reason=reason,
        core_end=core_end,
        baseline_text=baseline_text,
        audit_text=audit_text,
        coverage_ratio=coverage_ratio,
        token_recall=recall,
        baseline_score=baseline_score,
        audit_score=audit_score,
        baseline_anchor_hits=baseline_anchor_hits,
        audit_anchor_hits=audit_anchor_hits,
        baseline_section_hits=baseline_section_hits,
        audit_section_hits=audit_section_hits,
    )


def apply_opening_audit(
    segments: list[SegmentRecord],
    audit_segments: list[SegmentRecord],
    core_end: float,
) -> list[SegmentRecord]:
    retained = [
        segment
        for segment in segments
        if (segment.start + segment.end) / 2.0 > core_end
    ]
    return deduplicate_segments([*audit_segments, *retained])


def segments_between(
    segments: Iterable[SegmentRecord],
    start_time: float,
    end_time: float,
) -> list[SegmentRecord]:
    return [
        segment
        for segment in segments
        if start_time <= (segment.start + segment.end) / 2.0 <= end_time
    ]


def find_section_interval(
    segments: list[SegmentRecord],
    start_section: str,
    end_section: str,
) -> tuple[float, float] | None:
    start_time: float | None = None
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        hits = strict_section_hits(segment.text)
        if start_time is None and hits.get(start_section, False):
            start_time = segment.start
            continue
        if start_time is not None and hits.get(end_section, False):
            if segment.start > start_time:
                return start_time, segment.start
    return None


def decide_section_audit(
    section_name: str,
    baseline_segments: list[SegmentRecord],
    audit_segments: list[SegmentRecord],
    anchors: list[str],
    core_start: float,
    core_end: float,
) -> SectionAuditDecision:
    baseline_text = format_transcript(baseline_segments).strip()
    audit_text = format_transcript(audit_segments).strip() if audit_segments else ''
    coverage_ratio = len(audit_text) / max(len(baseline_text), 1)
    recall = token_recall(baseline_text, audit_text)
    baseline_score = quality_score(baseline_text, baseline_segments)
    audit_score = quality_score(audit_text, audit_segments)
    baseline_anchor_hits = anchor_exact_hits(baseline_text, anchors)
    audit_anchor_hits = anchor_exact_hits(audit_text, anchors)
    baseline_section_hits = sum(strict_section_hits(baseline_text).values())
    audit_section_hits = sum(strict_section_hits(audit_text).values())
    baseline_candidates = len(split_english_candidates(baseline_text))
    audit_candidates = len(split_english_candidates(audit_text))

    accepted = False
    reason = 'baseline_retained'
    if not audit_text:
        reason = 'audit_empty'
    elif '�' in audit_text or detect_decoder_loop(audit_text):
        reason = 'audit_anomaly_rejected'
    elif not 0.95 <= coverage_ratio <= 1.65:
        reason = 'audit_coverage_guard_rejected'
    elif recall < 0.72:
        reason = 'audit_content_recall_rejected'
    elif audit_anchor_hits < baseline_anchor_hits:
        reason = 'audit_anchor_guard_rejected'
    elif audit_section_hits < baseline_section_hits:
        reason = 'audit_section_guard_rejected'
    elif audit_candidates >= baseline_candidates + 2 and audit_score >= baseline_score - 0.10:
        accepted = True
        reason = 'section_examples_recovered'
    elif coverage_ratio >= 1.06 and audit_score >= baseline_score - 0.05:
        accepted = True
        reason = 'section_coverage_improved'

    return SectionAuditDecision(
        accepted=accepted,
        reason=reason,
        section_name=section_name,
        core_start=core_start,
        core_end=core_end,
        baseline_text=baseline_text,
        audit_text=audit_text,
        coverage_ratio=coverage_ratio,
        token_recall=recall,
        baseline_score=baseline_score,
        audit_score=audit_score,
        baseline_anchor_hits=baseline_anchor_hits,
        audit_anchor_hits=audit_anchor_hits,
        baseline_section_hits=baseline_section_hits,
        audit_section_hits=audit_section_hits,
        baseline_english_candidates=baseline_candidates,
        audit_english_candidates=audit_candidates,
    )


def apply_section_audit(
    segments: list[SegmentRecord],
    audit_segments: list[SegmentRecord],
    core_start: float,
    core_end: float,
) -> list[SegmentRecord]:
    retained = [
        segment
        for segment in segments
        if not core_start <= (segment.start + segment.end) / 2.0 <= core_end
    ]
    return deduplicate_segments([*retained, *audit_segments])


def detect_strong_events(segments: list[SegmentRecord]) -> list[SuspicionEvent]:
    events: list[SuspicionEvent] = []
    for index, segment in enumerate(segments):
        duration = max(segment.end - segment.start, 0.1)
        if segment.avg_logprob < -0.90:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    2.5,
                    "low_logprob",
                    (index,),
                )
            )
        if segment.compression_ratio > 2.50:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    2.8,
                    "high_compression",
                    (index,),
                )
            )
        if "�" in segment.text:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    4.0,
                    "replacement_character",
                    (index,),
                )
            )
        if detect_decoder_loop(segment.text):
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    5.0,
                    "decoder_loop",
                    (index,),
                )
            )
        if duration >= 5.0 and len(segment.text) / duration < 0.9:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    2.0,
                    "very_low_text_rate",
                    (index,),
                )
            )
    return events


def build_repair_windows(
    events: list[SuspicionEvent],
    segments: list[SegmentRecord],
    audio_seconds: float,
    max_windows: int,
) -> list[RepairWindow]:
    if max_windows == 0 or not events:
        return []
    grouped: list[list[SuspicionEvent]] = []
    for event in sorted(events, key=lambda item: (item.start, item.end)):
        if not grouped or event.start > max(item.end for item in grouped[-1]) + 1.0:
            grouped.append([event])
        else:
            grouped[-1].append(event)

    candidates: list[tuple[float, RepairWindow]] = []
    for group in grouped:
        indexes = sorted(
            {index for event in group for index in event.segment_indexes}
        )
        core_start = min(segments[index].start for index in indexes)
        core_end = max(segments[index].end for index in indexes)
        reasons = tuple(sorted({event.reason for event in group}))
        severity = sum(event.severity for event in group)
        candidates.append(
            (
                severity,
                RepairWindow(
                    index=0,
                    core_start=core_start,
                    core_end=core_end,
                    slice_start=max(0.0, core_start - REPAIR_PADDING_SECONDS),
                    slice_end=min(audio_seconds, core_end + REPAIR_PADDING_SECONDS),
                    reasons=reasons,
                    segment_indexes=tuple(indexes),
                ),
            )
        )

    candidates.sort(key=lambda item: (-item[0], item[1].core_start))
    selected = [item[1] for item in candidates[:max_windows]]
    selected.sort(key=lambda item: item.core_start)
    return [
        RepairWindow(
            index=index,
            core_start=window.core_start,
            core_end=window.core_end,
            slice_start=window.slice_start,
            slice_end=window.slice_end,
            reasons=window.reasons,
            segment_indexes=window.segment_indexes,
        )
        for index, window in enumerate(selected)
    ]


def segments_in_core(
    segments: Iterable[SegmentRecord],
    core_start: float,
    core_end: float,
) -> list[SegmentRecord]:
    return [
        segment
        for segment in segments
        if core_start <= (segment.start + segment.end) / 2.0 <= core_end
    ]


def decide_repair(
    window: RepairWindow,
    baseline_segments: list[SegmentRecord],
    repair_segments: list[SegmentRecord],
) -> RepairDecision:
    baseline_text = format_transcript(baseline_segments).strip()
    repair_text = format_transcript(repair_segments).strip() if repair_segments else ""
    coverage_ratio = len(repair_text) / max(len(baseline_text), 1)
    recall = token_recall(baseline_text, repair_text)
    baseline_score = quality_score(baseline_text, baseline_segments)
    repair_score = quality_score(repair_text, repair_segments)
    baseline_suspicious = len(detect_strong_events(baseline_segments))
    repair_suspicious = len(detect_strong_events(repair_segments))

    accepted = False
    reason = "baseline_retained"
    if not repair_text:
        reason = "repair_empty"
    elif not 0.92 <= coverage_ratio <= 1.50:
        reason = "repair_coverage_guard_rejected"
    elif recall < 0.75:
        reason = "repair_content_recall_rejected"
    elif repair_suspicious < baseline_suspicious and repair_score >= baseline_score - 0.05:
        accepted = True
        reason = "strong_anomaly_reduced"
    elif repair_score >= baseline_score + 0.15 and repair_suspicious <= baseline_suspicious:
        accepted = True
        reason = "repair_quality_improved"

    return RepairDecision(
        window_index=window.index,
        accepted=accepted,
        reason=reason,
        baseline_text=baseline_text,
        repair_text=repair_text,
        coverage_ratio=coverage_ratio,
        token_recall=recall,
        baseline_score=baseline_score,
        repair_score=repair_score,
        baseline_suspicious_events=baseline_suspicious,
        repair_suspicious_events=repair_suspicious,
        core_start=window.core_start,
        core_end=window.core_end,
        slice_start=window.slice_start,
        slice_end=window.slice_end,
        reasons=window.reasons,
    )


def apply_repair(
    segments: list[SegmentRecord],
    replacement: list[SegmentRecord],
    window: RepairWindow,
) -> list[SegmentRecord]:
    retained = [
        segment
        for segment in segments
        if not window.core_start
        <= (segment.start + segment.end) / 2.0
        <= window.core_end
    ]
    return deduplicate_segments([*retained, *replacement])


def transcript_indicators(
    text: str,
    segments: list[SegmentRecord],
    anchors: list[str],
) -> dict[str, object]:
    sections = strict_section_hits(text)
    events = detect_strong_events(segments)
    return {
        "text_characters": len(text),
        "segment_count": len(segments),
        "japanese_characters": japanese_character_count(text),
        "latin_characters": latin_character_count(text),
        "replacement_characters": text.count("�"),
        "decoder_loop": detect_decoder_loop(text),
        "strict_section_hits": sections,
        "strict_section_count": sum(sections.values()),
        "anchor_exact_hits": anchor_exact_hits(text, anchors),
        "strong_suspicious_event_count": len(events),
        "strong_suspicious_reason_counts": dict(
            Counter(event.reason for event in events)
        ),
    }


def automatic_review_status(
    indicators: dict[str, object],
    audio_seconds: float,
) -> str:
    if bool(indicators["decoder_loop"]):
        return "RERUN_RECOMMENDED"
    if int(indicators["replacement_characters"]) > 0:
        return "RERUN_RECOMMENDED"
    if int(indicators["strong_suspicious_event_count"]) > 1:
        return "REVIEW_REQUIRED"
    if int(indicators["text_characters"]) < int(audio_seconds * 4.2):
        return "REVIEW_REQUIRED"
    return "READY_FOR_AI_CORRECTION"


def secondary_processing_effect(
    opening_decision: OpeningAuditDecision | None,
    repair_decisions: list[RepairDecision],
    baseline_indicators: dict[str, object],
    final_indicators: dict[str, object],
) -> str:
    opening_accepted = bool(opening_decision and opening_decision.accepted)
    repair_accepted = any(item.accepted for item in repair_decisions)
    if not opening_accepted and not repair_accepted:
        return "NO_MEASURABLE_GAIN"
    anomaly_change = (
        int(final_indicators["strong_suspicious_event_count"])
        - int(baseline_indicators["strong_suspicious_event_count"])
    )
    character_change = (
        int(final_indicators["text_characters"])
        - int(baseline_indicators["text_characters"])
    )
    anchor_change = (
        int(final_indicators["anchor_exact_hits"])
        - int(baseline_indicators["anchor_exact_hits"])
    )
    if anomaly_change < 0 or character_change >= 40 or anchor_change > 0:
        return "MEASURABLE_GAIN"
    return "CHANGE_ACCEPTED_WITHOUT_MEASURABLE_GLOBAL_GAIN"


def levenshtein_distance(left: list[str], right: list[str]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_value in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_value != right_value),
                )
            )
        previous = current
    return previous[-1]


def calculate_reference_metrics(
    reference_text: str,
    baseline_text: str,
    final_text: str,
) -> dict[str, object]:
    def character_error_rate(hypothesis: str) -> float:
        reference_units = list(re.sub(r"\s+", "", reference_text))
        hypothesis_units = list(re.sub(r"\s+", "", hypothesis))
        return levenshtein_distance(reference_units, hypothesis_units) / max(
            len(reference_units), 1
        )

    reference_english = [word.lower() for word in english_words(reference_text)]

    def english_word_error_rate(hypothesis: str) -> float | None:
        if not reference_english:
            return None
        hypothesis_english = [word.lower() for word in english_words(hypothesis)]
        return levenshtein_distance(reference_english, hypothesis_english) / len(
            reference_english
        )

    baseline_cer = character_error_rate(baseline_text)
    final_cer = character_error_rate(final_text)
    baseline_wer = english_word_error_rate(baseline_text)
    final_wer = english_word_error_rate(final_text)
    return {
        "status": "MEASURED",
        "baseline_cer": baseline_cer,
        "final_cer": final_cer,
        "cer_change": final_cer - baseline_cer,
        "baseline_english_wer": baseline_wer,
        "final_english_wer": final_wer,
        "english_wer_change": (
            None
            if baseline_wer is None or final_wer is None
            else final_wer - baseline_wer
        ),
    }


def write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".part")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json_atomically(path: Path, data: dict[str, object]) -> None:
    write_text_atomically(
        path,
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def opening_decision_to_dict(
    decision: OpeningAuditDecision | None,
) -> dict[str, object]:
    if decision is None:
        return {"status": "NOT_RUN"}
    return {
        "status": "RUN",
        "accepted": decision.accepted,
        "decision_reason": decision.reason,
        "core_end": round(decision.core_end, 3),
        "coverage_ratio": round(decision.coverage_ratio, 6),
        "token_recall": round(decision.token_recall, 6),
        "baseline_score": round(decision.baseline_score, 6),
        "audit_score": round(decision.audit_score, 6),
        "baseline_anchor_hits": decision.baseline_anchor_hits,
        "audit_anchor_hits": decision.audit_anchor_hits,
        "baseline_section_hits": decision.baseline_section_hits,
        "audit_section_hits": decision.audit_section_hits,
        "baseline_text": decision.baseline_text,
        "audit_text": decision.audit_text,
    }


def section_decision_to_dict(
    decision: SectionAuditDecision | None,
) -> dict[str, object]:
    if decision is None:
        return {'status': 'NOT_RUN'}
    return {
        'status': 'RUN',
        'accepted': decision.accepted,
        'decision_reason': decision.reason,
        'section_name': decision.section_name,
        'core_start': round(decision.core_start, 3),
        'core_end': round(decision.core_end, 3),
        'coverage_ratio': round(decision.coverage_ratio, 6),
        'token_recall': round(decision.token_recall, 6),
        'baseline_score': round(decision.baseline_score, 6),
        'audit_score': round(decision.audit_score, 6),
        'baseline_anchor_hits': decision.baseline_anchor_hits,
        'audit_anchor_hits': decision.audit_anchor_hits,
        'baseline_section_hits': decision.baseline_section_hits,
        'audit_section_hits': decision.audit_section_hits,
        'baseline_english_candidates': decision.baseline_english_candidates,
        'audit_english_candidates': decision.audit_english_candidates,
        'baseline_text': decision.baseline_text,
        'audit_text': decision.audit_text,
    }


def repair_decision_to_dict(decision: RepairDecision) -> dict[str, object]:
    return {
        "window_index": decision.window_index,
        "accepted": decision.accepted,
        "decision_reason": decision.reason,
        "coverage_ratio": round(decision.coverage_ratio, 6),
        "token_recall": round(decision.token_recall, 6),
        "baseline_score": round(decision.baseline_score, 6),
        "repair_score": round(decision.repair_score, 6),
        "baseline_suspicious_events": decision.baseline_suspicious_events,
        "repair_suspicious_events": decision.repair_suspicious_events,
        "core_start": round(decision.core_start, 3),
        "core_end": round(decision.core_end, 3),
        "slice_start": round(decision.slice_start, 3),
        "slice_end": round(decision.slice_end, 3),
        "reasons": list(decision.reasons),
        "baseline_text": decision.baseline_text,
        "repair_text": decision.repair_text,
    }


def run_child() -> int:
    timer = StepTimer()
    overall_result = "FAILED"

    arguments = parse_arguments()
    script_directory = Path(__file__).resolve().parent
    run_id = os.environ.get("TRANSCRIBE_RUN_ID") or datetime.now(JST).strftime(
        "%Y%m%d_%H%M%S"
    )
    input_file = resolve_path(script_directory, arguments.input)
    output_directory = resolve_path(script_directory, arguments.output_dir)
    output_paths = make_output_paths(output_directory, run_id)
    reference_file = (
        resolve_path(script_directory, arguments.reference)
        if arguments.reference
        else None
    )

    with TeeContext(output_paths.log):
        try:
            print("RUN_START")
            print(f"RUN_ID={run_id}")
            print(
                "BOOTSTRAP_PYTHON_FROM="
                + os.environ.get("TRANSCRIBE_BOOTSTRAP_FROM", str(sys.executable))
            )
            print(
                "BOOTSTRAP_PYTHON_TO="
                + os.environ.get("TRANSCRIBE_BOOTSTRAP_TO", str(sys.executable))
            )
            print("OUTPUT_DIRECTORY_POLICY=TRANSCRIPT_LOG_METRICS_SAME_DIRECTORY")
            print(f"TRANSCRIPT_FILE={output_paths.transcript}")
            print(f"LOG_FILE={output_paths.log}")
            print(f"METRICS_FILE={output_paths.metrics}")

            timer.start("check_and_update_software")
            software_metrics = check_and_update_runtime_packages(script_directory)
            timer.finish()

            timer.start("import_transcription_libraries")
            import ctranslate2
            import faster_whisper
            from faster_whisper import BatchedInferencePipeline, WhisperModel
            from faster_whisper.audio import decode_audio
            timer.finish()

            timer.start("check_model_revisions")
            print("MODEL_VERSION_CHECK_START")
            primary_model_information = check_model(
                script_directory,
                repository=PRIMARY_MODEL_REPOSITORY,
                directory_name=PRIMARY_MODEL_DIRECTORY_NAME,
                label="PRIMARY",
                download_now=True,
            )
            repair_model_information = check_model(
                script_directory,
                repository=REPAIR_MODEL_REPOSITORY,
                directory_name=REPAIR_MODEL_DIRECTORY_NAME,
                label="REPAIR",
                download_now=False,
            )
            print("REPAIR_MODEL_DOWNLOAD_POLICY=DOWNLOAD_ONLY_IF_STRONG_ANOMALY_EXISTS")
            print("MODEL_VERSION_CHECK_RESULT=PASS")
            timer.finish()

            timer.start("validate_environment")
            validate_environment(
                input_file,
                arguments.batch_size,
                arguments.opening_audit_seconds,
                arguments.max_repair_windows,
                ctranslate2,
            )
            if reference_file is not None and not reference_file.is_file():
                raise FileNotFoundError(f"Reference file was not found: {reference_file}")
            timer.finish()

            print("TRANSCRIPTION_CONFIGURATION")
            print("TRANSCRIPTION_MODE=BATCHED_TURBO_GUARDED_OPENING_AND_SECTION_AUDIT")
            print("PRIMARY_POLICY=FAST_BATCHED_VAD_FULL_PROGRAM_PASS")
            print("OPENING_AUDIT_POLICY=NO_VAD_ADD_COVERAGE_ONLY_WITH_CONTENT_GUARDS")
            print("SECTION_AUDIT_POLICY=DENSE_OVERLAPPED_FORCED_ENGLISH_CROSS_WINDOW_CONSENSUS")
            print("SECTION_MERGE_POLICY=TIMESTAMPED_ADD_ONLY_NEVER_REPLACE_BASELINE")
            print("REPEATED_ENGLISH_POLICY=WITHIN_RECORDING_MAJORITY_CONSENSUS_ONLY")
            print("COMPLEMENTARY_LANGUAGE_RECOVERY=DISABLED_NO_MEASURABLE_GAIN")
            print("LARGE_V3_POLICY=RARE_STRONG_ANOMALIES_ONLY")
            print("EDUCATIONAL_FORMATTING_APPLIED=NO")
            print("CONTENT_REMOVAL_APPLIED=NO")
            print("PARAPHRASING_APPLIED=NO")
            print("TRANSLATION_APPLIED=NO")
            print(f"PYTHON={sys.executable}")
            print(f"FASTER_WHISPER={faster_whisper.__version__}")
            print(f"CTRANSLATE2={ctranslate2.__version__}")
            print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
            print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
            print(f"LANGUAGE={arguments.language}")
            print(f"BATCH_SIZE_REQUESTED={arguments.batch_size}")
            print(f"OPENING_AUDIT_SECONDS={arguments.opening_audit_seconds:.1f}")
            print(f"MAX_REPAIR_WINDOWS={arguments.max_repair_windows}")
            print("PRIMARY_MODEL=large-v3-turbo")
            print("OPENING_AUDIT_MODEL=large-v3-turbo")
            print("REPAIR_MODEL=large-v3")
            print(f"INPUT={input_file}")
            print(f"REFERENCE={reference_file or 'NONE'}")

            timer.start("decode_audio")
            audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
            audio_seconds = len(audio) / SAMPLE_RATE
            audio_signal = analyze_audio_signal(audio)
            timer.finish()
            print(f"AUDIO_SECONDS={audio_seconds:.3f}")
            print(f"AUDIO_SIGNAL_QUALITY={audio_signal['quality_status']}")
            print(f"AUDIO_SIGNAL_RMS_DBFS={audio_signal['rms_dbfs']}")
            print(f"AUDIO_SIGNAL_PEAK_DBFS={audio_signal['peak_dbfs']}")
            print(
                "AUDIO_SIGNAL_FLAGS="
                + json.dumps(audio_signal["quality_flags"], ensure_ascii=False)
            )

            timer.start("load_primary_model")
            primary_model = load_model(
                WhisperModel,
                Path(str(primary_model_information["directory"])),
                "turbo",
            )
            timer.finish()

            requested_language = None if arguments.language == "auto" else arguments.language

            timer.start("primary_batched_transcription")
            baseline_segments, batch_size_used, detected_language = (
                transcribe_batched_baseline(
                    primary_model,
                    BatchedInferencePipeline,
                    audio,
                    requested_language,
                    arguments.batch_size,
                )
            )
            baseline_text = format_transcript(baseline_segments)
            timer.finish()

            timer.start("discover_repeated_content")
            anchors = discover_repeated_anchors(baseline_segments)
            baseline_indicators = transcript_indicators(
                baseline_text,
                baseline_segments,
                anchors,
            )
            timer.finish()

            print(f"PRIMARY_BATCH_SIZE_USED={batch_size_used}")
            print(f"DETECTED_LANGUAGE={detected_language}")
            print(f"BASELINE_SEGMENT_COUNT={len(baseline_segments)}")
            print(f"BASELINE_TEXT_CHARACTERS={len(baseline_text)}")
            print(f"REPEATED_PHRASE_ANCHORS={json.dumps(anchors, ensure_ascii=False)}")

            timer.start("opening_no_vad_audit")
            opening_decision: OpeningAuditDecision | None = None
            post_opening_segments = list(baseline_segments)
            core_end = opening_core_end(
                baseline_segments,
                arguments.opening_audit_seconds,
                audio_seconds,
            )
            if core_end > 0:
                audit_slice_end = min(
                    audio_seconds,
                    core_end + OPENING_AUDIT_PADDING_SECONDS,
                )
                opening_audit_segments = transcribe_slice(
                    primary_model,
                    audio,
                    start=0.0,
                    end=audit_slice_end,
                    language=requested_language,
                    source="turbo_opening_no_vad",
                    beam_size=5,
                )
                opening_audit_segments = segments_before(
                    opening_audit_segments,
                    core_end,
                )
                baseline_opening_segments = segments_before(
                    baseline_segments,
                    core_end,
                )
                opening_decision = decide_opening_audit(
                    baseline_opening_segments,
                    opening_audit_segments,
                    anchors,
                    core_end,
                )
                print(
                    "OPENING_AUDIT_DECISION "
                    f"ACCEPTED={'YES' if opening_decision.accepted else 'NO'} "
                    f"CORE_END={opening_decision.core_end:.3f} "
                    f"COVERAGE_RATIO={opening_decision.coverage_ratio:.3f} "
                    f"TOKEN_RECALL={opening_decision.token_recall:.3f} "
                    f"BASELINE_SCORE={opening_decision.baseline_score:.3f} "
                    f"AUDIT_SCORE={opening_decision.audit_score:.3f} "
                    f"REASON={opening_decision.reason}"
                )
                if opening_decision.accepted:
                    post_opening_segments = apply_opening_audit(
                        baseline_segments,
                        opening_audit_segments,
                        core_end,
                    )
            else:
                print("OPENING_AUDIT_DECISION=NOT_RUN")
            timer.finish()

            timer.start("essential_section_dense_english_audit")
            section_decision: SectionAuditDecision | None = None
            section_recovered_examples: list[dict[str, object]] = []
            section_recovery_evidence: list[dict[str, object]] = []
            section_dense_seconds = 0.0
            section_dense_windows = 0
            post_section_segments = list(post_opening_segments)
            section_interval = find_section_interval(
                post_opening_segments,
                "Essential Expressions",
                "Practical Usage",
            )
            if section_interval is not None:
                section_start, section_end = section_interval
                baseline_section_segments = segments_between(
                    post_opening_segments,
                    section_start,
                    section_end,
                )
                baseline_section_text = (
                    format_transcript(baseline_section_segments)
                    if baseline_section_segments
                    else ""
                )
                if len(split_english_candidates(baseline_section_text)) < 8:
                    dense_started = time.perf_counter()
                    dense_segments, section_dense_windows = (
                        transcribe_chunked_english_section(
                            primary_model,
                            audio,
                            start=max(0.0, section_start - SECTION_AUDIT_PADDING_SECONDS),
                            end=min(audio_seconds, section_end + SECTION_AUDIT_PADDING_SECONDS),
                        )
                    )
                    section_dense_seconds = time.perf_counter() - dense_started
                    dense_deduplicated = deduplicate_segments(dense_segments)
                    section_decision = decide_section_audit(
                        "Essential Expressions",
                        baseline_section_segments,
                        dense_deduplicated,
                        anchors,
                        section_start,
                        section_end,
                    )
                    (
                        section_recovered_examples,
                        section_recovery_evidence,
                    ) = recover_cross_window_section_examples(
                        baseline_section_text,
                        dense_segments,
                    )
                    section_decision.accepted = bool(section_recovered_examples)
                    section_decision.reason = (
                        "timestamped_cross_window_examples_recovered"
                        if section_recovered_examples
                        else "no_safe_cross_window_examples"
                    )
                    recovered_segments = recovered_examples_to_segments(
                        section_recovered_examples
                    )
                    if recovered_segments:
                        post_section_segments = deduplicate_segments(
                            [*post_opening_segments, *recovered_segments]
                        )
                    print(
                        "SECTION_AUDIT_DECISION "
                        f"ACCEPTED={'YES' if section_decision.accepted else 'NO'} "
                        f"SECTION={section_decision.section_name} "
                        f"CORE_START={section_decision.core_start:.3f} "
                        f"CORE_END={section_decision.core_end:.3f} "
                        f"BASELINE_EXAMPLES={section_decision.baseline_english_candidates} "
                        f"RECOVERED_EXAMPLES={len(section_recovered_examples)} "
                        f"DENSE_WINDOWS={section_dense_windows} "
                        f"DENSE_SECONDS={section_dense_seconds:.3f} "
                        f"REASON={section_decision.reason}"
                    )
                    for example_index, item in enumerate(
                        section_recovered_examples,
                        start=1,
                    ):
                        event_starts = [
                            round(float(event["start"]), 3)
                            for event in item.get("events", [])
                        ]
                        print(
                            f"SECTION_RECOVERED_EXAMPLE_{example_index:02d}="
                            f"{item['text']} | OCCURRENCES={item['occurrences']} "
                            f"| STARTS={event_starts} | EVIDENCE={item['evidence']}"
                        )
                else:
                    print("SECTION_AUDIT_DECISION=SKIPPED_SUFFICIENT_ENGLISH_COVERAGE")
            else:
                print("SECTION_AUDIT_DECISION=NOT_APPLICABLE")
            timer.finish()

            del primary_model
            gc.collect()

            timer.start("detect_strong_repair_windows")
            strong_events = detect_strong_events(post_section_segments)
            repair_windows = build_repair_windows(
                strong_events,
                post_section_segments,
                audio_seconds,
                arguments.max_repair_windows,
            )
            timer.finish()

            print(f"STRONG_SUSPICIOUS_EVENT_COUNT={len(strong_events)}")
            print(
                "STRONG_SUSPICIOUS_EVENT_REASONS="
                + json.dumps(
                    Counter(event.reason for event in strong_events),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            print(f"STRONG_REPAIR_WINDOW_COUNT={len(repair_windows)}")

            timer.start("selective_large_v3_repairs")
            final_segments = list(post_section_segments)
            repair_decisions: list[RepairDecision] = []
            repair_model_updated = False
            if repair_windows:
                repair_model_information = ensure_repair_model(
                    script_directory,
                    repair_model_information,
                )
                repair_model_updated = bool(repair_model_information["updated"])
                repair_model = load_model(
                    WhisperModel,
                    Path(str(repair_model_information["directory"])),
                    "large_v3",
                )
                for window in repair_windows:
                    print(
                        f"SELECTIVE_REPAIR_START INDEX={window.index:02d} "
                        f"SLICE_START={window.slice_start:.3f} "
                        f"SLICE_END={window.slice_end:.3f}"
                    )
                    baseline_core = segments_in_core(
                        final_segments,
                        window.core_start,
                        window.core_end,
                    )
                    repair_slice = transcribe_slice(
                        repair_model,
                        audio,
                        start=window.slice_start,
                        end=window.slice_end,
                        language=requested_language,
                        source="large_v3",
                        beam_size=5,
                    )
                    repair_core = segments_in_core(
                        repair_slice,
                        window.core_start,
                        window.core_end,
                    )
                    decision = decide_repair(
                        window,
                        baseline_core,
                        repair_core,
                    )
                    repair_decisions.append(decision)
                    print(
                        f"SELECTIVE_REPAIR_DECISION INDEX={window.index:02d} "
                        f"ACCEPTED={'YES' if decision.accepted else 'NO'} "
                        f"COVERAGE_RATIO={decision.coverage_ratio:.3f} "
                        f"TOKEN_RECALL={decision.token_recall:.3f} "
                        f"BASELINE_SCORE={decision.baseline_score:.3f} "
                        f"REPAIR_SCORE={decision.repair_score:.3f} "
                        f"REASON={decision.reason}"
                    )
                    if decision.accepted:
                        final_segments = apply_repair(
                            final_segments,
                            repair_core,
                            window,
                        )
                del repair_model
                gc.collect()
            timer.finish()

            timer.start("assemble_final_transcript")
            final_segments = deduplicate_segments(final_segments)
            final_text = format_transcript(final_segments)
            final_text, consensus_corrections, unresolved_variant_clusters = (
                correct_repeated_english_variants(final_text)
            )
            print(f"CONSENSUS_CORRECTIONS_APPLIED={len(consensus_corrections)}")
            print(
                f"SECTION_RECOVERED_EXAMPLES_INSERTED="
                f"{len(section_recovered_examples)}"
            )
            print(f"REPEATED_VARIANT_CLUSTERS_UNRESOLVED={unresolved_variant_clusters}")
            final_indicators = transcript_indicators(
                final_text,
                final_segments,
                anchors,
            )
            review_status = automatic_review_status(
                final_indicators,
                audio_seconds,
            )
            effect = secondary_processing_effect(
                opening_decision,
                repair_decisions,
                baseline_indicators,
                final_indicators,
            )
            if consensus_corrections and section_recovered_examples:
                effect = "MEASURABLE_TIMESTAMPED_EXAMPLE_RECOVERY_AND_CONSENSUS_CORRECTION"
            elif consensus_corrections:
                effect = "MEASURABLE_CONSENSUS_CORRECTION"
            elif section_recovered_examples:
                effect = "MEASURABLE_TIMESTAMPED_EXAMPLE_RECOVERY"
            elif effect == "MEASURABLE_GAIN":
                effect = "COVERAGE_CHANGE_NOT_ACCURACY_MEASUREMENT"
            timer.finish()

            timer.start("calculate_reference_metrics")
            if reference_file is None:
                reference_metrics: dict[str, object] = {"status": "NOT_MEASURED"}
            else:
                reference_text = reference_file.read_text(encoding="utf-8")
                reference_metrics = calculate_reference_metrics(
                    reference_text,
                    baseline_text,
                    final_text,
                )
            timer.finish()

            timer.start("write_transcript")
            write_text_atomically(output_paths.transcript, final_text)
            timer.finish()

            primary_seconds = timer.seconds_for("primary_batched_transcription")
            opening_seconds = timer.seconds_for("opening_no_vad_audit")
            section_seconds = timer.seconds_for("essential_section_dense_english_audit")
            repair_seconds = timer.seconds_for("selective_large_v3_repairs")
            accepted_repairs = sum(item.accepted for item in repair_decisions)
            opening_accepted = bool(opening_decision and opening_decision.accepted)

            metrics = {
                "run_id": run_id,
                "created_at": datetime.now(JST).isoformat(),
                "input": str(input_file),
                "output_files": {
                    "transcript": str(output_paths.transcript),
                    "log": str(output_paths.log),
                    "metrics": str(output_paths.metrics),
                },
                "configuration": {
                    "mode": "batched_turbo_opening_audit_dense_cross_window_example_recovery",
                    "language": arguments.language,
                    "batch_size_requested": arguments.batch_size,
                    "batch_size_used": batch_size_used,
                    "opening_audit_seconds": arguments.opening_audit_seconds,
                    "max_repair_windows": arguments.max_repair_windows,
                    "complementary_language_recovery": "disabled_no_measurable_gain",
                },
                "software": software_metrics,
                "models": {
                    "primary": primary_model_information,
                    "repair": repair_model_information,
                    "repair_model_updated_during_run": repair_model_updated,
                },
                "audio_seconds": audio_seconds,
                "audio_signal": audio_signal,
                "detected_language": detected_language,
                "repeated_phrase_anchors": anchors,
                "baseline": baseline_indicators,
                "opening_audit": opening_decision_to_dict(opening_decision),
                "essential_section_audit": section_decision_to_dict(section_decision),
                "essential_section_recovery": {
                    "forced_language": "en",
                    "recovered_example_count": len(section_recovered_examples),
                    "recovered_examples": section_recovered_examples,
                    "candidate_evidence": section_recovery_evidence,
                    "dense_window_count": section_dense_windows,
                    "dense_seconds": section_dense_seconds,
                    "window_seconds": SECTION_CHUNK_SECONDS,
                    "window_overlap_seconds": SECTION_CHUNK_OVERLAP_SECONDS,
                    "event_cluster_seconds": SECTION_EVENT_CLUSTER_SECONDS,
                    "minimum_cross_window_support": SECTION_CROSS_WINDOW_MIN_SUPPORT,
                    "merge_policy": "timestamped_add_only_cross_window_consensus_never_replace_baseline",
                },
                "repeated_english_consensus": {
                    "correction_count": len(consensus_corrections),
                    "corrections": consensus_corrections,
                    "unresolved_variant_cluster_count": unresolved_variant_clusters,
                    "source": "same_recording_repeated_occurrences_only",
                },
                "strong_repair_detection": {
                    "event_count": len(strong_events),
                    "reason_counts": dict(
                        Counter(event.reason for event in strong_events)
                    ),
                    "window_count": len(repair_windows),
                    "windows": [
                        {
                            "index": window.index,
                            "core_start": round(window.core_start, 3),
                            "core_end": round(window.core_end, 3),
                            "slice_start": round(window.slice_start, 3),
                            "slice_end": round(window.slice_end, 3),
                            "reasons": list(window.reasons),
                        }
                        for window in repair_windows
                    ],
                },
                "large_v3_repair_decisions": [
                    repair_decision_to_dict(item)
                    for item in repair_decisions
                ],
                "final": final_indicators,
                "reference_accuracy": reference_metrics,
                "automatic_review": {
                    "status": review_status,
                    "scope": "STRUCTURAL_COVERAGE_AND_STRONG_ANOMALIES_ONLY",
                    "is_accuracy_measurement": False,
                },
                "secondary_processing": {
                    "effect": effect,
                    "opening_audit_accepted": opening_accepted,
                    "essential_section_audit_accepted": bool(
                        section_recovered_examples
                    ),
                    "essential_section_recovered_examples": len(
                        section_recovered_examples
                    ),
                    "consensus_corrections_applied": len(consensus_corrections),
                    "large_v3_repairs_accepted": accepted_repairs,
                    "text_character_change": (
                        int(final_indicators["text_characters"])
                        - int(baseline_indicators["text_characters"])
                    ),
                    "anchor_exact_hit_change": (
                        int(final_indicators["anchor_exact_hits"])
                        - int(baseline_indicators["anchor_exact_hits"])
                    ),
                    "strong_suspicious_event_change": (
                        int(final_indicators["strong_suspicious_event_count"])
                        - int(baseline_indicators["strong_suspicious_event_count"])
                    ),
                },
                "performance": {
                    "primary_seconds": primary_seconds,
                    "opening_audit_seconds": opening_seconds,
                    "essential_section_audit_seconds": section_seconds,
                    "essential_section_dense_asr_seconds": section_dense_seconds,
                    "essential_section_dense_windows": section_dense_windows,
                    "large_v3_repair_seconds": repair_seconds,
                    "secondary_seconds": opening_seconds + section_seconds + repair_seconds,
                    "total_seconds": timer.total_seconds,
                    "primary_realtime_factor": primary_seconds / audio_seconds,
                    "total_realtime_factor": timer.total_seconds / audio_seconds,
                    "audio_minutes_per_processing_minute": (
                        audio_seconds / max(timer.total_seconds, 0.001)
                    ),
                },
                "timings": timer.steps,
            }
            total_seconds = timer.total_seconds
            startup_seconds = sum(
                timer.seconds_for(name)
                for name in (
                    "check_and_update_software",
                    "import_transcription_libraries",
                    "check_model_revisions",
                    "validate_environment",
                    "decode_audio",
                    "load_primary_model",
                )
            )
            core_processing_seconds = (
                primary_seconds + opening_seconds + section_seconds + repair_seconds
            )
            performance_class = (
                "FAST"
                if total_seconds / audio_seconds <= 0.10
                else "MODERATE"
                if total_seconds / audio_seconds <= 0.20
                else "SLOW"
            )
            metrics["performance"]["startup_seconds"] = startup_seconds
            metrics["performance"]["core_processing_seconds"] = core_processing_seconds
            metrics["performance"]["performance_class"] = performance_class
            write_json_atomically(output_paths.metrics, metrics)

            print("TRANSCRIPTION_RESULT=PASS")
            print(f"OUTPUT_FILE={output_paths.transcript}")
            print(f"LOG_FILE={output_paths.log}")
            print(f"METRICS_FILE={output_paths.metrics}")
            print(f"PRIMARY_SECONDS={primary_seconds:.3f}")
            print(f"OPENING_AUDIT_SECONDS={opening_seconds:.3f}")
            print(f"ESSENTIAL_SECTION_AUDIT_SECONDS={section_seconds:.3f}")
            print(
                f"ESSENTIAL_SECTION_DENSE_ASR_SECONDS="
                f"{section_dense_seconds:.3f}"
            )
            print(f"ESSENTIAL_SECTION_DENSE_WINDOWS={section_dense_windows}")
            print(f"LARGE_V3_REPAIR_SECONDS={repair_seconds:.3f}")
            print(
                f"SECONDARY_SECONDS="
                f"{opening_seconds + section_seconds + repair_seconds:.3f}"
            )
            print(f"STARTUP_SECONDS={startup_seconds:.3f}")
            print(f"CORE_PROCESSING_SECONDS={core_processing_seconds:.3f}")
            print(f"TOTAL_SECONDS={total_seconds:.3f}")
            print(f"PERFORMANCE_CLASS={performance_class}")
            print(f"TOTAL_PROCESS_REALTIME_FACTOR={total_seconds / audio_seconds:.4f}")
            print(f"OPENING_AUDIT_ACCEPTED={'YES' if opening_accepted else 'NO'}")
            print(
                f"ESSENTIAL_SECTION_AUDIT_ACCEPTED="
                f"{'YES' if section_recovered_examples else 'NO'}"
            )
            print(f"LARGE_V3_WINDOWS_PROCESSED={len(repair_windows)}")
            print(f"LARGE_V3_REPAIRS_ACCEPTED={accepted_repairs}")
            print(f"BASELINE_TEXT_CHARACTERS={len(baseline_text)}")
            print(f"FINAL_TEXT_CHARACTERS={len(final_text)}")
            print(
                f"BASELINE_STRICT_SECTION_COUNT="
                f"{baseline_indicators['strict_section_count']}/4"
            )
            print(
                f"FINAL_STRICT_SECTION_COUNT="
                f"{final_indicators['strict_section_count']}/4"
            )
            print(
                f"BASELINE_ANCHOR_EXACT_HITS="
                f"{baseline_indicators['anchor_exact_hits']}"
            )
            print(
                f"FINAL_ANCHOR_EXACT_HITS="
                f"{final_indicators['anchor_exact_hits']}"
            )
            print(
                f"FINAL_STRONG_SUSPICIOUS_EVENTS="
                f"{final_indicators['strong_suspicious_event_count']}"
            )
            print(f"SECONDARY_PROCESSING_EFFECT={effect}")
            print(f"REFERENCE_ACCURACY={reference_metrics['status']}")
            print(f"AUTOMATIC_REVIEW_STATUS={review_status}")
            print("AUTOMATIC_REVIEW_STATUS_IS_ACCURACY_MEASUREMENT=NO")
            print("EDUCATIONAL_FORMATTING_APPLIED=NO")

            overall_result = "PASS"
            return 0

        except KeyboardInterrupt:
            timer.fail_current()
            print("TRANSCRIPTION_RESULT=CANCELLED", file=sys.stderr)
            overall_result = "CANCELLED"
            return 130
        except Exception as exception:
            timer.fail_current()
            print("TRANSCRIPTION_RESULT=FAILED", file=sys.stderr)
            print(f"ERROR={type(exception).__name__}: {exception}", file=sys.stderr)
            print(f"OUTPUT_FILE_NOT_CREATED={output_paths.transcript}", file=sys.stderr)
            overall_result = "FAILED"
            return 1
        finally:
            timer.print_summary(overall_result)


COVERAGE_WINDOW_SECONDS_V19 = 30.0
COVERAGE_WINDOW_OVERLAP_SECONDS_V19 = 5.0
COVERAGE_TIME_PADDING_SECONDS_V19 = 0.45
ACTIVE_FRAME_SECONDS_V19 = 0.20
ACTIVE_HOP_SECONDS_V19 = 0.10
ACTIVE_BRIDGE_GAP_SECONDS_V19 = 0.35
ACTIVE_MIN_INTERVAL_SECONDS_V19 = 0.40
UNRESOLVED_MIN_SECONDS_V19 = 0.45
UNRESOLVED_PADDING_SECONDS_V19 = 1.00


def classify_language_v19(text: str) -> str:
    latin = latin_character_count(text)
    japanese = japanese_character_count(text)
    english_word_count = len(english_words(text))
    if english_word_count >= 3 and latin >= max(12, japanese * 2):
        return "en"
    if japanese >= 8 and japanese >= latin:
        return "ja"
    if latin >= 10 and japanese >= 6:
        return "mixed"
    if english_word_count >= 2:
        return "en"
    if japanese >= 2:
        return "ja"
    return "unknown"


def segment_to_dict_v19(segment: SegmentRecord) -> dict[str, object]:
    return {
        "start": round(segment.start, 3),
        "end": round(segment.end, 3),
        "language": classify_language_v19(segment.text),
        "text": segment.text,
        "source": segment.source,
        "avg_logprob": round(segment.avg_logprob, 6),
        "compression_ratio": round(segment.compression_ratio, 6),
        "no_speech_prob": round(segment.no_speech_prob, 6),
    }


def transcribe_full_coverage_pass_v19(
    model: object,
    audio: object,
    *,
    audio_seconds: float,
    language: str | None,
) -> tuple[list[SegmentRecord], int]:
    records: list[SegmentRecord] = []
    cursor = 0.0
    window_count = 0
    while cursor < audio_seconds:
        window_end = min(audio_seconds, cursor + COVERAGE_WINDOW_SECONDS_V19)
        window_count += 1
        print(
            f"FULL_COVERAGE_WINDOW={window_count:03d} "
            f"START={cursor:.3f} END={window_end:.3f}"
        )
        records.extend(
            transcribe_slice(
                model,
                audio,
                start=cursor,
                end=window_end,
                language=language,
                source=f"turbo_full_no_vad_w{window_count:03d}",
                beam_size=3,
                multilingual=True,
            )
        )
        if window_end >= audio_seconds:
            break
        next_cursor = window_end - COVERAGE_WINDOW_OVERLAP_SECONDS_V19
        if next_cursor <= cursor:
            next_cursor = cursor + 1.0
        cursor = next_cursor
    return records, window_count


def nearby_segments_v19(
    segments: list[SegmentRecord],
    candidate: SegmentRecord,
    padding: float = COVERAGE_TIME_PADDING_SECONDS_V19,
) -> list[SegmentRecord]:
    return [
        segment
        for segment in segments
        if candidate.start <= segment.end + padding
        and candidate.end >= segment.start - padding
    ]


def merge_add_only_v19(
    baseline: list[SegmentRecord],
    candidates: list[SegmentRecord],
    *,
    stage: str,
) -> tuple[list[SegmentRecord], list[dict[str, object]], list[dict[str, object]]]:
    merged = list(baseline)
    additions: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []

    for original_candidate in sorted(candidates, key=lambda item: (item.start, item.end)):
        candidate = original_candidate
        candidate_text = clean_text(candidate.text)
        if not candidate_text or candidate.no_speech_prob >= 0.90:
            continue
        if detect_decoder_loop(candidate_text) or "�" in candidate_text:
            continue

        overlaps = nearby_segments_v19(merged, candidate)
        if not overlaps:
            merged.append(candidate)
            additions.append(
                {
                    "stage": stage,
                    "reason": "previously_uncovered_time",
                    **segment_to_dict_v19(candidate),
                }
            )
            continue

        overlap_text = " ".join(
            segment.text for segment in sorted(overlaps, key=lambda item: item.start)
        )
        candidate_normalized = normalize_for_comparison(candidate_text)
        overlap_normalized = normalize_for_comparison(overlap_text)
        if not candidate_normalized:
            continue
        if candidate_normalized in overlap_normalized:
            continue

        similarity = SequenceMatcher(
            None,
            candidate_normalized,
            overlap_normalized,
        ).ratio()
        candidate_language = classify_language_v19(candidate_text)
        candidate_english = len(english_words(candidate_text))
        overlap_english = len(english_words(overlap_text))
        candidate_japanese = japanese_character_count(candidate_text)
        overlap_japanese = japanese_character_count(overlap_text)

        reason: str | None = None
        if (
            candidate_language in {"en", "mixed"}
            and candidate_english >= 5
            and overlap_english < max(3, int(candidate_english * 0.45))
            and candidate.avg_logprob >= -0.80
        ):
            reason = "english_content_missing_from_parallel_pass"
        elif (
            candidate_language in {"ja", "mixed"}
            and candidate_japanese >= 12
            and overlap_japanese < max(6, int(candidate_japanese * 0.45))
            and candidate.avg_logprob >= -0.85
        ):
            reason = "japanese_content_missing_from_parallel_pass"
        elif (
            similarity < 0.36
            and candidate.avg_logprob >= -0.65
            and len(candidate_normalized) >= 18
        ):
            reason = "independent_parallel_decode_disagrees"

        if reason is None:
            continue

        if similarity < 0.28:
            language_label = candidate_language.upper()
            candidate = SegmentRecord(
                start=candidate.start,
                end=candidate.end,
                text=f"[{language_label}候補] {candidate_text}",
                avg_logprob=candidate.avg_logprob,
                compression_ratio=candidate.compression_ratio,
                no_speech_prob=candidate.no_speech_prob,
                source=candidate.source + "_parallel_candidate",
            )
            conflicts.append(
                {
                    "stage": stage,
                    "reason": reason,
                    "similarity": round(similarity, 6),
                    "existing_text": overlap_text,
                    "candidate_text": candidate_text,
                    "start": round(candidate.start, 3),
                    "end": round(candidate.end, 3),
                }
            )

        merged.append(candidate)
        additions.append(
            {
                "stage": stage,
                "reason": reason,
                "similarity": round(similarity, 6),
                **segment_to_dict_v19(candidate),
            }
        )

    return deduplicate_segments(merged), additions, conflicts


def merge_numeric_intervals_v19(
    intervals: list[tuple[float, float]],
    *,
    bridge_gap: float = 0.0,
) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1] + bridge_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def detect_active_audio_intervals_v19(
    audio: object,
) -> tuple[list[tuple[float, float]], dict[str, object]]:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    frame_size = max(1, int(SAMPLE_RATE * ACTIVE_FRAME_SECONDS_V19))
    hop_size = max(1, int(SAMPLE_RATE * ACTIVE_HOP_SECONDS_V19))
    if samples.size < frame_size:
        return [], {
            "threshold_dbfs": -50.0,
            "frame_count": 0,
            "scope": "ENERGY_BASED_ACTIVE_AUDIO_COVERAGE_DIAGNOSTIC",
        }

    starts = list(range(0, samples.size - frame_size + 1, hop_size))
    levels: list[float] = []
    for start_sample in starts:
        frame = samples[start_sample : start_sample + frame_size]
        rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
        levels.append(20.0 * math.log10(max(rms, 1e-12)))

    noise_floor = float(np.percentile(levels, 15))
    threshold = min(-34.0, max(-50.0, noise_floor + 11.0))
    active_frames = [
        (
            start_sample / SAMPLE_RATE,
            min(samples.size, start_sample + frame_size) / SAMPLE_RATE,
        )
        for start_sample, level in zip(starts, levels)
        if level >= threshold
    ]
    merged = merge_numeric_intervals_v19(
        active_frames,
        bridge_gap=ACTIVE_BRIDGE_GAP_SECONDS_V19,
    )
    merged = [
        (start, end)
        for start, end in merged
        if end - start >= ACTIVE_MIN_INTERVAL_SECONDS_V19
    ]
    return merged, {
        "threshold_dbfs": round(threshold, 3),
        "noise_floor_p15_dbfs": round(noise_floor, 3),
        "frame_count": len(levels),
        "active_interval_count": len(merged),
        "active_seconds": round(sum(end - start for start, end in merged), 3),
        "scope": "ENERGY_BASED_ACTIVE_AUDIO_DIAGNOSTIC_NOT_SPEECH_OR_ACCURACY_MEASUREMENT",
    }


def covered_intervals_v19(segments: list[SegmentRecord]) -> list[tuple[float, float]]:
    intervals = [
        (
            max(0.0, segment.start - COVERAGE_TIME_PADDING_SECONDS_V19),
            segment.end + COVERAGE_TIME_PADDING_SECONDS_V19,
        )
        for segment in segments
        if segment.text.strip()
        and not segment.text.startswith("[聞き取り不確実")
    ]
    return merge_numeric_intervals_v19(intervals, bridge_gap=0.15)


def subtract_intervals_v19(
    source_intervals: list[tuple[float, float]],
    covered_intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    uncovered: list[tuple[float, float]] = []
    covered = merge_numeric_intervals_v19(covered_intervals)
    for source_start, source_end in source_intervals:
        cursor = source_start
        for cover_start, cover_end in covered:
            if cover_end <= cursor:
                continue
            if cover_start >= source_end:
                break
            if cover_start > cursor:
                uncovered.append((cursor, min(cover_start, source_end)))
            cursor = max(cursor, cover_end)
            if cursor >= source_end:
                break
        if cursor < source_end:
            uncovered.append((cursor, source_end))
    return merge_numeric_intervals_v19(
        [
            (start, end)
            for start, end in uncovered
            if end - start >= UNRESOLVED_MIN_SECONDS_V19
        ],
        bridge_gap=0.20,
    )


def valid_forced_candidate_v19(segment: SegmentRecord, language: str) -> bool:
    if segment.avg_logprob < -1.10 or segment.no_speech_prob >= 0.85:
        return False
    if detect_decoder_loop(segment.text) or "�" in segment.text:
        return False
    if language == "en":
        return (
            len(english_words(segment.text)) >= 2
            and latin_character_count(segment.text) >= 8
        )
    return japanese_character_count(segment.text) >= 3


def recover_gaps_with_model_v19(
    model: object,
    audio: object,
    gaps: list[tuple[float, float]],
    *,
    model_label: str,
    include_auto: bool,
    audio_seconds: float,
) -> tuple[list[SegmentRecord], list[dict[str, object]]]:
    recovered: list[SegmentRecord] = []
    evidence: list[dict[str, object]] = []
    for index, (gap_start, gap_end) in enumerate(gaps, start=1):
        slice_start = max(0.0, gap_start - UNRESOLVED_PADDING_SECONDS_V19)
        slice_end = min(audio_seconds, gap_end + UNRESOLVED_PADDING_SECONDS_V19)
        candidates_by_language: dict[str, list[SegmentRecord]] = {}
        languages: list[str | None] = [None] if include_auto else []
        languages.extend(["ja", "en"])
        for language in languages:
            language_key = language or "auto"
            candidates = transcribe_slice(
                model,
                audio,
                start=slice_start,
                end=slice_end,
                language=language,
                source=f"{model_label}_gap_{index:03d}_{language_key}",
                beam_size=5 if model_label == "large_v3" else 3,
                multilingual=language is None,
            )
            core = segments_in_core(candidates, gap_start, gap_end)
            if language in {"ja", "en"}:
                core = [
                    segment
                    for segment in core
                    if valid_forced_candidate_v19(segment, language)
                ]
            else:
                core = [
                    segment
                    for segment in core
                    if segment.avg_logprob >= -1.10
                    and segment.no_speech_prob < 0.85
                    and not detect_decoder_loop(segment.text)
                    and "�" not in segment.text
                ]
            if core:
                candidates_by_language[language_key] = core

        flattened = [
            segment
            for candidate_group in candidates_by_language.values()
            for segment in candidate_group
        ]
        if not flattened:
            evidence.append(
                {
                    "gap_index": index,
                    "start": round(gap_start, 3),
                    "end": round(gap_end, 3),
                    "model": model_label,
                    "result": "NO_CANDIDATE",
                }
            )
            continue

        auto_candidates = candidates_by_language.get("auto", [])
        if auto_candidates:
            selected = auto_candidates
            selection_reason = "auto_candidate_available"
        else:
            ja_candidates = candidates_by_language.get("ja", [])
            en_candidates = candidates_by_language.get("en", [])
            if ja_candidates and en_candidates:
                ja_text = " ".join(item.text for item in ja_candidates)
                en_text = " ".join(item.text for item in en_candidates)
                similarity = SequenceMatcher(
                    None,
                    normalize_for_comparison(ja_text),
                    normalize_for_comparison(en_text),
                ).ratio()
                if similarity < 0.35:
                    combined = SegmentRecord(
                        start=min(item.start for item in [*ja_candidates, *en_candidates]),
                        end=max(item.end for item in [*ja_candidates, *en_candidates]),
                        text=f"[JA候補] {ja_text}\n[EN候補] {en_text}",
                        avg_logprob=max(
                            average_logprob(ja_candidates),
                            average_logprob(en_candidates),
                        ),
                        compression_ratio=0.0,
                        no_speech_prob=0.0,
                        source=f"{model_label}_bilingual_uncertain_{index:03d}",
                    )
                    selected = [combined]
                    selection_reason = "bilingual_candidates_preserved"
                else:
                    selected = (
                        ja_candidates
                        if average_logprob(ja_candidates) >= average_logprob(en_candidates)
                        else en_candidates
                    )
                    selection_reason = "higher_confidence_forced_candidate"
            else:
                selected = ja_candidates or en_candidates
                selection_reason = "single_language_candidate"

        recovered.extend(selected)
        evidence.append(
            {
                "gap_index": index,
                "start": round(gap_start, 3),
                "end": round(gap_end, 3),
                "model": model_label,
                "result": "CANDIDATE_RECOVERED",
                "selection_reason": selection_reason,
                "candidate_languages": sorted(candidates_by_language),
                "selected": [segment_to_dict_v19(item) for item in selected],
            }
        )
    return recovered, evidence


def timecode_v19(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000.0))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def uncertain_marker_segments_v19(
    gaps: list[tuple[float, float]],
) -> list[SegmentRecord]:
    return [
        SegmentRecord(
            start=start,
            end=end,
            text=(
                f"[聞き取り不確実 {timecode_v19(start)}-"
                f"{timecode_v19(end)}]"
            ),
            avg_logprob=-10.0,
            compression_ratio=0.0,
            no_speech_prob=0.0,
            source="unresolved_active_audio_interval",
        )
        for start, end in gaps
    ]


def load_benchmark_v19(
    script_directory: Path,
    input_file: Path,
) -> dict[str, object]:
    benchmark_file = script_directory / "TRANSCRIPTION_BENCHMARK.json"
    if not benchmark_file.is_file():
        return {"status": "NOT_CONFIGURED", "items": []}
    payload = json.loads(benchmark_file.read_text(encoding="utf-8"))
    expected_input = str(payload.get("input_basename", "")).strip()
    if expected_input and input_file.name.lower() != expected_input.lower():
        return {
            "status": "SKIPPED_INPUT_MISMATCH",
            "expected_input_basename": expected_input,
            "actual_input_basename": input_file.name,
            "items": [],
        }
    return {"status": "CONFIGURED", "items": list(payload.get("items", []))}


def evaluate_benchmark_v19(
    benchmark: dict[str, object],
    segments: list[SegmentRecord],
) -> dict[str, object]:
    if benchmark.get("status") != "CONFIGURED":
        return benchmark
    results: list[dict[str, object]] = []
    for item in benchmark.get("items", []):
        record = dict(item)
        phrase = str(record.get("phrase", "")).strip()
        start = float(record.get("start", 0.0))
        end = float(record.get("end", float("inf")))
        minimum = int(record.get("min_count", 1))
        selected = [
            segment
            for segment in segments
            if segment.end >= start and segment.start <= end
        ]
        selected_text = " ".join(segment.text for segment in selected)
        phrase_normalized = normalize_for_comparison(phrase)
        count = (
            normalize_for_comparison(selected_text).count(phrase_normalized)
            if phrase_normalized
            else 0
        )
        passed = count >= minimum
        results.append(
            {
                **record,
                "observed_count": count,
                "passed": passed,
            }
        )
        print(
            f"BENCHMARK_ITEM={'PASS' if passed else 'FAIL'} "
            f"COUNT={count} MIN={minimum} PHRASE={phrase}"
        )
    failed = [item for item in results if not bool(item["passed"])]
    return {
        "status": "PASS" if not failed else "FAIL",
        "item_count": len(results),
        "passed_count": len(results) - len(failed),
        "failed_count": len(failed),
        "items": results,
    }


def run_child_v19() -> int:
    timer = StepTimer()
    overall_result = "FAILED"
    arguments = parse_arguments()
    script_directory = Path(__file__).resolve().parent
    run_id = os.environ.get("TRANSCRIBE_RUN_ID") or datetime.now(JST).strftime(
        "%Y%m%d_%H%M%S"
    )
    input_file = resolve_path(script_directory, arguments.input)
    output_directory = resolve_path(script_directory, arguments.output_dir)
    output_paths = make_output_paths(output_directory, run_id)
    reference_file = (
        resolve_path(script_directory, arguments.reference)
        if arguments.reference
        else None
    )

    with TeeContext(output_paths.log):
        try:
            print("RUN_START")
            print(f"RUN_ID={run_id}")
            print("TRANSCRIPTION_TRANSACTION=1")
            print("TRANSCRIPTION_VERSION=19")
            print("OUTPUT_DIRECTORY_POLICY=TRANSCRIPT_LOG_METRICS_SAME_DIRECTORY")
            print(f"TRANSCRIPT_FILE={output_paths.transcript}")
            print(f"LOG_FILE={output_paths.log}")
            print(f"METRICS_FILE={output_paths.metrics}")

            timer.start("check_and_update_software")
            software_metrics = check_and_update_runtime_packages(script_directory)
            timer.finish()

            timer.start("import_transcription_libraries")
            import ctranslate2
            import faster_whisper
            from faster_whisper import BatchedInferencePipeline, WhisperModel
            from faster_whisper.audio import decode_audio
            timer.finish()

            timer.start("check_model_revisions")
            print("MODEL_VERSION_CHECK_START")
            primary_model_information = check_model(
                script_directory,
                repository=PRIMARY_MODEL_REPOSITORY,
                directory_name=PRIMARY_MODEL_DIRECTORY_NAME,
                label="PRIMARY",
                download_now=True,
            )
            repair_model_information = check_model(
                script_directory,
                repository=REPAIR_MODEL_REPOSITORY,
                directory_name=REPAIR_MODEL_DIRECTORY_NAME,
                label="REPAIR",
                download_now=False,
            )
            print("MODEL_VERSION_CHECK_RESULT=PASS")
            timer.finish()

            timer.start("validate_environment")
            validate_environment(
                input_file,
                arguments.batch_size,
                0.0,
                arguments.max_repair_windows,
                ctranslate2,
            )
            if reference_file is not None and not reference_file.is_file():
                raise FileNotFoundError(f"Reference file was not found: {reference_file}")
            timer.finish()

            print("TRANSCRIPTION_CONFIGURATION")
            print("TRANSCRIPTION_MODE=TRANSACTION1_DUAL_FULL_AUDIO_COVERAGE")
            print("PASS_A=FAST_BATCHED_VAD_FULL_AUDIO")
            print("PASS_B=FULL_AUDIO_NO_VAD_OVERLAPPED_WINDOWS")
            print("MERGE_POLICY=TIMESTAMP_ADD_ONLY_NO_SECTION_REPLACEMENT")
            print("LANGUAGE_POLICY=SEGMENT_LANGUAGE_RECORDED_JA_EN_MIXED_UNKNOWN")
            print("UNCERTAIN_POLICY=NEVER_SILENTLY_DROP_UNRESOLVED_ACTIVE_AUDIO")
            print("GLOBAL_CROSS_TIME_REPLACEMENT=DISABLED")
            print("EDUCATIONAL_FORMATTING_APPLIED=NO")
            print("PARAPHRASING_APPLIED=NO")
            print("TRANSLATION_APPLIED=NO")
            print(f"PYTHON={sys.executable}")
            print(f"FASTER_WHISPER={faster_whisper.__version__}")
            print(f"CTRANSLATE2={ctranslate2.__version__}")
            print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
            print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
            print(f"LANGUAGE={arguments.language}")
            print(f"BATCH_SIZE_REQUESTED={arguments.batch_size}")
            print(f"INPUT={input_file}")
            print(f"REFERENCE={reference_file or 'NONE'}")

            timer.start("decode_audio")
            audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
            audio_seconds = len(audio) / SAMPLE_RATE
            audio_signal = analyze_audio_signal(audio)
            active_intervals, active_diagnostics = detect_active_audio_intervals_v19(audio)
            timer.finish()
            print(f"AUDIO_SECONDS={audio_seconds:.3f}")
            print(f"AUDIO_SIGNAL_QUALITY={audio_signal['quality_status']}")
            print(f"ACTIVE_AUDIO_INTERVAL_COUNT={len(active_intervals)}")
            print(f"ACTIVE_AUDIO_SECONDS={active_diagnostics['active_seconds']}")
            print(f"ACTIVE_AUDIO_THRESHOLD_DBFS={active_diagnostics['threshold_dbfs']}")

            timer.start("load_primary_model")
            primary_model = load_model(
                WhisperModel,
                Path(str(primary_model_information["directory"])),
                "turbo",
            )
            timer.finish()
            requested_language = None if arguments.language == "auto" else arguments.language

            timer.start("pass_a_batched_transcription")
            baseline_segments, batch_size_used, detected_language = transcribe_batched_baseline(
                primary_model,
                BatchedInferencePipeline,
                audio,
                requested_language,
                arguments.batch_size,
            )
            baseline_text = format_transcript(baseline_segments)
            timer.finish()

            timer.start("pass_b_full_no_vad_coverage")
            coverage_segments, coverage_window_count = transcribe_full_coverage_pass_v19(
                primary_model,
                audio,
                audio_seconds=audio_seconds,
                language=requested_language,
            )
            timer.finish()

            timer.start("merge_pass_a_and_b")
            merged_segments, coverage_additions, coverage_conflicts = merge_add_only_v19(
                baseline_segments,
                coverage_segments,
                stage="full_audio_no_vad",
            )
            initial_gaps = subtract_intervals_v19(
                active_intervals,
                covered_intervals_v19(merged_segments),
            )
            timer.finish()
            print(f"PASS_A_SEGMENTS={len(baseline_segments)}")
            print(f"PASS_B_SEGMENTS={len(coverage_segments)}")
            print(f"PASS_B_WINDOWS={coverage_window_count}")
            print(f"PASS_B_ADDITIONS={len(coverage_additions)}")
            print(f"PASS_B_CONFLICTS_PRESERVED={len(coverage_conflicts)}")
            print(f"UNCOVERED_AFTER_DUAL_PASS={len(initial_gaps)}")

            timer.start("turbo_gap_language_recovery")
            turbo_gap_segments, turbo_gap_evidence = recover_gaps_with_model_v19(
                primary_model,
                audio,
                initial_gaps,
                model_label="turbo",
                include_auto=False,
                audio_seconds=audio_seconds,
            )
            merged_segments, turbo_additions, turbo_conflicts = merge_add_only_v19(
                merged_segments,
                turbo_gap_segments,
                stage="turbo_gap_language_recovery",
            )
            gaps_after_turbo = subtract_intervals_v19(
                active_intervals,
                covered_intervals_v19(merged_segments),
            )
            timer.finish()
            print(f"TURBO_GAP_CANDIDATES={len(turbo_gap_segments)}")
            print(f"TURBO_GAP_ADDITIONS={len(turbo_additions)}")
            print(f"UNCOVERED_AFTER_TURBO_GAP_RECOVERY={len(gaps_after_turbo)}")

            del primary_model
            gc.collect()

            timer.start("large_v3_unresolved_recovery")
            large_gap_segments: list[SegmentRecord] = []
            large_gap_evidence: list[dict[str, object]] = []
            large_additions: list[dict[str, object]] = []
            large_conflicts: list[dict[str, object]] = []
            repair_model_updated = False
            if gaps_after_turbo:
                repair_model_information = ensure_repair_model(
                    script_directory,
                    repair_model_information,
                )
                repair_model_updated = bool(repair_model_information["updated"])
                repair_model = load_model(
                    WhisperModel,
                    Path(str(repair_model_information["directory"])),
                    "large_v3",
                )
                large_gap_segments, large_gap_evidence = recover_gaps_with_model_v19(
                    repair_model,
                    audio,
                    gaps_after_turbo,
                    model_label="large_v3",
                    include_auto=True,
                    audio_seconds=audio_seconds,
                )
                merged_segments, large_additions, large_conflicts = merge_add_only_v19(
                    merged_segments,
                    large_gap_segments,
                    stage="large_v3_unresolved_recovery",
                )
                del repair_model
                gc.collect()
            final_gaps = subtract_intervals_v19(
                active_intervals,
                covered_intervals_v19(merged_segments),
            )
            timer.finish()
            print(f"LARGE_V3_GAP_CANDIDATES={len(large_gap_segments)}")
            print(f"LARGE_V3_GAP_ADDITIONS={len(large_additions)}")
            print(f"UNCOVERED_AFTER_LARGE_V3={len(final_gaps)}")

            timer.start("assemble_transaction1_transcript")
            uncertainty_segments = uncertain_marker_segments_v19(final_gaps)
            final_segments = deduplicate_segments([*merged_segments, *uncertainty_segments])
            final_text = format_transcript(final_segments)
            anchors = discover_repeated_anchors(final_segments)
            baseline_indicators = transcript_indicators(
                baseline_text,
                baseline_segments,
                discover_repeated_anchors(baseline_segments),
            )
            final_indicators = transcript_indicators(final_text, final_segments, anchors)
            language_counts = dict(
                Counter(classify_language_v19(segment.text) for segment in final_segments)
            )
            timer.finish()

            timer.start("evaluate_benchmark")
            benchmark_configuration = load_benchmark_v19(script_directory, input_file)
            benchmark_results = evaluate_benchmark_v19(
                benchmark_configuration,
                final_segments,
            )
            timer.finish()

            if benchmark_results.get("status") == "FAIL":
                coverage_status = "INCOMPLETE"
            elif final_gaps:
                coverage_status = "UNCERTAIN_INTERVALS_PRESENT"
            else:
                coverage_status = "COMPLETE"

            timer.start("calculate_reference_metrics")
            if reference_file is None:
                reference_metrics: dict[str, object] = {"status": "NOT_MEASURED"}
            else:
                reference_metrics = calculate_reference_metrics(
                    reference_file.read_text(encoding="utf-8"),
                    baseline_text,
                    final_text,
                )
            timer.finish()

            timer.start("write_transcript")
            write_text_atomically(output_paths.transcript, final_text)
            timer.finish()

            pass_a_seconds = timer.seconds_for("pass_a_batched_transcription")
            pass_b_seconds = timer.seconds_for("pass_b_full_no_vad_coverage")
            turbo_gap_seconds = timer.seconds_for("turbo_gap_language_recovery")
            large_gap_seconds = timer.seconds_for("large_v3_unresolved_recovery")
            total_seconds = timer.total_seconds
            metrics = {
                "run_id": run_id,
                "created_at": datetime.now(JST).isoformat(),
                "transaction": 1,
                "version": 19,
                "input": str(input_file),
                "output_files": {
                    "transcript": str(output_paths.transcript),
                    "log": str(output_paths.log),
                    "metrics": str(output_paths.metrics),
                },
                "configuration": {
                    "mode": "dual_full_audio_coverage_timestamp_add_only",
                    "pass_a": "batched_vad",
                    "pass_b": "no_vad_overlapped_full_audio",
                    "coverage_window_seconds": COVERAGE_WINDOW_SECONDS_V19,
                    "coverage_overlap_seconds": COVERAGE_WINDOW_OVERLAP_SECONDS_V19,
                    "global_cross_time_replacement": False,
                    "educational_formatting": False,
                },
                "software": software_metrics,
                "models": {
                    "primary": primary_model_information,
                    "repair": repair_model_information,
                    "repair_model_updated_during_run": repair_model_updated,
                },
                "audio_seconds": audio_seconds,
                "audio_signal": audio_signal,
                "active_audio_detection": active_diagnostics,
                "detected_language": detected_language,
                "pass_a": {
                    "batch_size_used": batch_size_used,
                    "segment_count": len(baseline_segments),
                    "text_characters": len(baseline_text),
                    "indicators": baseline_indicators,
                },
                "pass_b": {
                    "window_count": coverage_window_count,
                    "segment_count": len(coverage_segments),
                    "additions": coverage_additions,
                    "conflicts_preserved": coverage_conflicts,
                },
                "coverage": {
                    "status": coverage_status,
                    "status_is_accuracy_measurement": False,
                    "active_interval_count": len(active_intervals),
                    "active_seconds": sum(end - start for start, end in active_intervals),
                    "initial_uncovered_interval_count": len(initial_gaps),
                    "after_turbo_uncovered_interval_count": len(gaps_after_turbo),
                    "final_uncovered_interval_count": len(final_gaps),
                    "final_uncovered_seconds": sum(end - start for start, end in final_gaps),
                    "final_uncovered_intervals": [
                        {"start": round(start, 3), "end": round(end, 3)}
                        for start, end in final_gaps
                    ],
                    "uncertain_markers_written": len(uncertainty_segments),
                },
                "turbo_gap_recovery": {
                    "evidence": turbo_gap_evidence,
                    "additions": turbo_additions,
                    "conflicts_preserved": turbo_conflicts,
                },
                "large_v3_gap_recovery": {
                    "evidence": large_gap_evidence,
                    "additions": large_additions,
                    "conflicts_preserved": large_conflicts,
                },
                "benchmark": benchmark_results,
                "final": {
                    **final_indicators,
                    "language_segment_counts": language_counts,
                    "segments": [segment_to_dict_v19(item) for item in final_segments],
                },
                "reference_accuracy": reference_metrics,
                "performance": {
                    "pass_a_seconds": pass_a_seconds,
                    "pass_b_seconds": pass_b_seconds,
                    "turbo_gap_recovery_seconds": turbo_gap_seconds,
                    "large_v3_gap_recovery_seconds": large_gap_seconds,
                    "total_seconds": total_seconds,
                    "total_realtime_factor": total_seconds / max(audio_seconds, 0.001),
                    "audio_minutes_per_processing_minute": audio_seconds / max(total_seconds, 0.001),
                },
                "timings": timer.steps,
            }
            write_json_atomically(output_paths.metrics, metrics)

            print("TRANSCRIPTION_RESULT=PASS")
            print(f"OUTPUT_FILE={output_paths.transcript}")
            print(f"LOG_FILE={output_paths.log}")
            print(f"METRICS_FILE={output_paths.metrics}")
            print(f"PASS_A_SECONDS={pass_a_seconds:.3f}")
            print(f"PASS_B_SECONDS={pass_b_seconds:.3f}")
            print(f"TURBO_GAP_RECOVERY_SECONDS={turbo_gap_seconds:.3f}")
            print(f"LARGE_V3_GAP_RECOVERY_SECONDS={large_gap_seconds:.3f}")
            print(f"TOTAL_SECONDS={total_seconds:.3f}")
            print(f"FINAL_SEGMENT_COUNT={len(final_segments)}")
            print(f"FINAL_TEXT_CHARACTERS={len(final_text)}")
            print(f"LANGUAGE_SEGMENT_COUNTS={json.dumps(language_counts, ensure_ascii=False)}")
            print(f"BENCHMARK_STATUS={benchmark_results.get('status')}")
            print(f"TRANSCRIPTION_COVERAGE_STATUS={coverage_status}")
            print(f"UNCOVERED_ACTIVE_INTERVAL_COUNT={len(final_gaps)}")
            print(
                f"UNCOVERED_ACTIVE_AUDIO_SECONDS="
                f"{sum(end - start for start, end in final_gaps):.3f}"
            )
            print("TRANSCRIPTION_COVERAGE_STATUS_IS_ACCURACY_MEASUREMENT=NO")
            print("EDUCATIONAL_FORMATTING_APPLIED=NO")

            overall_result = "PASS"
            return 0
        except KeyboardInterrupt:
            timer.fail_current()
            print("TRANSCRIPTION_RESULT=CANCELLED", file=sys.stderr)
            overall_result = "CANCELLED"
            return 130
        except Exception as exception:
            timer.fail_current()
            print("TRANSCRIPTION_RESULT=FAILED", file=sys.stderr)
            print(f"ERROR={type(exception).__name__}: {exception}", file=sys.stderr)
            print(f"OUTPUT_FILE_NOT_CREATED={output_paths.transcript}", file=sys.stderr)
            overall_result = "FAILED"
            return 1
        finally:
            timer.print_summary(overall_result)


def main() -> int:
    script_directory = Path(__file__).resolve().parent
    ensure_stt_virtual_environment(script_directory)
    return run_child_v19()


if __name__ == "__main__":
    raise SystemExit(main())
