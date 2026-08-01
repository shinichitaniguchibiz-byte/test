from __future__ import annotations

import argparse
import gc
import importlib.metadata
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence


DEVICE = "cuda"
COMPUTE_TYPE = "float16"
SAMPLE_RATE = 16000
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_REPAIR_WINDOWS = 8
REPAIR_SLICE_SECONDS = 28.0
REPAIR_CORE_SECONDS = 20.0
JST = timezone(timedelta(hours=9), name="JST")

RUNTIME_PACKAGES = (
    "faster-whisper",
    "ctranslate2",
    "huggingface-hub",
)

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
    "Miller, Theo, Jennifer, ミラー, セオ, ジェニファー, "
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish, 仮定法, 目的格, 不定詞, 意味上の主語, "
    "現在進行形, 説明型オーバーラッピング, 接近のニュアンス, "
    "控えめな提案, come out, not exactly, what if, for us, be at this"
)

PRIMARY_MODEL_KEY = "turbo"
PRIMARY_MODEL_REPOSITORY = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
PRIMARY_MODEL_DIRECTORY = "faster-whisper-large-v3-turbo"
REPAIR_MODEL_KEY = "large_v3"
REPAIR_MODEL_REPOSITORY = "Systran/faster-whisper-large-v3"
REPAIR_MODEL_DIRECTORY = "faster-whisper-large-v3"

SECTION_NAMES = (
    "Grammar and Vocabulary",
    "Essential Expressions",
    "Practical Usage",
    "Pronunciation Polish",
)


class TranscriptionError(RuntimeError):
    pass


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
class RepairEvent:
    start: float
    end: float
    severity: float
    reasons: list[str]


@dataclass
class RepairWindow:
    index: int
    start: float
    end: float
    core_start: float
    core_end: float
    severity: float
    reasons: list[str]


@dataclass
class RepairDecision:
    window_index: int
    start: float
    end: float
    core_start: float
    core_end: float
    reasons: list[str]
    baseline_score: float
    repair_score: float
    baseline_characters: int
    repair_characters: int
    accepted: bool
    decision_reason: str


@dataclass
class RepeatedPhraseCluster:
    canonical: str
    members: list[tuple[int, str, float]] = field(default_factory=list)


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
                "result": result,
                "elapsed_seconds": round(elapsed, 6),
            }
        )
        self.current_name = None
        self.current_started_at = None

    def fail_current(self) -> None:
        self.finish("FAILED")

    @property
    def total_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def seconds(self, name: str) -> float:
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


class TeeTextIO(io.TextIOBase):
    def __init__(self, primary: io.TextIOBase, log_file: io.TextIOBase) -> None:
        self.primary = primary
        self.log_file = log_file

    def write(self, text: str) -> int:
        primary_result = self.primary.write(text)
        self.log_file.write(text)
        self.log_file.flush()
        return primary_result

    def flush(self) -> None:
        self.primary.flush()
        self.log_file.flush()

    @property
    def encoding(self) -> str:
        return getattr(self.primary, "encoding", "utf-8") or "utf-8"


class ConsoleLogCapture:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_file: io.TextIOWrapper | None = None
        self.original_stdout: io.TextIOBase | None = None
        self.original_stderr: io.TextIOBase | None = None

    def __enter__(self) -> "ConsoleLogCapture":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_path.open("a", encoding="utf-8", newline="")
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        sys.stdout = TeeTextIO(sys.stdout, self.log_file)
        sys.stderr = TeeTextIO(sys.stderr, self.log_file)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.original_stdout is not None:
            sys.stdout = self.original_stdout
        if self.original_stderr is not None:
            sys.stderr = self.original_stderr
        if self.log_file is not None:
            self.log_file.flush()
            self.log_file.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a faithful full transcript using a fast primary pass and "
            "selective high-accuracy repairs. Educational formatting is separate."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--max-repair-windows",
        type=int,
        default=DEFAULT_MAX_REPAIR_WINDOWS,
    )
    parser.add_argument(
        "--language",
        default="auto",
        help="Use auto for multilingual detection, or a Whisper language code.",
    )
    parser.add_argument(
        "--reference",
        default=None,
        help="Optional reference transcript used only for CER and English WER metrics.",
    )
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


