from __future__ import annotations

import argparse
import concurrent.futures
import gc
import importlib.metadata
import json
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


TRANSCRIPTION_VERSION = 21
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
SAMPLE_RATE = 16000
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_BATCH_SIZE = 8
DEFAULT_OPENING_AUDIT_SECONDS = 210.0
JST = timezone(timedelta(hours=9), name="JST")

PRIMARY_MODEL_REPOSITORY = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
PRIMARY_MODEL_DIRECTORY_NAME = "faster-whisper-large-v3-turbo"
MODEL_REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")
MODEL_ALLOW_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.json",
    "vocabulary.*",
)
RUNTIME_PACKAGES = ("faster-whisper", "ctranslate2", "huggingface-hub")

TRANSCRIPTION_GLOSSARY = (
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish, lesson, dialogue, key sentence, example, "
    "pronunciation, Suppose, Imagine, ask for advice, 仮定法, 目的格, 不定詞, "
    "意味上の主語, 現在進行形, 説明型オーバーラッピング, "
    "come out, not exactly, what if, for us"
)

STRICT_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Grammar and Vocabulary": ("Grammar and Vocabulary", "文法と語彙"),
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
AUDITED_SECTIONS = (
    "Grammar and Vocabulary",
    "Essential Expressions",
    "Practical Usage",
)

SECTION_WINDOW_SECONDS = 30.0
SECTION_WINDOW_HOP_SECONDS = 10.0
EVENT_CLUSTER_SECONDS = 7.0
LOCAL_DUPLICATE_PADDING_SECONDS = 5.0
MIN_EVENT_SOURCE_SUPPORT = 2
MIN_EVENT_AVG_LOGPROB = -0.55
MIN_REPEATED_EVENT_AVG_LOGPROB = -0.48


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


class StepTimer:
    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.steps: list[dict[str, object]] = []
        self.current_name: str | None = None
        self.current_started_at: float | None = None

    def start(self, name: str) -> None:
        if self.current_name is not None:
            self.finish()
        self.current_name = name
        self.current_started_at = time.perf_counter()

    def finish(self, result: str = "PASS") -> float:
        if self.current_name is None or self.current_started_at is None:
            return 0.0
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
        return elapsed

    def fail_current(self) -> None:
        self.finish("FAILED")

    def seconds_for(self, name: str) -> float:
        return sum(
            float(step["elapsed_seconds"])
            for step in self.steps
            if step["name"] == name
        )

    @property
    def total_seconds(self) -> float:
        return time.perf_counter() - self.started_at

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
        self.stdout: TextIO | None = None
        self.stderr: TextIO | None = None

    def __enter__(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("w", encoding="utf-8", newline="")
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        sys.stdout = TeeStream(self.stdout, self.log_handle)
        sys.stderr = TeeStream(self.stderr, self.log_handle)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.stdout is not None:
            sys.stdout = self.stdout
        if self.stderr is not None:
            sys.stderr = self.stderr
        if self.log_handle is not None:
            self.log_handle.flush()
            self.log_handle.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a faithful Japanese-English transcript locally."
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
    parser.add_argument("--reference", default="")
    return parser.parse_args()


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def make_output_paths(output_directory: Path, run_id: str) -> OutputPaths:
    output_directory.mkdir(parents=True, exist_ok=True)
    return OutputPaths(
        run_id=run_id,
        transcript=output_directory / f"{run_id}.txt",
        log=output_directory / f"{run_id}.log",
        metrics=output_directory / f"{run_id}.metrics.json",
    )


def ensure_stt_virtual_environment(script_directory: Path) -> None:
    expected = script_directory / ".venv-stt" / "Scripts" / "python.exe"
    current = Path(sys.executable).resolve()
    if not expected.is_file():
        raise FileNotFoundError(f"STT virtual environment not found: {expected}")
    if current == expected.resolve():
        return
    run_id = os.environ.get("TRANSCRIBE_RUN_ID") or datetime.now(JST).strftime(
        "%Y%m%d_%H%M%S"
    )
    environment = dict(os.environ)
    environment["TRANSCRIBE_RUN_ID"] = run_id
    print("PYTHON_ENVIRONMENT_SWITCH")
    print(f"FROM={current}")
    print(f"TO={expected}")
    completed = subprocess.run(
        [str(expected), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(script_directory),
        env=environment,
        check=False,
    )
    raise SystemExit(completed.returncode)


def installed_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in RUNTIME_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def latest_pypi_version(package: str) -> str:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{package}/json",
        headers={"User-Agent": "radio-transcription-v21"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    version = str(payload.get("info", {}).get("version", "")).strip()
    if not version:
        raise TranscriptionError(f"PyPI returned no version for {package}")
    return version


def version_is_newer(latest: str, installed: str | None) -> bool:
    try:
        from packaging.version import Version
    except ImportError:
        from pip._vendor.packaging.version import Version
    return installed is None or Version(latest) > Version(installed)


def check_and_update_software(script_directory: Path) -> dict[str, object]:
    before = installed_versions()
    latest: dict[str, str] = {}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(latest_pypi_version, package): package
            for package in RUNTIME_PACKAGES
        }
        for future in concurrent.futures.as_completed(futures):
            package = futures[future]
            try:
                latest[package] = future.result()
            except Exception as exception:
                errors[package] = f"{type(exception).__name__}: {exception}"

    outdated = [
        package
        for package, latest_version in latest.items()
        if version_is_newer(latest_version, before.get(package))
    ]
    if outdated:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--upgrade-strategy",
                "only-if-needed",
                "--disable-pip-version-check",
                *outdated,
            ],
            cwd=str(script_directory),
            text=True,
            capture_output=True,
            check=False,
            timeout=900,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise TranscriptionError(f"Software update failed: {details}")

    after = installed_versions()
    print("SOFTWARE_VERSION_CHECK_RESULT=PASS")
    print("SOFTWARE_VERSIONS=" + json.dumps(after, ensure_ascii=False, sort_keys=True))
    print("SOFTWARE_UPDATED=" + (",".join(outdated) if outdated else "NONE"))
    if errors:
        print("SOFTWARE_VERSION_CHECK_WARNINGS=" + json.dumps(errors, ensure_ascii=False))
    return {
        "before": before,
        "latest": latest,
        "after": after,
        "updated": outdated,
        "warnings": errors,
    }


def check_model(script_directory: Path, snapshot_download: object, HfApi: object) -> dict[str, object]:
    model_directory = script_directory / "models" / PRIMARY_MODEL_DIRECTORY_NAME
    revision_file = model_directory / ".revision.txt"
    required_present = all((model_directory / name).is_file() for name in MODEL_REQUIRED_FILES)
    installed_revision = (
        revision_file.read_text(encoding="utf-8").strip()
        if revision_file.is_file()
        else ""
    )
    latest_revision = ""
    warning = ""
    try:
        latest_revision = str(HfApi().model_info(PRIMARY_MODEL_REPOSITORY).sha or "")
    except Exception as exception:
        warning = f"{type(exception).__name__}: {exception}"

    download_required = not required_present
    if required_present and latest_revision and installed_revision:
        download_required = latest_revision != installed_revision

    if download_required:
        model_directory.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=PRIMARY_MODEL_REPOSITORY,
            revision=latest_revision or None,
            local_dir=str(model_directory),
            allow_patterns=list(MODEL_ALLOW_PATTERNS),
        )
        required_present = all(
            (model_directory / name).is_file() for name in MODEL_REQUIRED_FILES
        )
        if not required_present:
            raise TranscriptionError("Primary model download is incomplete")

    if latest_revision and required_present:
        revision_file.write_text(latest_revision + "\n", encoding="utf-8")
        installed_revision = latest_revision

    if not required_present:
        raise FileNotFoundError(f"Primary model not found: {model_directory}")

    print("PRIMARY_MODEL_CHECK=PASS")
    print(f"MODEL_DIRECTORY={model_directory}")
    print(f"MODEL_DOWNLOAD_REQUIRED={'YES' if download_required else 'NO'}")
    if warning:
        print(f"MODEL_VERSION_CHECK_WARNING={warning}")
    return {
        "repository": PRIMARY_MODEL_REPOSITORY,
        "directory": str(model_directory),
        "installed_revision": installed_revision or None,
        "latest_revision": latest_revision or None,
        "downloaded": download_required,
        "warning": warning or None,
    }