def resolve_path(script_directory: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (script_directory / path).resolve()


def ensure_stt_virtual_environment(script_directory: Path) -> None:
    expected_python = script_directory / ".venv-stt" / "Scripts" / "python.exe"
    current_python = Path(sys.executable).resolve()

    if not expected_python.is_file():
        raise FileNotFoundError(
            f"The transcription virtual environment was not found: {expected_python}"
        )

    if current_python == expected_python.resolve():
        return

    print("PYTHON_ENVIRONMENT_SWITCH")
    print(f"FROM={current_python}")
    print(f"TO={expected_python}")

    environment = os.environ.copy()
    environment["TRANSCRIBE_BOOTSTRAP_FROM"] = str(current_python)
    completed = subprocess.run(
        [str(expected_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(script_directory),
        env=environment,
        check=False,
    )
    raise SystemExit(completed.returncode)


def build_run_paths(
    output_directory: Path,
    created_at: datetime,
    requested_run_id: str | None,
) -> tuple[str, Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    base_run_id = requested_run_id or created_at.astimezone(JST).strftime("%Y%m%d_%H%M%S")
    run_id = base_run_id
    sequence = 2

    while any(
        (output_directory / f"{run_id}{suffix}").exists()
        for suffix in (".txt", ".log", ".metrics.json")
    ):
        run_id = f"{base_run_id}_{sequence:02d}"
        sequence += 1

    return (
        run_id,
        output_directory / f"{run_id}.txt",
        output_directory / f"{run_id}.log",
        output_directory / f"{run_id}.metrics.json",
    )


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


def check_and_update_runtime_packages(script_directory: Path) -> dict[str, object]:
    print("SOFTWARE_VERSION_CHECK_START")
    before = installed_package_versions()
    print(
        "SOFTWARE_VERSIONS_BEFORE="
        + json.dumps(before, ensure_ascii=False, sort_keys=True)
    )

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
            *RUNTIME_PACKAGES,
        ],
        cwd=script_directory,
        timeout_seconds=900,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise TranscriptionError(
            "The latest transcription software could not be checked or installed. "
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
    return {"before": before, "after": after, "updated": changed}


def configure_huggingface_output() -> None:
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"


def query_model_revision(repository: str) -> str:
    from huggingface_hub import HfApi

    information = HfApi().model_info(
        repo_id=repository,
        revision="main",
        timeout=30,
    )
    revision = str(information.sha or "").strip()
    if not revision:
        raise TranscriptionError(
            f"The model service returned no revision for {repository}."
        )
    return revision


def model_files_are_complete(model_directory: Path) -> bool:
    return all((model_directory / name).is_file() for name in MODEL_REQUIRED_FILES)


def model_installed_revision(model_directory: Path) -> str:
    revision_file = model_directory / ".model_revision.txt"
    if not revision_file.is_file():
        return ""
    return revision_file.read_text(encoding="utf-8").strip()


def ensure_model_files(
    *,
    repository: str,
    model_directory: Path,
    latest_revision: str,
) -> bool:
    from huggingface_hub import snapshot_download

    installed_revision = model_installed_revision(model_directory)
    download_required = (
        installed_revision != latest_revision
        or not model_files_are_complete(model_directory)
    )

    print(f"MODEL_REPOSITORY={repository}")
    print(f"MODEL_DIRECTORY={model_directory}")
    print(f"MODEL_INSTALLED_REVISION={installed_revision or 'NONE'}")
    print(f"MODEL_LATEST_REVISION={latest_revision}")
    print(f"MODEL_DOWNLOAD_REQUIRED={'YES' if download_required else 'NO'}")

    if not download_required:
        return False

    model_directory.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repository,
        revision=latest_revision,
        local_dir=str(model_directory),
        allow_patterns=list(MODEL_ALLOW_PATTERNS),
    )

    if not model_files_are_complete(model_directory):
        missing = [
            name
            for name in MODEL_REQUIRED_FILES
            if not (model_directory / name).is_file()
        ]
        raise TranscriptionError(
            f"The downloaded model {repository} is incomplete. Missing: "
            + ", ".join(missing)
        )

    (model_directory / ".model_revision.txt").write_text(
        latest_revision + "\n",
        encoding="utf-8",
    )
    return True


def check_model_revisions(script_directory: Path) -> dict[str, object]:
    configure_huggingface_output()

    primary_directory = script_directory / "models" / PRIMARY_MODEL_DIRECTORY
    repair_directory = script_directory / "models" / REPAIR_MODEL_DIRECTORY

    print("MODEL_VERSION_CHECK_START")
    primary_revision = query_model_revision(PRIMARY_MODEL_REPOSITORY)
    repair_revision = query_model_revision(REPAIR_MODEL_REPOSITORY)

    print("PRIMARY_MODEL_CHECK")
    primary_updated = ensure_model_files(
        repository=PRIMARY_MODEL_REPOSITORY,
        model_directory=primary_directory,
        latest_revision=primary_revision,
    )

    repair_installed = model_files_are_complete(repair_directory)
    repair_installed_revision = model_installed_revision(repair_directory)
    repair_update_required = (
        repair_installed_revision != repair_revision or not repair_installed
    )
    print("REPAIR_MODEL_CHECK")
    print(f"MODEL_REPOSITORY={REPAIR_MODEL_REPOSITORY}")
    print(f"MODEL_DIRECTORY={repair_directory}")
    print(f"MODEL_INSTALLED_REVISION={repair_installed_revision or 'NONE'}")
    print(f"MODEL_LATEST_REVISION={repair_revision}")
    print(f"MODEL_DOWNLOAD_REQUIRED={'YES' if repair_update_required else 'NO'}")
    print("REPAIR_MODEL_DOWNLOAD_POLICY=DOWNLOAD_ONLY_IF_REPAIR_WINDOWS_EXIST")
    print("MODEL_VERSION_CHECK_RESULT=PASS")

    return {
        "primary": {
            "repository": PRIMARY_MODEL_REPOSITORY,
            "directory": str(primary_directory),
            "revision": primary_revision,
            "updated": primary_updated,
        },
        "repair": {
            "repository": REPAIR_MODEL_REPOSITORY,
            "directory": str(repair_directory),
            "revision": repair_revision,
            "installed_revision": repair_installed_revision or None,
            "download_required": repair_update_required,
        },
    }


def validate_environment(
    input_file: Path,
    batch_size: int,
    max_repair_windows: int,
    ctranslate2: object,
) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")
    if max_repair_windows < 0:
        raise ValueError("--max-repair-windows must be zero or greater.")
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


def to_segment(segment: object, source: str, offset: float = 0.0) -> SegmentRecord:
    start = max(0.0, float(getattr(segment, "start", 0.0)) + offset)
    end = max(start, float(getattr(segment, "end", start)) + offset)
    text = re.sub(r"[ \t]+", " ", str(getattr(segment, "text", ""))).strip()
    return SegmentRecord(
        start=start,
        end=end,
        text=text,
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
        source=source,
    )


def build_batch_candidates(initial_batch_size: int) -> list[int]:
    candidates: list[int] = []
    current = initial_batch_size
    while current >= 1:
        if current not in candidates:
            candidates.append(current)
        if current == 1:
            break
        current = max(1, current // 2)
    return candidates


def is_cuda_memory_error(exception: BaseException) -> bool:
    message = str(exception).lower()
    return any(
        marker in message
        for marker in (
            "out of memory",
            "cuda_error_out_of_memory",
            "failed to allocate",
            "memory allocation",
        )
    )


def transcribe_primary_once(
    *,
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    batch_size: int,
    language: str | None,
) -> tuple[list[SegmentRecord], object]:
    pipeline = BatchedInferencePipeline(model=model)
    iterator, information = pipeline.transcribe(
        audio,
        task="transcribe",
        language=language,
        multilingual=True,
        beam_size=5,
        best_of=5,
        patience=1.0,
        length_penalty=1.0,
        repetition_penalty=1.05,
        temperature=0.0,
        hotwords=TRANSCRIPTION_GLOSSARY,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 220,
            "speech_pad_ms": 500,
            "min_speech_duration_ms": 80,
        },
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        batch_size=batch_size,
        log_progress=True,
    )
    records = [to_segment(segment, "turbo") for segment in iterator]
    records = [record for record in records if record.text]
    if not records:
        raise TranscriptionError("The primary model returned no transcription segments.")
    return records, information


def transcribe_primary_with_fallback(
    *,
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    batch_candidates: Iterable[int],
    language: str | None,
) -> tuple[list[SegmentRecord], object, int]:
    last_exception: BaseException | None = None
    for batch_size in batch_candidates:
        print("PRIMARY_TRANSCRIPTION_ATTEMPT")
        print(f"BATCH_SIZE={batch_size}")
        try:
            records, information = transcribe_primary_once(
                model=model,
                BatchedInferencePipeline=BatchedInferencePipeline,
                audio=audio,
                batch_size=batch_size,
                language=language,
            )
            return records, information, batch_size
        except Exception as exception:
            last_exception = exception
            if not is_cuda_memory_error(exception):
                raise
            print(f"GPU_MEMORY_RETRY=BATCH_SIZE_{batch_size}")
            gc.collect()
    raise TranscriptionError(
        "Primary transcription failed at every batch size."
    ) from last_exception


def normalize_for_comparison(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龯々]+", "", text.lower())


def text_similarity(left: str, right: str) -> float:
    normalized_left = normalize_for_comparison(left)
    normalized_right = normalize_for_comparison(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def english_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


def contains_decoder_loop(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    for unit_length in range(2, 17):
        if re.search(rf"(.{{{unit_length}}})\1{{7,}}", compact):
            return True
    return False


def dominant_script(text: str) -> str:
    counts = {
        "latin": len(re.findall(r"[A-Za-z]", text)),
        "cjk": len(re.findall(r"[ぁ-んァ-ヶ一-龯々가-힣]", text)),
        "cyrillic": len(re.findall(r"[\u0400-\u04FF]", text)),
        "arabic": len(re.findall(r"[\u0600-\u06FF]", text)),
        "devanagari": len(re.findall(r"[\u0900-\u097F]", text)),
    }
    script, count = max(counts.items(), key=lambda item: item[1])
    return script if count > 0 else "other"


def weighted_average(values: Iterable[tuple[float, float]], default: float) -> float:
    numerator = 0.0
    denominator = 0.0
    for value, weight in values:
        effective_weight = max(weight, 0.01)
        numerator += value * effective_weight
        denominator += effective_weight
    return numerator / denominator if denominator else default


def aggregate_segment_metrics(segments: Sequence[SegmentRecord]) -> dict[str, float | int]:
    text = " ".join(segment.text for segment in segments).strip()
    return {
        "characters": len(text),
        "avg_logprob": weighted_average(
            (
                (segment.avg_logprob, max(segment.end - segment.start, 0.1))
                for segment in segments
            ),
            -10.0,
        ),
        "max_compression_ratio": max(
            (segment.compression_ratio for segment in segments),
            default=0.0,
        ),
        "avg_no_speech_prob": weighted_average(
            (
                (segment.no_speech_prob, max(segment.end - segment.start, 0.1))
                for segment in segments
            ),
            1.0,
        ),
    }


def extract_english_phrase(text: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", text).strip(" -–—")
    words = english_words(cleaned)
    if not 5 <= len(words) <= 28:
        return None
    visible = len(re.sub(r"\s+", "", cleaned))
    if visible == 0 or latin_character_count(cleaned) / visible < 0.65:
        return None
    return cleaned


def build_repeated_phrase_clusters(
    segments: Sequence[SegmentRecord],
) -> list[RepeatedPhraseCluster]:
    clusters: list[RepeatedPhraseCluster] = []
    for index, segment in enumerate(segments):
        phrase = extract_english_phrase(segment.text)
        if phrase is None:
            continue
        best_cluster: RepeatedPhraseCluster | None = None
        best_similarity = 0.0
        for cluster in clusters:
            similarity = text_similarity(phrase, cluster.canonical)
            if similarity > best_similarity:
                best_cluster = cluster
                best_similarity = similarity
        if best_cluster is not None and best_similarity >= 0.72:
            best_cluster.members.append((index, phrase, segment.avg_logprob))
        else:
            clusters.append(
                RepeatedPhraseCluster(
                    canonical=phrase,
                    members=[(index, phrase, segment.avg_logprob)],
                )
            )

    return [cluster for cluster in clusters if len(cluster.members) >= 2]


def canonical_anchor(cluster: RepeatedPhraseCluster) -> str:
    exact_counts = Counter(normalize_for_comparison(text) for _, text, _ in cluster.members)
    best_normalized, _count = exact_counts.most_common(1)[0]
    matching = [
        (text, logprob)
        for _index, text, logprob in cluster.members
        if normalize_for_comparison(text) == best_normalized
    ]
    return max(matching, key=lambda item: (item[1], len(item[0])))[0]


def event_for_segment(
    segment: SegmentRecord,
    severity: float,
    reason: str,
) -> RepairEvent:
    return RepairEvent(
        start=segment.start,
        end=segment.end,
        severity=severity,
        reasons=[reason],
    )


def detect_repair_events(
    segments: Sequence[SegmentRecord],
) -> tuple[list[RepairEvent], list[str]]:
    events: list[RepairEvent] = []
    anchors: list[str] = []

    for index, segment in enumerate(segments):
        if segment.avg_logprob < -0.55:
            events.append(event_for_segment(segment, 2.2, "low_logprob"))
        if segment.compression_ratio > 2.35:
            events.append(event_for_segment(segment, 2.0, "high_compression"))
        if segment.no_speech_prob > 0.75 and len(segment.text) >= 4:
            events.append(event_for_segment(segment, 1.6, "speech_probability_conflict"))
        if "�" in segment.text:
            events.append(event_for_segment(segment, 4.0, "replacement_character"))
        if contains_decoder_loop(segment.text):
            events.append(event_for_segment(segment, 5.0, "decoder_loop"))

        words = english_words(segment.text)
        if segment.text.strip().lower() in {"and", "to", "the", "a", "an", "but"}:
            events.append(event_for_segment(segment, 3.0, "isolated_function_word"))

        if 0 < index < len(segments) - 1 and len(segment.text) <= 40:
            previous_script = dominant_script(segments[index - 1].text)
            current_script = dominant_script(segment.text)
            next_script = dominant_script(segments[index + 1].text)
            if (
                previous_script == next_script
                and current_script != previous_script
                and len(words) <= 5
                and segment.avg_logprob < -0.20
            ):
                events.append(event_for_segment(segment, 2.4, "isolated_script_switch"))

    for index in range(1, len(segments)):
        previous = segments[index - 1]
        current = segments[index]
        gap = current.start - previous.end
        similarity = text_similarity(previous.text, current.text)
        if gap <= 1.2 and similarity >= 0.84:
            events.append(
                RepairEvent(
                    start=previous.start,
                    end=current.end,
                    severity=2.8,
                    reasons=["adjacent_duplicate_candidate"],
                )
            )

    for cluster in build_repeated_phrase_clusters(segments):
        variants = {
            normalize_for_comparison(text)
            for _index, text, _logprob in cluster.members
        }
        if len(variants) < 2:
            continue
        anchor = canonical_anchor(cluster)
        anchors.append(anchor)
        for segment_index, phrase, _logprob in cluster.members:
            if text_similarity(phrase, anchor) < 0.94:
                events.append(
                    event_for_segment(
                        segments[segment_index],
                        3.2,
                        "repeated_phrase_inconsistency",
                    )
                )

    return events, anchors[:8]


def build_repair_windows(
    events: Sequence[RepairEvent],
    audio_seconds: float,
    max_windows: int,
) -> list[RepairWindow]:
    if max_windows == 0 or not events:
        return []

    candidates: list[RepairWindow] = []
    half_slice = REPAIR_SLICE_SECONDS / 2.0
    half_core = REPAIR_CORE_SECONDS / 2.0

    for event in events:
        center = (event.start + event.end) / 2.0
        start = max(0.0, center - half_slice)
        end = min(audio_seconds, center + half_slice)
        if end - start < REPAIR_SLICE_SECONDS and audio_seconds >= REPAIR_SLICE_SECONDS:
            if start == 0.0:
                end = min(audio_seconds, REPAIR_SLICE_SECONDS)
            elif end == audio_seconds:
                start = max(0.0, audio_seconds - REPAIR_SLICE_SECONDS)
        core_start = max(start, center - half_core)
        core_end = min(end, center + half_core)
        candidates.append(
            RepairWindow(
                index=-1,
                start=start,
                end=end,
                core_start=core_start,
                core_end=core_end,
                severity=event.severity,
                reasons=list(event.reasons),
            )
        )

    candidates.sort(key=lambda item: item.severity, reverse=True)
    selected: list[RepairWindow] = []
    for candidate in candidates:
        overlapping: RepairWindow | None = None
        for existing in selected:
            overlap = min(candidate.end, existing.end) - max(candidate.start, existing.start)
            if overlap > 0 and overlap / min(
                candidate.end - candidate.start,
                existing.end - existing.start,
            ) >= 0.50:
                overlapping = existing
                break
        if overlapping is not None:
            overlapping.severity = max(overlapping.severity, candidate.severity)
            overlapping.reasons = sorted(
                set(overlapping.reasons + candidate.reasons)
            )
            continue
        selected.append(candidate)
        if len(selected) >= max_windows:
            break

    selected.sort(key=lambda item: item.start)
    for index, window in enumerate(selected):
        window.index = index
    return selected


def segments_in_range(
    segments: Sequence[SegmentRecord],
    start: float,
    end: float,
) -> list[SegmentRecord]:
    return [
        segment
        for segment in segments
        if start <= (segment.start + segment.end) / 2.0 < end
    ]


def transcribe_repair_window(
    *,
    model: object,
    audio: object,
    window: RepairWindow,
    language: str | None,
    hotwords: str,
) -> list[SegmentRecord]:
    start_sample = int(window.start * SAMPLE_RATE)
    end_sample = int(window.end * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]
    iterator, _information = model.transcribe(
        audio_slice,
        task="transcribe",
        language=language,
        multilingual=True,
        beam_size=5,
        best_of=5,
        patience=1.0,
        length_penalty=1.0,
        repetition_penalty=1.05,
        temperature=(0.0, 0.2, 0.4),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        hotwords=hotwords,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        log_progress=False,
    )
    records = [
        to_segment(segment, "large_v3", window.start)
        for segment in iterator
    ]
    return [record for record in records if record.text]


def score_candidate_segments(
    segments: Sequence[SegmentRecord],
    anchors: Sequence[str],
) -> float:
    if not segments:
        return -100.0
    metrics = aggregate_segment_metrics(segments)
    text = " ".join(segment.text for segment in segments)
    score = float(metrics["avg_logprob"]) * 2.8
    score += min(math.log1p(len(text)), 7.0) * 0.20
    if float(metrics["max_compression_ratio"]) > 2.35:
        score -= 1.1
    if float(metrics["avg_no_speech_prob"]) > 0.75 and text:
        score -= 0.8
    if contains_decoder_loop(text):
        score -= 5.0
    if "�" in text:
        score -= 4.0
    for anchor in anchors:
        similarity = text_similarity(text, anchor)
        if similarity >= 0.70:
            score += similarity * 0.45
    return score


def decide_repair(
    *,
    window: RepairWindow,
    baseline_segments: Sequence[SegmentRecord],
    repair_segments: Sequence[SegmentRecord],
    anchors: Sequence[str],
) -> RepairDecision:
    baseline_core = segments_in_range(
        baseline_segments,
        window.core_start,
        window.core_end,
    )
    repair_core = segments_in_range(
        repair_segments,
        window.core_start,
        window.core_end,
    )
    baseline_score = score_candidate_segments(baseline_core, anchors)
    repair_score = score_candidate_segments(repair_core, anchors) + 0.12
    baseline_text = " ".join(segment.text for segment in baseline_core)
    repair_text = " ".join(segment.text for segment in repair_core)

    accepted = False
    reason = "baseline_retained"
    if not repair_text:
        reason = "repair_empty"
    elif len(baseline_text) >= 40 and len(repair_text) < len(baseline_text) * 0.45:
        reason = "repair_too_short"
    elif contains_decoder_loop(repair_text) or "�" in repair_text:
        reason = "repair_structural_failure"
    elif repair_score >= baseline_score + 0.15:
        accepted = True
        reason = "repair_score_higher"
    elif window.severity >= 3.0 and repair_score >= baseline_score - 0.05:
        accepted = True
        reason = "high_severity_repair_not_worse"

    return RepairDecision(
        window_index=window.index,
        start=window.start,
        end=window.end,
        core_start=window.core_start,
        core_end=window.core_end,
        reasons=window.reasons,
        baseline_score=round(baseline_score, 6),
        repair_score=round(repair_score, 6),
        baseline_characters=len(baseline_text),
        repair_characters=len(repair_text),
        accepted=accepted,
        decision_reason=reason,
    )


def apply_accepted_repairs(
    baseline_segments: Sequence[SegmentRecord],
    repairs: Sequence[tuple[RepairWindow, Sequence[SegmentRecord], RepairDecision]],
) -> list[SegmentRecord]:
    result = list(baseline_segments)
    for window, repair_segments, decision in repairs:
        if not decision.accepted:
            continue
        result = [
            segment
            for segment in result
            if not (
                window.core_start
                <= (segment.start + segment.end) / 2.0
                < window.core_end
            )
        ]
        result.extend(
            segment
            for segment in repair_segments
            if window.core_start
            <= (segment.start + segment.end) / 2.0
            < window.core_end
        )

    result.sort(key=lambda item: (item.start, item.end))
    deduplicated: list[SegmentRecord] = []
    for segment in result:
        if not segment.text:
            continue
        if deduplicated:
            previous = deduplicated[-1]
            gap = segment.start - previous.end
            similarity = text_similarity(previous.text, segment.text)
            if gap <= 0.6 and similarity >= 0.90:
                previous_score = previous.avg_logprob + min(len(previous.text), 180) / 1000.0
                current_score = segment.avg_logprob + min(len(segment.text), 180) / 1000.0
                if current_score > previous_score:
                    deduplicated[-1] = segment
                continue
        deduplicated.append(segment)
    return deduplicated


def format_faithful_transcript(segments: Sequence[SegmentRecord]) -> str:
    lines: list[str] = []
    previous_end: float | None = None
    for segment in segments:
        if previous_end is not None and segment.start - previous_end >= 1.4:
            if lines and lines[-1] != "":
                lines.append("")
        lines.append(segment.text)
        previous_end = segment.end

    compact: list[str] = []
    for line in lines:
        if line == "" and compact and compact[-1] == "":
            continue
        compact.append(line)
    transcript = "\n".join(compact).strip()
    if not transcript:
        raise TranscriptionError("The transcription text is empty.")
    return transcript + "\n"


def structural_metrics(
    segments: Sequence[SegmentRecord],
    transcript: str,
) -> dict[str, object]:
    events, _anchors = detect_repair_events(segments)
    reason_counts = Counter(
        reason for event in events for reason in event.reasons
    )
    return {
        "segment_count": len(segments),
        "text_characters": len(transcript),
        "japanese_characters": japanese_character_count(transcript),
        "latin_characters": latin_character_count(transcript),
        "replacement_characters": transcript.count("�"),
        "decoder_loop": contains_decoder_loop(transcript),
        "section_names_found": sum(
            1 for section in SECTION_NAMES if section.lower() in transcript.lower()
        ),
        "suspicious_event_count": len(events),
        "suspicious_reason_counts": dict(reason_counts),
    }


def levenshtein_distance(left: Sequence[str], right: Sequence[str]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_value in enumerate(right, start=1):
            insert_cost = current[right_index - 1] + 1
            delete_cost = previous[right_index] + 1
            replace_cost = previous[right_index - 1] + (
                0 if left_value == right_value else 1
            )
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def normalize_reference_characters(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    normalized = re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龯々]", "", normalized)
    return list(normalized)


def normalize_reference_english_words(text: str) -> list[str]:
    return [word.lower() for word in english_words(text)]


def error_rate(reference: Sequence[str], hypothesis: Sequence[str]) -> float | None:
    if not reference:
        return None
    return levenshtein_distance(reference, hypothesis) / len(reference)


def calculate_reference_metrics(
    reference_text: str,
    baseline_text: str,
    final_text: str,
) -> dict[str, object]:
    reference_characters = normalize_reference_characters(reference_text)
    reference_english = normalize_reference_english_words(reference_text)

    baseline_cer = error_rate(
        reference_characters,
        normalize_reference_characters(baseline_text),
    )
    final_cer = error_rate(
        reference_characters,
        normalize_reference_characters(final_text),
    )
    baseline_wer = error_rate(
        reference_english,
        normalize_reference_english_words(baseline_text),
    )
    final_wer = error_rate(
        reference_english,
        normalize_reference_english_words(final_text),
    )

    return {
        "status": "MEASURED",
        "reference_characters": len(reference_characters),
        "reference_english_words": len(reference_english),
        "baseline_cer": baseline_cer,
        "final_cer": final_cer,
        "cer_change": (
            final_cer - baseline_cer
            if baseline_cer is not None and final_cer is not None
            else None
        ),
        "baseline_english_wer": baseline_wer,
        "final_english_wer": final_wer,
        "english_wer_change": (
            final_wer - baseline_wer
            if baseline_wer is not None and final_wer is not None
            else None
        ),
    }


def write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".part")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json_atomically(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".part")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def print_metric_rate(name: str, value: object) -> None:
    if value is None:
        print(f"{name}=NOT_AVAILABLE")
    else:
        print(f"{name}={float(value):.6f}")


def run() -> int:
    timer = StepTimer()
    overall_result = "FAILED"
    arguments = parse_arguments()
    script_directory = Path(__file__).resolve().parent
    created_at = datetime.now(JST)
    input_file = resolve_path(script_directory, arguments.input)
    output_directory = resolve_path(script_directory, arguments.output_dir)
    reference_file = (
        resolve_path(script_directory, arguments.reference)
        if arguments.reference
        else None
    )
    run_id, transcript_path, log_path, metrics_path = build_run_paths(
        output_directory,
        created_at,
        arguments.run_id,
    )

    with ConsoleLogCapture(log_path):
        try:
            print("RUN_START")
            print(f"RUN_ID={run_id}")
            bootstrap_from = os.environ.get("TRANSCRIBE_BOOTSTRAP_FROM")
            if bootstrap_from:
                print(f"BOOTSTRAP_PYTHON_FROM={bootstrap_from}")
                print(f"BOOTSTRAP_PYTHON_TO={sys.executable}")
            print("OUTPUT_DIRECTORY_POLICY=TRANSCRIPT_LOG_METRICS_SAME_DIRECTORY")
            print(f"TRANSCRIPT_FILE={transcript_path}")
            print(f"LOG_FILE={log_path}")
            print(f"METRICS_FILE={metrics_path}")

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
            model_metrics = check_model_revisions(script_directory)
            timer.finish()

            timer.start("validate_environment")
            validate_environment(
                input_file=input_file,
                batch_size=arguments.batch_size,
                max_repair_windows=arguments.max_repair_windows,
                ctranslate2=ctranslate2,
            )
            if reference_file is not None and not reference_file.is_file():
                raise FileNotFoundError(
                    f"Reference transcript was not found: {reference_file}"
                )
            timer.finish()

            language = None if arguments.language.lower() == "auto" else arguments.language
            batch_candidates = build_batch_candidates(arguments.batch_size)
            primary_model_path = Path(str(model_metrics["primary"]["directory"]))
            repair_model_path = Path(str(model_metrics["repair"]["directory"]))

            print("TRANSCRIPTION_CONFIGURATION")
            print("TRANSCRIPTION_MODE=FAST_BASELINE_SELECTIVE_REPAIR")
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
            print(f"MAX_REPAIR_WINDOWS={arguments.max_repair_windows}")
            print(f"REPAIR_SLICE_SECONDS={REPAIR_SLICE_SECONDS:.1f}")
            print(f"REPAIR_CORE_SECONDS={REPAIR_CORE_SECONDS:.1f}")
            print("PRIMARY_MODEL=large-v3-turbo")
            print("REPAIR_MODEL=large-v3")
            print("JAPANESE_SPECIALIST_MODEL=DISABLED_PENDING_VALID_BENCHMARK")
            print(f"INPUT={input_file}")
            print(f"REFERENCE={reference_file or 'NONE'}")

            timer.start("decode_audio")
            audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
            audio_seconds = len(audio) / SAMPLE_RATE
            timer.finish()
            print(f"AUDIO_SECONDS={audio_seconds:.3f}")

            timer.start("primary_transcription")
            primary_model = load_model(
                WhisperModel,
                primary_model_path,
                "turbo",
            )
            baseline_segments, _information, batch_size_used = (
                transcribe_primary_with_fallback(
                    model=primary_model,
                    BatchedInferencePipeline=BatchedInferencePipeline,
                    audio=audio,
                    batch_candidates=batch_candidates,
                    language=language,
                )
            )
            del primary_model
            gc.collect()
            timer.finish()

            baseline_text = format_faithful_transcript(baseline_segments)
            baseline_structural = structural_metrics(
                baseline_segments,
                baseline_text,
            )
            print(f"PRIMARY_BATCH_SIZE_USED={batch_size_used}")
            print(f"BASELINE_SEGMENT_COUNT={len(baseline_segments)}")
            print(f"BASELINE_TEXT_CHARACTERS={len(baseline_text)}")

            timer.start("detect_repair_windows")
            repair_events, repeated_anchors = detect_repair_events(baseline_segments)
            repair_windows = build_repair_windows(
                repair_events,
                audio_seconds,
                arguments.max_repair_windows,
            )
            timer.finish()

            reason_counts = Counter(
                reason for event in repair_events for reason in event.reasons
            )
            print(f"REPAIR_EVENT_COUNT={len(repair_events)}")
            print(
                "REPAIR_EVENT_REASONS="
                + json.dumps(reason_counts, ensure_ascii=False, sort_keys=True)
            )
            print(f"REPAIR_WINDOW_COUNT={len(repair_windows)}")
            print(
                "REPEATED_PHRASE_ANCHORS="
                + (
                    json.dumps(repeated_anchors, ensure_ascii=False)
                    if repeated_anchors
                    else "NONE"
                )
            )
            for window in repair_windows:
                print(
                    f"REPAIR_WINDOW INDEX={window.index:02d} "
                    f"START={window.start:.3f} END={window.end:.3f} "
                    f"CORE_START={window.core_start:.3f} CORE_END={window.core_end:.3f} "
                    f"SEVERITY={window.severity:.2f} "
                    f"REASONS={'|'.join(window.reasons)}"
                )

            repairs: list[
                tuple[RepairWindow, Sequence[SegmentRecord], RepairDecision]
            ] = []
            repair_model_updated = False

            timer.start("selective_large_v3_repairs")
            if repair_windows:
                repair_revision = str(model_metrics["repair"]["revision"])
                repair_model_updated = ensure_model_files(
                    repository=REPAIR_MODEL_REPOSITORY,
                    model_directory=repair_model_path,
                    latest_revision=repair_revision,
                )
                repair_model = load_model(
                    WhisperModel,
                    repair_model_path,
                    "large_v3",
                )
                repair_hotwords = TRANSCRIPTION_GLOSSARY
                if repeated_anchors:
                    repair_hotwords += ", " + ", ".join(repeated_anchors)
                repair_hotwords = repair_hotwords[:1200]

                for window in repair_windows:
                    print(
                        f"SELECTIVE_REPAIR_START INDEX={window.index:02d} "
                        f"START={window.start:.3f} END={window.end:.3f}"
                    )
                    repaired_segments = transcribe_repair_window(
                        model=repair_model,
                        audio=audio,
                        window=window,
                        language=language,
                        hotwords=repair_hotwords,
                    )
                    decision = decide_repair(
                        window=window,
                        baseline_segments=baseline_segments,
                        repair_segments=repaired_segments,
                        anchors=repeated_anchors,
                    )
                    repairs.append((window, repaired_segments, decision))
                    print(
                        f"SELECTIVE_REPAIR_DECISION INDEX={window.index:02d} "
                        f"ACCEPTED={'YES' if decision.accepted else 'NO'} "
                        f"BASELINE_SCORE={decision.baseline_score:.3f} "
                        f"REPAIR_SCORE={decision.repair_score:.3f} "
                        f"REASON={decision.decision_reason}"
                    )
                del repair_model
                gc.collect()
            else:
                print("SELECTIVE_REPAIR_RESULT=SKIPPED_NO_FLAGGED_WINDOWS")
            timer.finish()

            timer.start("assemble_final_transcript")
            final_segments = apply_accepted_repairs(
                baseline_segments,
                repairs,
            )
            final_text = format_faithful_transcript(final_segments)
            final_structural = structural_metrics(final_segments, final_text)
            timer.finish()

            timer.start("calculate_reference_metrics")
            if reference_file is not None:
                reference_text = reference_file.read_text(encoding="utf-8")
                reference_metrics = calculate_reference_metrics(
                    reference_text,
                    baseline_text,
                    final_text,
                )
            else:
                reference_metrics = {"status": "NOT_MEASURED"}
            timer.finish()

            timer.start("write_output_files")
            write_text_atomically(transcript_path, final_text)
            accepted_repairs = [
                decision
                for _window, _segments, decision in repairs
                if decision.accepted
            ]
            metrics: dict[str, object] = {
                "run_id": run_id,
                "created_at": created_at.isoformat(),
                "input": str(input_file),
                "output_files": {
                    "transcript": str(transcript_path),
                    "log": str(log_path),
                    "metrics": str(metrics_path),
                },
                "configuration": {
                    "language": arguments.language,
                    "batch_size_requested": arguments.batch_size,
                    "batch_size_used": batch_size_used,
                    "max_repair_windows": arguments.max_repair_windows,
                    "repair_slice_seconds": REPAIR_SLICE_SECONDS,
                    "repair_core_seconds": REPAIR_CORE_SECONDS,
                    "mode": "fast_baseline_selective_repair",
                },
                "software": software_metrics,
                "models": {
                    **model_metrics,
                    "repair_model_updated_during_run": repair_model_updated,
                    "kotoba_specialist_status": "disabled_pending_valid_benchmark",
                },
                "audio_seconds": audio_seconds,
                "baseline": baseline_structural,
                "repair_detection": {
                    "event_count": len(repair_events),
                    "reason_counts": dict(reason_counts),
                    "window_count": len(repair_windows),
                    "repeated_phrase_anchors": repeated_anchors,
                    "windows": [asdict(window) for window in repair_windows],
                },
                "repair_decisions": [
                    asdict(decision)
                    for _window, _segments, decision in repairs
                ],
                "accepted_repair_count": len(accepted_repairs),
                "final": final_structural,
                "reference_accuracy": reference_metrics,
                "timings": timer.steps,
            }
            write_json_atomically(metrics_path, metrics)
            timer.finish()

            primary_seconds = timer.seconds("primary_transcription")
            repair_seconds = timer.seconds("selective_large_v3_repairs")
            total_seconds = timer.total_seconds
            print("TRANSCRIPTION_RESULT=PASS")
            print(f"OUTPUT_FILE={transcript_path}")
            print(f"LOG_FILE={log_path}")
            print(f"METRICS_FILE={metrics_path}")
            print(f"BASELINE_SECONDS={primary_seconds:.3f}")
            print(f"SELECTIVE_REPAIR_SECONDS={repair_seconds:.3f}")
            print(f"TOTAL_SECONDS={total_seconds:.3f}")
            print(
                f"BASELINE_REALTIME_FACTOR="
                f"{primary_seconds / audio_seconds:.4f}"
            )
            print(
                f"TOTAL_PROCESS_REALTIME_FACTOR="
                f"{total_seconds / audio_seconds:.4f}"
            )
            print(f"REPAIR_WINDOWS_PROCESSED={len(repair_windows)}")
            print(f"REPAIR_WINDOWS_ACCEPTED={len(accepted_repairs)}")
            print(
                f"BASELINE_SUSPICIOUS_EVENTS="
                f"{baseline_structural['suspicious_event_count']}"
            )
            print(
                f"FINAL_SUSPICIOUS_EVENTS="
                f"{final_structural['suspicious_event_count']}"
            )
            print("REFERENCE_ACCURACY=" + str(reference_metrics["status"]))
            if reference_metrics["status"] == "MEASURED":
                print_metric_rate("BASELINE_CER", reference_metrics["baseline_cer"])
                print_metric_rate("FINAL_CER", reference_metrics["final_cer"])
                print_metric_rate("CER_CHANGE", reference_metrics["cer_change"])
                print_metric_rate(
                    "BASELINE_ENGLISH_WER",
                    reference_metrics["baseline_english_wer"],
                )
                print_metric_rate(
                    "FINAL_ENGLISH_WER",
                    reference_metrics["final_english_wer"],
                )
                print_metric_rate(
                    "ENGLISH_WER_CHANGE",
                    reference_metrics["english_wer_change"],
                )
            print("AUTOMATIC_QUALITY_SCOPE=STRUCTURAL_AND_CROSS_PASS_ONLY")
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
            print(f"OUTPUT_FILE_NOT_CREATED={transcript_path}", file=sys.stderr)
            overall_result = "FAILED"
            return 1
        finally:
            timer.print_summary(overall_result)


def main() -> int:
    script_directory = Path(__file__).resolve().parent
    ensure_stt_virtual_environment(script_directory)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