def validate_environment(input_file: Path, batch_size: int, ctranslate2: object) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input audio not found: {input_file}")
    if not 1 <= batch_size <= 32:
        raise ValueError("batch-size must be between 1 and 32")
    if int(ctranslate2.get_cuda_device_count()) < 1:
        raise TranscriptionError("CUDA device was not detected")
    supported = set(ctranslate2.get_supported_compute_types(DEVICE))
    if COMPUTE_TYPE not in supported:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is unsupported. Supported: {sorted(supported)}"
        )


def load_model(WhisperModel: object, model_directory: Path) -> object:
    print("MODEL_LOAD_START")
    model = WhisperModel(
        str(model_directory),
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        local_files_only=True,
        use_flash_attention=False,
    )
    print("FLASH_ATTENTION=False")
    print("MODEL_LOAD_COMPLETE")
    return model


def analyze_audio_signal(audio: object) -> dict[str, object]:
    import numpy as np

    values = np.asarray(audio, dtype=np.float32)
    if values.size == 0:
        raise TranscriptionError("Decoded audio is empty")
    rms = float(np.sqrt(np.mean(values * values) + 1e-12))
    peak = float(np.max(np.abs(values)))
    frame_size = SAMPLE_RATE
    frame_levels: list[float] = []
    for start in range(0, len(values), frame_size):
        frame = values[start : start + frame_size]
        if frame.size == 0:
            continue
        frame_rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
        frame_levels.append(20.0 * math.log10(max(frame_rms, 1e-12)))
    levels = np.asarray(frame_levels, dtype=np.float32)
    clipped_ratio = float(np.mean(np.abs(values) >= 0.999))
    low_level_ratio = float(np.mean(levels < -50.0)) if levels.size else 0.0
    flags: list[str] = []
    if clipped_ratio > 0.001:
        flags.append("CLIPPING")
    if low_level_ratio > 0.55:
        flags.append("MANY_LOW_LEVEL_FRAMES")
    return {
        "scope": "SIGNAL_LEVEL_DIAGNOSTIC_NOT_TRANSCRIPTION_ACCURACY",
        "sample_rate": SAMPLE_RATE,
        "sample_count": int(values.size),
        "rms_dbfs": round(20.0 * math.log10(max(rms, 1e-12)), 3),
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-12)), 3),
        "clipped_sample_ratio": clipped_ratio,
        "low_level_frame_ratio": low_level_ratio,
        "frame_dbfs_p10": round(float(np.percentile(levels, 10)), 3),
        "frame_dbfs_p50": round(float(np.percentile(levels, 50)), 3),
        "frame_dbfs_p95": round(float(np.percentile(levels, 95)), 3),
        "dynamic_range_p95_minus_p10_db": round(
            float(np.percentile(levels, 95) - np.percentile(levels, 10)), 3
        ),
        "quality_flags": flags,
        "quality_status": "PASS" if not flags else "REVIEW",
    }


def clean_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("�", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text.strip()


def normalize_for_comparison(text: str) -> str:
    return re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龯々]+", "", text.lower())


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


def english_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)


def language_label(text: str) -> str:
    japanese = japanese_character_count(text)
    latin = latin_character_count(text)
    if japanese and latin:
        return "mixed"
    if japanese:
        return "ja"
    if latin:
        return "en"
    return "unknown"


def split_english_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for piece in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        piece = clean_text(piece)
        words = english_words(piece)
        compact = re.sub(r"\s+", "", piece)
        if not compact or not 4 <= len(words) <= 40:
            continue
        if latin_character_count(piece) / len(compact) < 0.82:
            continue
        if len({word.lower() for word in words}) / len(words) < 0.42:
            continue
        candidates.append(piece)
    return candidates


def detect_decoder_loop(text: str) -> bool:
    words = re.findall(r"[A-Za-zぁ-んァ-ヶ一-龯々]+", text.lower())
    if len(words) < 18:
        return False
    for size in (2, 3, 4, 5):
        chunks = [tuple(words[index : index + size]) for index in range(len(words) - size + 1)]
        if chunks and Counter(chunks).most_common(1)[0][1] >= 6:
            return True
    return False


def strict_section_hits(text: str) -> dict[str, bool]:
    lower = text.lower()
    return {
        section: any(alias.lower() in lower for alias in aliases)
        for section, aliases in STRICT_SECTION_ALIASES.items()
    }


def segment_from_object(segment: object, source: str, offset: float = 0.0) -> SegmentRecord | None:
    text = clean_text(str(getattr(segment, "text", "")))
    if not text:
        return None
    return SegmentRecord(
        start=offset + float(getattr(segment, "start", 0.0)),
        end=offset + float(getattr(segment, "end", 0.0)),
        text=text,
        avg_logprob=float(getattr(segment, "avg_logprob", -10.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
        source=source,
    )


def deduplicate_segments(segments: Iterable[SegmentRecord]) -> list[SegmentRecord]:
    result: list[SegmentRecord] = []
    for candidate in sorted(segments, key=lambda item: (item.start, item.end)):
        duplicate: int | None = None
        for index in range(len(result) - 1, max(-1, len(result) - 8), -1):
            previous = result[index]
            if candidate.start - previous.end > 2.0:
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
                duplicate = index
                break
        if duplicate is None:
            result.append(candidate)
            continue
        previous = result[duplicate]
        previous_score = previous.avg_logprob + min(len(previous.text), 180) / 1000.0
        candidate_score = candidate.avg_logprob + min(len(candidate.text), 180) / 1000.0
        if candidate_score > previous_score:
            result[duplicate] = candidate
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
        raise TranscriptionError("Final transcript is empty")
    return transcript + "\n"


def batch_sizes(initial: int) -> list[int]:
    values: list[int] = []
    current = initial
    while True:
        if current not in values:
            values.append(current)
        if current == 1:
            return values
        current = max(1, current // 2)


def is_cuda_memory_error(exception: BaseException) -> bool:
    message = str(exception).lower()
    return any(
        token in message
        for token in (
            "out of memory",
            "cuda_error_out_of_memory",
            "failed to allocate",
            "memory allocation",
        )
    )


def transcribe_batched_baseline(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    language: str | None,
    requested_batch_size: int,
) -> tuple[list[SegmentRecord], int, str]:
    pipeline = BatchedInferencePipeline(model=model)
    last_exception: BaseException | None = None
    for batch_size in batch_sizes(requested_batch_size):
        print(f"PRIMARY_TRANSCRIPTION_ATTEMPT BATCH_SIZE={batch_size}")
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
                if (record := segment_from_object(segment, "turbo_batched"))
                is not None
            ]
            if not records:
                raise TranscriptionError("Primary model returned no segments")
            detected = str(getattr(information, "language", language or "unknown"))
            return deduplicate_segments(records), batch_size, detected
        except Exception as exception:
            last_exception = exception
            if not is_cuda_memory_error(exception):
                raise
            print(f"GPU_MEMORY_RETRY_AFTER_BATCH_SIZE={batch_size}")
            gc.collect()
    raise TranscriptionError("Primary transcription failed at every batch size") from last_exception


def transcribe_slice(
    model: object,
    audio: object,
    *,
    start: float,
    end: float,
    language: str | None,
    source: str,
    beam_size: int,
    multilingual: bool,
) -> list[SegmentRecord]:
    start_sample = max(0, int(start * SAMPLE_RATE))
    end_sample = min(len(audio), int(end * SAMPLE_RATE))
    if end_sample <= start_sample:
        return []
    iterator, _information = model.transcribe(
        audio[start_sample:end_sample],
        task="transcribe",
        language=language,
        multilingual=multilingual,
        beam_size=beam_size,
        best_of=beam_size,
        patience=1.0,
        temperature=0.0,
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
    return deduplicate_segments(
        record
        for segment in iterator
        if (record := segment_from_object(segment, source, start)) is not None
    )


def section_intervals(
    segments: list[SegmentRecord], audio_seconds: float
) -> dict[str, tuple[float, float]]:
    first_hits: dict[str, float] = {}
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        for section, hit in strict_section_hits(segment.text).items():
            if hit and section not in first_hits:
                first_hits[section] = segment.start
    order = [
        "Grammar and Vocabulary",
        "Essential Expressions",
        "Practical Usage",
        "Pronunciation Polish",
    ]
    intervals: dict[str, tuple[float, float]] = {}
    for index, section in enumerate(order[:-1]):
        start = first_hits.get(section)
        end = first_hits.get(order[index + 1])
        if start is None or end is None or end - start < 8.0:
            continue
        intervals[section] = (max(0.0, start), min(audio_seconds, end))
    return intervals


def transcribe_section_windows(
    model: object,
    audio: object,
    *,
    section_name: str,
    start: float,
    end: float,
) -> tuple[list[SegmentRecord], int]:
    records: list[SegmentRecord] = []
    window_count = 0
    cursor = start
    slug = re.sub(r"[^a-z0-9]+", "_", section_name.lower()).strip("_")
    while cursor < end:
        chunk_end = min(end, cursor + SECTION_WINDOW_SECONDS)
        if chunk_end - cursor < 8.0 and window_count > 0:
            break
        window_count += 1
        print(
            f"SECTION_COVERAGE_WINDOW SECTION={section_name} "
            f"INDEX={window_count:02d} START={cursor:.3f} END={chunk_end:.3f}"
        )
        records.extend(
            transcribe_slice(
                model,
                audio,
                start=cursor,
                end=chunk_end,
                language="en",
                source=f"v21_{slug}_w{window_count:02d}",
                beam_size=3,
                multilingual=False,
            )
        )
        if chunk_end >= end:
            break
        cursor += SECTION_WINDOW_HOP_SECONDS
    return records, window_count


def candidate_observations(segments: list[SegmentRecord]) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for segment in segments:
        for candidate in split_english_candidates(segment.text):
            if segment.avg_logprob < -0.72 or detect_decoder_loop(candidate):
                continue
            observations.append(
                {
                    "text": candidate,
                    "normalized": normalize_for_comparison(candidate),
                    "start": segment.start,
                    "end": segment.end,
                    "midpoint": (segment.start + segment.end) / 2.0,
                    "avg_logprob": segment.avg_logprob,
                    "source": segment.source,
                }
            )
    return observations


def local_baseline_candidates(
    baseline_segments: list[SegmentRecord], start: float, end: float
) -> list[str]:
    text = " ".join(
        segment.text
        for segment in baseline_segments
        if segment.end >= start - LOCAL_DUPLICATE_PADDING_SECONDS
        and segment.start <= end + LOCAL_DUPLICATE_PADDING_SECONDS
    )
    return split_english_candidates(text)


def recover_time_local_consensus(
    baseline_segments: list[SegmentRecord],
    audit_segments: list[SegmentRecord],
    section_name: str,
) -> tuple[list[SegmentRecord], list[dict[str, object]]]:
    observations = candidate_observations(audit_segments)
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

    recovered: list[SegmentRecord] = []
    evidence: list[dict[str, object]] = []
    for cluster_index, cluster in enumerate(clusters):
        variants = Counter(clean_text(str(item["text"])) for item in cluster)
        canonical = max(variants, key=lambda value: (variants[value], len(value)))
        canonical_normalized = normalize_for_comparison(canonical)
        if any(
            alias.lower() in canonical.lower()
            for aliases in STRICT_SECTION_ALIASES.values()
            for alias in aliases
        ):
            continue
        matching = [
            item
            for item in cluster
            if SequenceMatcher(
                None, canonical_normalized, str(item["normalized"])
            ).ratio()
            >= 0.90
        ]
        matching.sort(key=lambda item: float(item["midpoint"]))
        if not matching:
            continue

        events: list[list[dict[str, object]]] = []
        for observation in matching:
            midpoint = float(observation["midpoint"])
            if not events:
                events.append([observation])
                continue
            previous_midpoint = sum(float(item["midpoint"]) for item in events[-1]) / len(
                events[-1]
            )
            if midpoint - previous_midpoint > EVENT_CLUSTER_SECONDS:
                events.append([observation])
            else:
                events[-1].append(observation)

        repeated_at_separate_times = len(events) >= 2
        for event_index, event in enumerate(events):
            sources = sorted({str(item["source"]) for item in event})
            event_start = min(float(item["start"]) for item in event)
            event_end = max(float(item["end"]) for item in event)
            event_avg_logprob = sum(float(item["avg_logprob"]) for item in event) / len(
                event
            )
            source_supported = len(sources) >= MIN_EVENT_SOURCE_SUPPORT
            repeated_supported = (
                repeated_at_separate_times
                and event_avg_logprob >= MIN_REPEATED_EVENT_AVG_LOGPROB
            )
            accepted = source_supported and event_avg_logprob >= MIN_EVENT_AVG_LOGPROB
            evidence_type = "overlapping_window_consensus"
            if not accepted and repeated_supported:
                accepted = True
                evidence_type = "same_sentence_repeated_at_separate_times"

            baseline_candidates = local_baseline_candidates(
                baseline_segments, event_start, event_end
            )
            similarities = [
                SequenceMatcher(
                    None,
                    canonical_normalized,
                    normalize_for_comparison(candidate),
                ).ratio()
                for candidate in baseline_candidates
                if normalize_for_comparison(candidate)
            ]
            local_similarity = max(similarities, default=0.0)
            already_present = any(
                canonical_normalized in normalize_for_comparison(candidate)
                or normalize_for_comparison(candidate) in canonical_normalized
                or SequenceMatcher(
                    None,
                    canonical_normalized,
                    normalize_for_comparison(candidate),
                ).ratio()
                >= 0.86
                for candidate in baseline_candidates
                if normalize_for_comparison(candidate)
            )
            reason = "accepted"
            if already_present:
                accepted = False
                reason = "already_present_at_same_time"
            elif not source_supported and not repeated_supported:
                reason = "insufficient_independent_support"
            elif event_avg_logprob < MIN_EVENT_AVG_LOGPROB and not repeated_supported:
                reason = "low_event_confidence"

            evidence.append(
                {
                    "cluster_index": cluster_index,
                    "event_index": event_index,
                    "section": section_name,
                    "text": canonical,
                    "start": event_start,
                    "end": event_end,
                    "source_count": len(sources),
                    "sources": sources,
                    "avg_logprob": event_avg_logprob,
                    "temporal_event_count": len(events),
                    "evidence_type": evidence_type,
                    "local_similarity": local_similarity,
                    "accepted": accepted,
                    "decision_reason": reason,
                }
            )
            if accepted:
                recovered.append(
                    SegmentRecord(
                        start=event_start,
                        end=event_end,
                        text=canonical,
                        avg_logprob=event_avg_logprob,
                        compression_ratio=0.0,
                        no_speech_prob=0.0,
                        source="turbo_v21_time_local_section_consensus",
                    )
                )

    return deduplicate_segments(recovered), evidence


def opening_is_sparse(segments: list[SegmentRecord], core_end: float) -> tuple[bool, dict[str, object]]:
    opening = [
        segment
        for segment in segments
        if (segment.start + segment.end) / 2.0 <= core_end
    ]
    text = format_transcript(opening) if opening else ""
    diagnostics = {
        "segment_count": len(opening),
        "text_characters": len(text),
        "japanese_characters": japanese_character_count(text),
        "latin_characters": latin_character_count(text),
        "english_candidate_count": len(split_english_candidates(text)),
        "decoder_loop": detect_decoder_loop(text),
    }
    sufficient = (
        len(opening) >= 8
        and len(text) >= 500
        and int(diagnostics["japanese_characters"]) >= 120
        and int(diagnostics["latin_characters"]) >= 250
        and int(diagnostics["english_candidate_count"]) >= 5
        and not bool(diagnostics["decoder_loop"])
    )
    diagnostics["decision"] = "SKIP_SUFFICIENT_BASELINE" if sufficient else "RUN_AUDIT"
    return not sufficient, diagnostics


def token_recall(baseline: str, candidate: str) -> float:
    baseline_tokens = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[ぁ-んァ-ヶ一-龯々]", baseline.lower())
    available = Counter(
        re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[ぁ-んァ-ヶ一-龯々]", candidate.lower())
    )
    if not baseline_tokens:
        return 1.0
    matched = 0
    for token in baseline_tokens:
        if available[token] > 0:
            matched += 1
            available[token] -= 1
    return matched / len(baseline_tokens)


def run_conditional_opening_audit(
    model: object,
    audio: object,
    baseline_segments: list[SegmentRecord],
    audio_seconds: float,
    requested_seconds: float,
    language: str | None,
) -> tuple[list[SegmentRecord], dict[str, object]]:
    if requested_seconds <= 0:
        return baseline_segments, {"status": "DISABLED"}
    core_end = min(requested_seconds, audio_seconds)
    run_audit, diagnostics = opening_is_sparse(baseline_segments, core_end)
    if not run_audit:
        return baseline_segments, {"status": "SKIPPED", "diagnostics": diagnostics}

    candidate = transcribe_slice(
        model,
        audio,
        start=0.0,
        end=min(audio_seconds, core_end + 2.5),
        language=language,
        source="turbo_opening_no_vad",
        beam_size=5,
        multilingual=True,
    )
    candidate = [
        segment
        for segment in candidate
        if (segment.start + segment.end) / 2.0 <= core_end
    ]
    baseline_opening = [
        segment
        for segment in baseline_segments
        if (segment.start + segment.end) / 2.0 <= core_end
    ]
    baseline_text = format_transcript(baseline_opening)
    candidate_text = format_transcript(candidate) if candidate else ""
    coverage_ratio = len(candidate_text) / max(len(baseline_text), 1)
    recall = token_recall(baseline_text, candidate_text)
    accepted = (
        bool(candidate_text)
        and 1.03 <= coverage_ratio <= 1.50
        and recall >= 0.80
        and not detect_decoder_loop(candidate_text)
    )
    if accepted:
        retained = [
            segment
            for segment in baseline_segments
            if (segment.start + segment.end) / 2.0 > core_end
        ]
        merged = deduplicate_segments([*candidate, *retained])
    else:
        merged = baseline_segments
    return merged, {
        "status": "RUN",
        "diagnostics": diagnostics,
        "accepted": accepted,
        "coverage_ratio": coverage_ratio,
        "token_recall": recall,
        "baseline_text": baseline_text,
        "candidate_text": candidate_text,
    }


def correct_repeated_english_variants(
    text: str,
) -> tuple[str, list[dict[str, object]]]:
    candidates = split_english_candidates(text)
    clusters: list[list[str]] = []
    for candidate in candidates:
        normalized = normalize_for_comparison(candidate)
        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None, normalized, normalize_for_comparison(cluster[0])
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
    for cluster in clusters:
        counts = Counter(clean_text(candidate) for candidate in cluster)
        if sum(counts.values()) < 4 or len(counts) < 2:
            continue
        canonical, canonical_count = max(
            counts.items(), key=lambda item: (item[1], len(item[0]))
        )
        canonical_words = english_words(canonical)
        if canonical_count < 2 or len(canonical_words) < 5:
            continue
        for variant, variant_count in counts.items():
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
            similarity = SequenceMatcher(
                None,
                normalize_for_comparison(canonical),
                normalize_for_comparison(variant),
            ).ratio()
            if similarity < 0.82 or canonical_count <= variant_count:
                continue
            occurrences = corrected.count(variant)
            if occurrences == 0:
                continue
            corrected = corrected.replace(variant, canonical)
            corrections.append(
                {
                    "from": variant,
                    "to": canonical,
                    "occurrences": occurrences,
                    "canonical_count": canonical_count,
                    "variant_count": variant_count,
                    "similarity": round(similarity, 6),
                }
            )
    return corrected, corrections


def apply_corrections_to_segments(
    segments: list[SegmentRecord], corrections: list[dict[str, object]]
) -> None:
    for segment in segments:
        for correction in corrections:
            segment.text = segment.text.replace(
                str(correction["from"]), str(correction["to"])
            )


def transcript_indicators(text: str, segments: list[SegmentRecord]) -> dict[str, object]:
    sections = strict_section_hits(text)
    return {
        "text_characters": len(text),
        "segment_count": len(segments),
        "japanese_characters": japanese_character_count(text),
        "latin_characters": latin_character_count(text),
        "replacement_characters": text.count("�"),
        "decoder_loop": detect_decoder_loop(text),
        "strict_section_hits": sections,
        "strict_section_count": sum(sections.values()),
        "language_segment_counts": dict(
            Counter(language_label(segment.text) for segment in segments)
        ),
    }


def lesson76_regression(
    input_file: Path,
    audio_seconds: float,
    segments: list[SegmentRecord],
) -> dict[str, object]:
    if input_file.name.lower() != "b.m4a" or not 850.0 <= audio_seconds <= 870.0:
        return {"status": "NOT_APPLICABLE", "profile": None, "items": []}
    checks = [
        ("Dad's birthday is coming up next week", 20.0, 100.0, 1, "opening_dialogue"),
        ("We're not exactly great in the kitchen", 20.0, 120.0, 1, "opening_dialogue"),
        ("We're not exactly great in the kitchen", 180.0, 330.0, 2, "grammar_quote_and_replay"),
        ("Suppose we asked Emily for advice", 385.0, 435.0, 1, "essential_first_example"),
        ("Suppose we asked Emily for advice", 465.0, 535.0, 1, "essential_practice"),
        ("Imagine we asked Emily for advice", 385.0, 435.0, 1, "essential_first_example"),
        ("Imagine we asked Emily for advice", 465.0, 535.0, 1, "essential_practice"),
        ("It might be a good idea for us to discuss this together", 410.0, 470.0, 1, "essential_first_example"),
        ("It might be a good idea for us to discuss this together", 480.0, 540.0, 1, "essential_practice"),
        ("Just an idea, but we could try leaving a little earlier tomorrow", 440.0, 490.0, 1, "essential_first_example"),
        ("Just an idea, but we could try leaving a little earlier tomorrow", 490.0, 545.0, 1, "essential_practice"),
        ("It might be a good idea for us to take a break for a while", 620.0, 660.0, 1, "practical_model_answer"),
        ("We've been at this for a long time", 620.0, 660.0, 1, "practical_model_answer"),
        ("We're all a bit tired", 620.0, 660.0, 1, "practical_model_answer"),
        ("It might be a good idea for us to take a break for a while", 680.0, 735.0, 1, "practical_practice"),
        ("We've been at this for a long time", 680.0, 735.0, 1, "practical_practice"),
        ("We're all a bit tired", 680.0, 735.0, 1, "practical_practice"),
        ("What if we baked him a cake ourselves", 735.0, 830.0, 4, "pronunciation_repetition"),
    ]
    items: list[dict[str, object]] = []
    for phrase, start, end, minimum, role in checks:
        expected = normalize_for_comparison(phrase)
        region_text = " ".join(
            segment.text
            for segment in segments
            if segment.end >= start and segment.start <= end
        )
        occurrences = normalize_for_comparison(region_text).count(expected)
        items.append(
            {
                "phrase": phrase,
                "start": start,
                "end": end,
                "role": role,
                "minimum_occurrences": minimum,
                "observed_occurrences": occurrences,
                "passed": occurrences >= minimum,
            }
        )
    return {
        "status": "PASS" if all(bool(item["passed"]) for item in items) else "FAIL",
        "profile": "lesson76_time_local_repetition_regression_v21",
        "scope": "KNOWN_PHRASE_TIME_AND_OCCURRENCE_REGRESSION_NOT_ACCURACY_PERCENTAGE",
        "passed_count": sum(bool(item["passed"]) for item in items),
        "total_count": len(items),
        "items": items,
    }


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


def reference_metrics(reference: str, baseline: str, final: str) -> dict[str, object]:
    reference_characters = list(re.sub(r"\s+", "", reference))
    reference_english = [word.lower() for word in english_words(reference)]

    def cer(text: str) -> float:
        units = list(re.sub(r"\s+", "", text))
        return levenshtein_distance(reference_characters, units) / max(
            len(reference_characters), 1
        )

    def wer(text: str) -> float | None:
        if not reference_english:
            return None
        units = [word.lower() for word in english_words(text)]
        return levenshtein_distance(reference_english, units) / len(reference_english)

    baseline_wer = wer(baseline)
    final_wer = wer(final)
    return {
        "status": "MEASURED",
        "baseline_cer": cer(baseline),
        "final_cer": cer(final),
        "baseline_english_wer": baseline_wer,
        "final_english_wer": final_wer,
    }


def write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json_atomically(path: Path, data: dict[str, object]) -> None:
    write_text_atomically(
        path,
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def run_transcription() -> int:
    timer = StepTimer()
    overall_result = "FAILED"
    arguments = parse_arguments()
    script_directory = Path(__file__).resolve().parent
    run_id = os.environ.get("TRANSCRIBE_RUN_ID") or datetime.now(JST).strftime(
        "%Y%m%d_%H%M%S"
    )
    input_file = resolve_path(script_directory, arguments.input)
    output_directory = resolve_path(script_directory, arguments.output_dir)
    outputs = make_output_paths(output_directory, run_id)
    reference_file = (
        resolve_path(script_directory, arguments.reference)
        if arguments.reference
        else None
    )

    with TeeContext(outputs.log):
        try:
            print("RUN_START")
            print(f"RUN_ID={run_id}")
            print("TRANSCRIPTION_TRANSACTION=1")
            print(f"TRANSCRIPTION_VERSION={TRANSCRIPTION_VERSION}")
            print("TRANSCRIPTION_MODE=FAST_PRIMARY_PLUS_TIME_LOCAL_SECTION_CONSENSUS")
            print("PRIMARY_PASS=FAST_BATCHED_VAD_FULL_AUDIO")
            print("SECTION_RECOVERY=GRAMMAR_ESSENTIAL_PRACTICAL_ONLY")
            print("SECTION_RECOVERY_WINDOW=30_SECONDS_HOP_10_SECONDS")
            print("SECTION_RECOVERY_CONSENSUS=TIME_LOCAL_OVERLAPPING_WINDOWS")
            print("FULL_AUDIO_SECOND_PASS=DISABLED")
            print("LARGE_V3_AUTOMATIC_PASS=DISABLED")
            print("CANDIDATES_IN_TRANSCRIPT=NO")
            print("CANDIDATES_IN_METRICS=YES")
            print("CONTENT_REMOVAL_APPLIED=NO")
            print("EDUCATIONAL_FORMATTING_APPLIED=NO")
            print(f"TRANSCRIPT_FILE={outputs.transcript}")
            print(f"LOG_FILE={outputs.log}")
            print(f"METRICS_FILE={outputs.metrics}")

            timer.start("check_and_update_software")
            software = check_and_update_software(script_directory)
            timer.finish()

            timer.start("import_transcription_libraries")
            import ctranslate2
            import faster_whisper
            from faster_whisper import BatchedInferencePipeline, WhisperModel
            from faster_whisper.audio import decode_audio
            from huggingface_hub import HfApi, snapshot_download

            print(f"FASTER_WHISPER={faster_whisper.__version__}")
            print(f"CTRANSLATE2={ctranslate2.__version__}")
            timer.finish()

            timer.start("check_primary_model_revision")
            model_information = check_model(
                script_directory, snapshot_download, HfApi
            )
            timer.finish()

            timer.start("validate_environment")
            validate_environment(input_file, arguments.batch_size, ctranslate2)
            if reference_file is not None and not reference_file.is_file():
                raise FileNotFoundError(f"Reference file not found: {reference_file}")
            timer.finish()

            timer.start("decode_audio")
            audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
            audio_seconds = len(audio) / SAMPLE_RATE
            audio_signal = analyze_audio_signal(audio)
            timer.finish()
            print(f"AUDIO_SECONDS={audio_seconds:.3f}")
            print(f"AUDIO_SIGNAL_QUALITY={audio_signal['quality_status']}")

            timer.start("load_primary_model")
            model = load_model(
                WhisperModel, Path(str(model_information["directory"]))
            )
            timer.finish()

            requested_language = None if arguments.language == "auto" else arguments.language

            timer.start("primary_batched_transcription")
            baseline_segments, batch_size_used, detected_language = transcribe_batched_baseline(
                model,
                BatchedInferencePipeline,
                audio,
                requested_language,
                arguments.batch_size,
            )
            baseline_text = format_transcript(baseline_segments)
            timer.finish()
            print(f"PRIMARY_BATCH_SIZE_USED={batch_size_used}")
            print(f"DETECTED_LANGUAGE={detected_language}")
            print(f"BASELINE_SEGMENT_COUNT={len(baseline_segments)}")

            timer.start("conditional_opening_audit")
            post_opening_segments, opening_audit = run_conditional_opening_audit(
                model,
                audio,
                baseline_segments,
                audio_seconds,
                arguments.opening_audit_seconds,
                requested_language,
            )
            timer.finish()
            print(f"OPENING_AUDIT_STATUS={opening_audit['status']}")

            timer.start("section_coverage_planning")
            intervals = section_intervals(post_opening_segments, audio_seconds)
            section_plan = [
                {
                    "section": section,
                    "start": intervals[section][0],
                    "end": intervals[section][1],
                    "seconds": intervals[section][1] - intervals[section][0],
                }
                for section in AUDITED_SECTIONS
                if section in intervals
            ]
            timer.finish()
            print(f"SECTION_COVERAGE_SECTIONS={len(section_plan)}")

            timer.start("section_coverage_recovery")
            recovered_segments: list[SegmentRecord] = []
            evidence: list[dict[str, object]] = []
            window_counts: dict[str, int] = {}
            section_seconds: dict[str, float] = {}
            for plan in section_plan:
                section_name = str(plan["section"])
                section_started = time.perf_counter()
                audit_segments, window_count = transcribe_section_windows(
                    model,
                    audio,
                    section_name=section_name,
                    start=float(plan["start"]),
                    end=float(plan["end"]),
                )
                recovered, section_evidence = recover_time_local_consensus(
                    post_opening_segments,
                    audit_segments,
                    section_name,
                )
                recovered_segments.extend(recovered)
                evidence.extend(section_evidence)
                window_counts[section_name] = window_count
                elapsed = time.perf_counter() - section_started
                section_seconds[section_name] = elapsed
                print(
                    f"SECTION_COVERAGE_RESULT SECTION={section_name} "
                    f"WINDOWS={window_count} RECOVERED={len(recovered)} "
                    f"ELAPSED_SECONDS={elapsed:.3f}"
                )
            recovered_segments = deduplicate_segments(recovered_segments)
            timer.finish()

            del model
            gc.collect()

            timer.start("assemble_final_transcript")
            final_segments = deduplicate_segments(
                [*post_opening_segments, *recovered_segments]
            )
            raw_final_text = format_transcript(final_segments)
            final_text, corrections = correct_repeated_english_variants(raw_final_text)
            apply_corrections_to_segments(final_segments, corrections)
            final_text = format_transcript(final_segments)
            final_indicators = transcript_indicators(final_text, final_segments)
            timer.finish()

            timer.start("evaluate_regression_profile")
            regression = lesson76_regression(
                input_file, audio_seconds, final_segments
            )
            timer.finish()

            timer.start("calculate_reference_metrics")
            accuracy = (
                {"status": "NOT_MEASURED"}
                if reference_file is None
                else reference_metrics(
                    reference_file.read_text(encoding="utf-8"),
                    baseline_text,
                    final_text,
                )
            )
            timer.finish()

            timer.start("write_transcript")
            write_text_atomically(outputs.transcript, final_text)
            timer.finish()

            startup_steps = (
                "check_and_update_software",
                "import_transcription_libraries",
                "check_primary_model_revision",
                "validate_environment",
                "decode_audio",
                "load_primary_model",
            )
            startup_seconds = sum(timer.seconds_for(name) for name in startup_steps)
            primary_seconds = timer.seconds_for("primary_batched_transcription")
            opening_seconds = timer.seconds_for("conditional_opening_audit")
            planning_seconds = timer.seconds_for("section_coverage_planning")
            recovery_seconds = timer.seconds_for("section_coverage_recovery")
            warm_core_seconds = (
                primary_seconds + opening_seconds + planning_seconds + recovery_seconds
            )
            total_seconds = timer.total_seconds
            total_rtf = total_seconds / max(audio_seconds, 0.001)
            warm_rtf = warm_core_seconds / max(audio_seconds, 0.001)
            performance_class = (
                "FAST"
                if warm_rtf <= 0.12
                else "ACCEPTABLE"
                if warm_rtf <= 0.18
                else "SLOW"
            )
            structural_pass = (
                not bool(final_indicators["decoder_loop"])
                and int(final_indicators["replacement_characters"]) == 0
                and int(final_indicators["strict_section_count"]) == 4
            )
            regression_pass = regression.get("status") in {"PASS", "NOT_APPLICABLE"}
            quality_gate = "PASS" if structural_pass and regression_pass else "FAIL"
            acceptance = (
                "ACCEPT_TRANSACTION_1"
                if quality_gate == "PASS" and total_seconds <= 150.0
                else "REVIEW_REQUIRED"
            )

            metrics = {
                "run_id": run_id,
                "created_at": datetime.now(JST).isoformat(),
                "transaction": 1,
                "version": TRANSCRIPTION_VERSION,
                "convergence": {
                    "status": "FINAL_ACCEPTANCE_TEST",
                    "source_versions_combined": [14, 16, 18, 19, 20],
                    "v19_removed": [
                        "full_audio_second_pass",
                        "automatic_large_v3_gap_recovery",
                        "candidate_text_in_transcript",
                        "uncertain_markers_in_transcript",
                    ],
                    "v20_removed": [
                        "gap_length_ranking",
                        "two_boundary_gap_only_consensus",
                    ],
                },
                "input": str(input_file),
                "output_files": {
                    "transcript": str(outputs.transcript),
                    "log": str(outputs.log),
                    "metrics": str(outputs.metrics),
                },
                "configuration": {
                    "batch_size_requested": arguments.batch_size,
                    "batch_size_used": batch_size_used,
                    "opening_audit_seconds": arguments.opening_audit_seconds,
                    "section_window_seconds": SECTION_WINDOW_SECONDS,
                    "section_window_hop_seconds": SECTION_WINDOW_HOP_SECONDS,
                    "full_audio_second_pass": False,
                    "large_v3_automatic": False,
                    "candidate_text_written_to_transcript": False,
                },
                "software": software,
                "model": model_information,
                "audio_seconds": audio_seconds,
                "audio_signal": audio_signal,
                "detected_language": detected_language,
                "baseline": transcript_indicators(
                    baseline_text, baseline_segments
                ),
                "opening_audit": opening_audit,
                "section_coverage_recovery": {
                    "section_count": len(section_plan),
                    "plan": section_plan,
                    "window_counts": window_counts,
                    "section_seconds": section_seconds,
                    "accepted_count": len(recovered_segments),
                    "accepted_segments": [
                        {
                            "start": segment.start,
                            "end": segment.end,
                            "text": segment.text,
                            "avg_logprob": segment.avg_logprob,
                            "source": segment.source,
                        }
                        for segment in recovered_segments
                    ],
                    "evidence": evidence,
                },
                "repeated_english_consensus": {
                    "correction_count": len(corrections),
                    "corrections": corrections,
                },
                "regression": regression,
                "final": final_indicators,
                "reference_accuracy": accuracy,
                "quality_gate": {
                    "status": quality_gate,
                    "acceptance": acceptance,
                    "is_accuracy_percentage": False,
                },
                "performance": {
                    "startup_seconds": startup_seconds,
                    "primary_seconds": primary_seconds,
                    "opening_audit_seconds": opening_seconds,
                    "section_coverage_planning_seconds": planning_seconds,
                    "section_coverage_recovery_seconds": recovery_seconds,
                    "section_seconds": section_seconds,
                    "warm_core_seconds": warm_core_seconds,
                    "total_seconds": total_seconds,
                    "total_realtime_factor": total_rtf,
                    "warm_core_realtime_factor": warm_rtf,
                    "audio_minutes_per_processing_minute_total": audio_seconds
                    / max(total_seconds, 0.001),
                    "audio_minutes_per_processing_minute_warm": audio_seconds
                    / max(warm_core_seconds, 0.001),
                    "performance_class": performance_class,
                },
                "timings": timer.steps,
            }
            write_json_atomically(outputs.metrics, metrics)

            print("TRANSCRIPTION_RESULT=PASS")
            print(f"OUTPUT_FILE={outputs.transcript}")
            print(f"LOG_FILE={outputs.log}")
            print(f"METRICS_FILE={outputs.metrics}")
            print(f"STARTUP_SECONDS={startup_seconds:.3f}")
            print(f"PRIMARY_SECONDS={primary_seconds:.3f}")
            print(f"OPENING_AUDIT_SECONDS={opening_seconds:.3f}")
            print(f"SECTION_COVERAGE_PLANNING_SECONDS={planning_seconds:.3f}")
            print(f"SECTION_COVERAGE_RECOVERY_SECONDS={recovery_seconds:.3f}")
            print(f"WARM_CORE_SECONDS={warm_core_seconds:.3f}")
            print(f"TOTAL_SECONDS={total_seconds:.3f}")
            print(f"PERFORMANCE_CLASS={performance_class}")
            print(f"SECTION_RECOVERED_SEGMENTS={len(recovered_segments)}")
            print(f"CONSENSUS_CORRECTIONS_APPLIED={len(corrections)}")
            print(f"REGRESSION_STATUS={regression.get('status')}")
            print(f"QUALITY_GATE={quality_gate}")
            print(f"TRANSACTION_1_ACCEPTANCE={acceptance}")
            print("FULL_AUDIO_SECOND_PASS_USED=NO")
            print("LARGE_V3_USED=NO")
            print("CANDIDATE_TEXT_WRITTEN_TO_TRANSCRIPT=NO")
            print("EDUCATIONAL_FORMATTING_APPLIED=NO")
            overall_result = "PASS"
            return 0
        except KeyboardInterrupt:
            timer.fail_current()
            overall_result = "CANCELLED"
            print("TRANSCRIPTION_RESULT=CANCELLED", file=sys.stderr)
            return 130
        except Exception as exception:
            timer.fail_current()
            print("TRANSCRIPTION_RESULT=FAILED", file=sys.stderr)
            print(f"ERROR={type(exception).__name__}: {exception}", file=sys.stderr)
            print(f"OUTPUT_FILE_NOT_CREATED={outputs.transcript}", file=sys.stderr)
            overall_result = "FAILED"
            return 1
        finally:
            timer.print_summary(overall_result)


def main() -> int:
    script_directory = Path(__file__).resolve().parent
    ensure_stt_virtual_environment(script_directory)
    return run_transcription()


if __name__ == "__main__":
    raise SystemExit(main())
