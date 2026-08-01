from __future__ import annotations

import argparse
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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, TextIO


DEVICE = "cuda"
COMPUTE_TYPE = "float16"
SAMPLE_RATE = 16000
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_MAX_REPAIR_WINDOWS = 5
PRIMARY_WINDOW_SECONDS = 29.5
PRIMARY_WINDOW_OVERLAP_SECONDS = 2.5
REPAIR_PADDING_SECONDS = 4.0
MIN_REPAIR_COVERAGE_RATIO = 0.88
MAX_REPAIR_COVERAGE_RATIO = 1.70
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
    "Miller, Theo, Jennifer, ミラー, セオ, ジェニファー, "
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish, 仮定法, 目的格, 不定詞, 意味上の主語, "
    "現在進行形, 説明型オーバーラッピング, 接近のニュアンス, "
    "控えめな提案, come out, not exactly, what if, for us, be at this"
)

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Grammar and Vocabulary": (
        "Grammar and Vocabulary",
        "文法と語彙",
    ),
    "Essential Expressions": (
        "Essential Expressions",
        "重要表現",
        "エッセンシャル・エクスプレッションズ",
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


@dataclass(frozen=True)
class PrimaryWindow:
    index: int
    start: float
    end: float
    keep_start: float
    keep_end: float


@dataclass
class WordRecord:
    start: float
    end: float
    text: str
    probability: float


@dataclass
class SegmentRecord:
    start: float
    end: float
    text: str
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float
    source: str
    words: list[WordRecord] = field(default_factory=list)


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
    severity: float
    reasons: tuple[str, ...]
    segment_indexes: tuple[int, ...]


@dataclass
class RepairDecision:
    window_index: int
    accepted: bool
    reason: str
    baseline_score: float
    repair_score: float
    coverage_ratio: float
    baseline_suspicious_events: int
    repair_suspicious_events: int
    baseline_section_hits: int
    repair_section_hits: int
    baseline_anchor_hits: int
    repair_anchor_hits: int
    baseline_text: str
    repair_text: str
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
            "Create a faithful full transcript with a no-VAD full-coverage Turbo "
            "pass and a small number of guarded large-v3 repairs."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--language", default="auto")
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
    max_repair_windows: int,
    ctranslate2: object,
) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")
    if max_repair_windows < 0 or max_repair_windows > 12:
        raise ValueError("--max-repair-windows must be between 0 and 12.")
    if ctranslate2.get_cuda_device_count() < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")

    supported = ctranslate2.get_supported_compute_types("cuda")
    if COMPUTE_TYPE not in supported:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is not supported. Supported types: {sorted(supported)}"
        )


def build_primary_windows(audio_seconds: float) -> list[PrimaryWindow]:
    if audio_seconds <= 0:
        raise TranscriptionError("The decoded audio has zero duration.")

    step = PRIMARY_WINDOW_SECONDS - PRIMARY_WINDOW_OVERLAP_SECONDS
    raw: list[tuple[float, float]] = []
    start = 0.0
    while start < audio_seconds:
        end = min(audio_seconds, start + PRIMARY_WINDOW_SECONDS)
        raw.append((start, end))
        if end >= audio_seconds:
            break
        start += step

    windows: list[PrimaryWindow] = []
    for index, (start, end) in enumerate(raw):
        keep_start = 0.0 if index == 0 else (start + raw[index - 1][1]) / 2.0
        keep_end = (
            audio_seconds
            if index == len(raw) - 1
            else (end + raw[index + 1][0]) / 2.0
        )
        windows.append(
            PrimaryWindow(
                index=index,
                start=start,
                end=end,
                keep_start=keep_start,
                keep_end=keep_end,
            )
        )
    return windows


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


def word_midpoint(word: WordRecord) -> float:
    return (word.start + word.end) / 2.0


def build_segment_from_words(
    original: object,
    words: list[WordRecord],
    source: str,
) -> SegmentRecord | None:
    if not words:
        return None
    text = clean_text("".join(word.text for word in words))
    if not text:
        return None
    return SegmentRecord(
        start=words[0].start,
        end=words[-1].end,
        text=text,
        avg_logprob=float(getattr(original, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(original, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(original, "no_speech_prob", 0.0)),
        source=source,
        words=words,
    )


def transcribe_primary_window(
    model: object,
    audio: object,
    window: PrimaryWindow,
    language: str | None,
) -> tuple[list[SegmentRecord], str]:
    start_sample = int(window.start * SAMPLE_RATE)
    end_sample = int(window.end * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]

    iterator, information = model.transcribe(
        audio_slice,
        task="transcribe",
        language=language,
        multilingual=True,
        beam_size=3,
        best_of=3,
        patience=1.0,
        temperature=(0.0, 0.2),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=True,
        hotwords=TRANSCRIPTION_GLOSSARY,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        log_progress=False,
    )

    records: list[SegmentRecord] = []
    for segment in iterator:
        absolute_words: list[WordRecord] = []
        for word in getattr(segment, "words", []) or []:
            absolute_word = WordRecord(
                start=max(0.0, float(getattr(word, "start", 0.0)) + window.start),
                end=max(0.0, float(getattr(word, "end", 0.0)) + window.start),
                text=str(getattr(word, "word", "")),
                probability=float(getattr(word, "probability", 0.0)),
            )
            midpoint = word_midpoint(absolute_word)
            if midpoint < window.keep_start:
                continue
            if midpoint >= window.keep_end and window.index != -1:
                continue
            absolute_words.append(absolute_word)

        record = build_segment_from_words(segment, absolute_words, "turbo")
        if record is not None:
            records.append(record)

    detected_language = str(getattr(information, "language", "unknown"))
    return records, detected_language


def transcribe_primary_full_coverage(
    model: object,
    audio: object,
    windows: list[PrimaryWindow],
    language: str | None,
) -> tuple[list[SegmentRecord], Counter[str]]:
    records: list[SegmentRecord] = []
    language_counts: Counter[str] = Counter()

    for position, window in enumerate(windows, start=1):
        print(
            f"PRIMARY_WINDOW={position}/{len(windows)} "
            f"INDEX={window.index:03d} START={window.start:.3f} "
            f"END={window.end:.3f} KEEP_START={window.keep_start:.3f} "
            f"KEEP_END={window.keep_end:.3f}"
        )
        window_records, detected_language = transcribe_primary_window(
            model,
            audio,
            window,
            language,
        )
        records.extend(window_records)
        language_counts[detected_language] += 1

    records.sort(key=lambda item: (item.start, item.end))
    if not records:
        raise TranscriptionError("The primary model returned no transcript segments.")
    return records, language_counts


def normalize_for_comparison(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龯々]+", "", text.lower())


def english_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


def count_unicode_scripts(text: str) -> dict[str, int]:
    counts = {
        "latin": 0,
        "japanese_kana": 0,
        "cjk": 0,
        "hangul": 0,
        "cyrillic": 0,
        "arabic": 0,
        "devanagari": 0,
    }
    for character in text:
        code = ord(character)
        if (0x0041 <= code <= 0x024F):
            counts["latin"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["japanese_kana"] += 1
        elif 0x3400 <= code <= 0x9FFF:
            counts["cjk"] += 1
        elif 0xAC00 <= code <= 0xD7AF:
            counts["hangul"] += 1
        elif 0x0400 <= code <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x0600 <= code <= 0x06FF:
            counts["arabic"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["devanagari"] += 1
    return counts


def detect_decoder_loop(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    for unit_length in range(2, 17):
        if re.search(rf"(.{{{unit_length}}})\1{{8,}}", compact):
            return True
    return False


def section_hits(text: str) -> dict[str, bool]:
    lower_text = text.lower()
    return {
        canonical: any(alias.lower() in lower_text for alias in aliases)
        for canonical, aliases in SECTION_ALIASES.items()
    }


def split_english_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for piece in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        piece = clean_text(piece)
        word_count = len(english_words(piece))
        if 5 <= word_count <= 26 and latin_character_count(piece) >= 18:
            candidates.append(piece)
    return candidates


def discover_repeated_english_anchors(segments: Iterable[SegmentRecord]) -> list[str]:
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
        if best_index is not None and best_ratio >= 0.80:
            clusters[best_index].append(candidate)
        else:
            clusters.append([candidate])

    anchors: list[tuple[int, str]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        representative = max(cluster, key=lambda value: len(value))
        anchors.append((len(cluster), representative))
    anchors.sort(key=lambda item: (-item[0], -len(item[1])))
    return [text for _count, text in anchors[:8]]


def anchor_exact_hits(text: str, anchors: Iterable[str]) -> int:
    normalized_text = normalize_for_comparison(text)
    return sum(
        normalized_text.count(normalize_for_comparison(anchor))
        for anchor in anchors
        if normalize_for_comparison(anchor)
    )


def script_profile(text: str) -> tuple[int, int]:
    return japanese_character_count(text), latin_character_count(text)


def local_suspicion_count(text: str, anchors: Iterable[str]) -> int:
    count = 0
    if not text.strip():
        count += 2
    if "�" in text:
        count += 2
    if detect_decoder_loop(text):
        count += 3
    normalized = normalize_for_comparison(text)
    for anchor in anchors:
        anchor_normalized = normalize_for_comparison(anchor)
        if not anchor_normalized or not normalized:
            continue
        ratio = SequenceMatcher(None, normalized, anchor_normalized).ratio()
        if 0.58 <= ratio < 0.88:
            count += 1
            break
    return count


def detect_suspicion_events(
    segments: list[SegmentRecord],
    anchors: list[str],
) -> list[SuspicionEvent]:
    events: list[SuspicionEvent] = []

    for index, segment in enumerate(segments):
        duration = max(segment.end - segment.start, 0.1)
        japanese, latin = script_profile(segment.text)
        words = len(english_words(segment.text))

        if segment.avg_logprob < -0.72:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    2.2,
                    "low_logprob",
                    (index,),
                )
            )
        if segment.compression_ratio > 2.35:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    2.3,
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
        if duration >= 3.0 and len(segment.text) / duration < 1.2:
            events.append(
                SuspicionEvent(
                    segment.start,
                    segment.end,
                    1.8,
                    "low_text_rate",
                    (index,),
                )
            )

        previous = segments[index - 1] if index > 0 else None
        following = segments[index + 1] if index + 1 < len(segments) else None
        if previous is not None and following is not None:
            previous_japanese, previous_latin = script_profile(previous.text)
            following_japanese, following_latin = script_profile(following.text)
            english_island = (
                japanese == 0
                and 1 <= words <= 6
                and previous_japanese >= 6
                and following_japanese >= 6
            )
            japanese_island = (
                latin == 0
                and 1 <= japanese <= 8
                and previous_latin >= 18
                and following_latin >= 18
            )
            if english_island or japanese_island:
                events.append(
                    SuspicionEvent(
                        segment.start,
                        segment.end,
                        2.4,
                        "isolated_script_switch",
                        (index,),
                    )
                )

        normalized = normalize_for_comparison(segment.text)
        for anchor in anchors:
            anchor_normalized = normalize_for_comparison(anchor)
            if not normalized or not anchor_normalized:
                continue
            ratio = SequenceMatcher(None, normalized, anchor_normalized).ratio()
            if 0.58 <= ratio < 0.88:
                events.append(
                    SuspicionEvent(
                        segment.start,
                        segment.end,
                        3.2,
                        "repeated_phrase_inconsistency",
                        (index,),
                    )
                )
                break

    return events


def merge_repair_events(
    events: list[SuspicionEvent],
    segments: list[SegmentRecord],
    audio_seconds: float,
    max_windows: int,
) -> list[RepairWindow]:
    if max_windows == 0:
        return []

    grouped: list[list[SuspicionEvent]] = []
    for event in sorted(events, key=lambda item: (item.start, item.end)):
        if not grouped or event.start > max(item.end for item in grouped[-1]) + 1.0:
            grouped.append([event])
        else:
            grouped[-1].append(event)

    windows: list[RepairWindow] = []
    for group in grouped:
        indexes = sorted(
            {
                index
                for event in group
                for index in event.segment_indexes
            }
        )
        if not indexes:
            continue

        core_start = min(segments[index].start for index in indexes)
        core_end = max(segments[index].end for index in indexes)
        reasons = tuple(sorted({event.reason for event in group}))
        severity = sum(event.severity for event in group)
        windows.append(
            RepairWindow(
                index=0,
                core_start=core_start,
                core_end=core_end,
                slice_start=max(0.0, core_start - REPAIR_PADDING_SECONDS),
                slice_end=min(audio_seconds, core_end + REPAIR_PADDING_SECONDS),
                severity=severity,
                reasons=reasons,
                segment_indexes=tuple(indexes),
            )
        )

    windows.sort(key=lambda item: (-item.severity, item.core_start))
    selected = windows[:max_windows]
    selected.sort(key=lambda item: item.core_start)
    return [
        RepairWindow(
            index=index,
            core_start=window.core_start,
            core_end=window.core_end,
            slice_start=window.slice_start,
            slice_end=window.slice_end,
            severity=window.severity,
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
    selected: list[SegmentRecord] = []
    for segment in segments:
        midpoint = (segment.start + segment.end) / 2.0
        if core_start <= midpoint <= core_end:
            selected.append(segment)
    return selected


def join_segment_text(segments: Iterable[SegmentRecord]) -> str:
    return clean_text(" ".join(segment.text for segment in segments))


def transcribe_repair_window(
    model: object,
    audio: object,
    window: RepairWindow,
    language: str | None,
) -> list[SegmentRecord]:
    start_sample = int(window.slice_start * SAMPLE_RATE)
    end_sample = int(window.slice_end * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]

    iterator, _information = model.transcribe(
        audio_slice,
        task="transcribe",
        language=language,
        multilingual=True,
        beam_size=5,
        best_of=5,
        patience=1.0,
        temperature=(0.0, 0.2, 0.4),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=True,
        hotwords=TRANSCRIPTION_GLOSSARY,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        log_progress=False,
    )

    records: list[SegmentRecord] = []
    for segment in iterator:
        words: list[WordRecord] = []
        for word in getattr(segment, "words", []) or []:
            absolute_word = WordRecord(
                start=max(0.0, float(getattr(word, "start", 0.0)) + window.slice_start),
                end=max(0.0, float(getattr(word, "end", 0.0)) + window.slice_start),
                text=str(getattr(word, "word", "")),
                probability=float(getattr(word, "probability", 0.0)),
            )
            midpoint = word_midpoint(absolute_word)
            if window.core_start <= midpoint <= window.core_end:
                words.append(absolute_word)
        record = build_segment_from_words(segment, words, "large_v3")
        if record is not None:
            records.append(record)
    return records


def average_logprob(segments: Iterable[SegmentRecord]) -> float:
    values = [segment.avg_logprob for segment in segments]
    return sum(values) / len(values) if values else -10.0


def quality_score(
    text: str,
    segments: list[SegmentRecord],
    anchors: list[str],
) -> float:
    confidence = max(0.0, min(1.0, (average_logprob(segments) + 1.2) / 1.2))
    length_score = min(len(text) / 180.0, 1.0)
    suspicion_penalty = min(local_suspicion_count(text, anchors) * 0.18, 0.72)
    return max(0.0, confidence * 0.72 + length_score * 0.28 - suspicion_penalty)


def decide_repair(
    window: RepairWindow,
    baseline_segments: list[SegmentRecord],
    repair_segments: list[SegmentRecord],
    anchors: list[str],
) -> RepairDecision:
    baseline_text = join_segment_text(baseline_segments)
    repair_text = join_segment_text(repair_segments)
    baseline_score = quality_score(baseline_text, baseline_segments, anchors)
    repair_score = quality_score(repair_text, repair_segments, anchors)
    coverage_ratio = len(repair_text) / max(len(baseline_text), 1)
    baseline_suspicious = local_suspicion_count(baseline_text, anchors)
    repair_suspicious = local_suspicion_count(repair_text, anchors)
    baseline_sections = sum(section_hits(baseline_text).values())
    repair_sections = sum(section_hits(repair_text).values())
    baseline_anchors = anchor_exact_hits(baseline_text, anchors)
    repair_anchors = anchor_exact_hits(repair_text, anchors)

    accepted = False
    reason = "baseline_retained"

    coverage_ok = (
        MIN_REPAIR_COVERAGE_RATIO
        <= coverage_ratio
        <= MAX_REPAIR_COVERAGE_RATIO
    )
    content_guards_ok = (
        repair_sections >= baseline_sections
        and repair_anchors >= baseline_anchors
    )
    clear_improvement = (
        repair_suspicious < baseline_suspicious
        or repair_score >= baseline_score + 0.12
    )

    if not repair_text:
        reason = "repair_empty"
    elif not coverage_ok:
        reason = "coverage_guard_rejected"
    elif not content_guards_ok:
        reason = "content_guard_rejected"
    elif clear_improvement:
        accepted = True
        reason = "guarded_quality_improvement"
    elif repair_score > baseline_score + 0.05 and coverage_ratio >= 0.95:
        accepted = True
        reason = "small_quality_improvement_with_full_coverage"

    return RepairDecision(
        window_index=window.index,
        accepted=accepted,
        reason=reason,
        baseline_score=baseline_score,
        repair_score=repair_score,
        coverage_ratio=coverage_ratio,
        baseline_suspicious_events=baseline_suspicious,
        repair_suspicious_events=repair_suspicious,
        baseline_section_hits=baseline_sections,
        repair_section_hits=repair_sections,
        baseline_anchor_hits=baseline_anchors,
        repair_anchor_hits=repair_anchors,
        baseline_text=baseline_text,
        repair_text=repair_text,
        core_start=window.core_start,
        core_end=window.core_end,
        slice_start=window.slice_start,
        slice_end=window.slice_end,
        reasons=window.reasons,
    )


def apply_repair(
    segments: list[SegmentRecord],
    window: RepairWindow,
    replacement: list[SegmentRecord],
) -> list[SegmentRecord]:
    retained: list[SegmentRecord] = []
    for segment in segments:
        midpoint = (segment.start + segment.end) / 2.0
        if window.core_start <= midpoint <= window.core_end:
            continue
        retained.append(segment)
    retained.extend(replacement)
    retained.sort(key=lambda item: (item.start, item.end))
    return retained


def format_transcript(segments: Iterable[SegmentRecord]) -> str:
    lines: list[str] = []
    previous_end: float | None = None

    for segment in segments:
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


def transcript_indicators(
    text: str,
    segments: list[SegmentRecord],
    events: list[SuspicionEvent],
    anchors: list[str],
) -> dict[str, object]:
    reasons = Counter(event.reason for event in events)
    sections = section_hits(text)
    return {
        "text_characters": len(text),
        "segment_count": len(segments),
        "japanese_characters": japanese_character_count(text),
        "latin_characters": latin_character_count(text),
        "script_counts": count_unicode_scripts(text),
        "replacement_characters": text.count("�"),
        "decoder_loop": detect_decoder_loop(text),
        "section_hits": sections,
        "section_count": sum(sections.values()),
        "anchor_exact_hits": anchor_exact_hits(text, anchors),
        "suspicious_event_count": len(events),
        "suspicious_reason_counts": dict(reasons),
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


def decision_to_dict(decision: RepairDecision) -> dict[str, object]:
    return {
        "window_index": decision.window_index,
        "accepted": decision.accepted,
        "decision_reason": decision.reason,
        "baseline_score": round(decision.baseline_score, 6),
        "repair_score": round(decision.repair_score, 6),
        "coverage_ratio": round(decision.coverage_ratio, 6),
        "baseline_suspicious_events": decision.baseline_suspicious_events,
        "repair_suspicious_events": decision.repair_suspicious_events,
        "baseline_section_hits": decision.baseline_section_hits,
        "repair_section_hits": decision.repair_section_hits,
        "baseline_anchor_hits": decision.baseline_anchor_hits,
        "repair_anchor_hits": decision.repair_anchor_hits,
        "baseline_text": decision.baseline_text,
        "repair_text": decision.repair_text,
        "core_start": round(decision.core_start, 3),
        "core_end": round(decision.core_end, 3),
        "slice_start": round(decision.slice_start, 3),
        "slice_end": round(decision.slice_end, 3),
        "reasons": list(decision.reasons),
    }


def run_child() -> int:
    timer = StepTimer()
    overall_result = "FAILED"
    metrics: dict[str, object] = {}

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
            from faster_whisper import WhisperModel
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
            print("REPAIR_MODEL_DOWNLOAD_POLICY=DOWNLOAD_ONLY_IF_REPAIR_WINDOWS_EXIST")
            print("MODEL_VERSION_CHECK_RESULT=PASS")
            timer.finish()

            timer.start("validate_environment")
            validate_environment(
                input_file,
                arguments.max_repair_windows,
                ctranslate2,
            )
            if reference_file is not None and not reference_file.is_file():
                raise FileNotFoundError(f"Reference file was not found: {reference_file}")
            timer.finish()

            print("TRANSCRIPTION_CONFIGURATION")
            print("TRANSCRIPTION_MODE=FULL_COVERAGE_TURBO_GUARDED_REPAIR")
            print("PRIMARY_AUDIO_COVERAGE=NO_VAD_OVERLAPPED_WINDOWS")
            print("PRIMARY_BOUNDARY_POLICY=WORD_TIMESTAMP_CORE_OWNERSHIP")
            print("REPAIR_BOUNDARY_POLICY=WHOLE_BASELINE_SEGMENT_CORE")
            print("REPAIR_ACCEPTANCE_POLICY=COVERAGE_AND_CONTENT_GUARDS")
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
            print(f"PRIMARY_WINDOW_SECONDS={PRIMARY_WINDOW_SECONDS:.1f}")
            print(
                f"PRIMARY_WINDOW_OVERLAP_SECONDS="
                f"{PRIMARY_WINDOW_OVERLAP_SECONDS:.1f}"
            )
            print(f"REPAIR_PADDING_SECONDS={REPAIR_PADDING_SECONDS:.1f}")
            print("PRIMARY_MODEL=large-v3-turbo")
            print("REPAIR_MODEL=large-v3")
            print("JAPANESE_SPECIALIST_MODEL=DISABLED_PENDING_VALID_BENCHMARK")
            print(f"INPUT={input_file}")
            print(f"REFERENCE={reference_file or 'NONE'}")

            timer.start("decode_audio")
            audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
            audio_seconds = len(audio) / SAMPLE_RATE
            primary_windows = build_primary_windows(audio_seconds)
            timer.finish()

            print(f"AUDIO_SECONDS={audio_seconds:.3f}")
            print(f"PRIMARY_WINDOW_COUNT={len(primary_windows)}")

            timer.start("primary_full_coverage_transcription")
            primary_model = load_model(
                WhisperModel,
                Path(str(primary_model_information["directory"])),
                "turbo",
            )
            requested_language = None if arguments.language == "auto" else arguments.language
            baseline_segments, detected_languages = transcribe_primary_full_coverage(
                primary_model,
                audio,
                primary_windows,
                requested_language,
            )
            del primary_model
            gc.collect()
            baseline_text = format_transcript(baseline_segments)
            timer.finish()

            timer.start("detect_repair_windows")
            anchors = discover_repeated_english_anchors(baseline_segments)
            baseline_events = detect_suspicion_events(baseline_segments, anchors)
            repair_windows = merge_repair_events(
                baseline_events,
                baseline_segments,
                audio_seconds,
                arguments.max_repair_windows,
            )
            baseline_indicators = transcript_indicators(
                baseline_text,
                baseline_segments,
                baseline_events,
                anchors,
            )
            timer.finish()

            print(f"BASELINE_SEGMENT_COUNT={len(baseline_segments)}")
            print(f"BASELINE_TEXT_CHARACTERS={len(baseline_text)}")
            print(f"DETECTED_LANGUAGE_COUNTS={json.dumps(detected_languages, sort_keys=True)}")
            print(f"REPEATED_PHRASE_ANCHORS={json.dumps(anchors, ensure_ascii=False)}")
            print(f"REPAIR_EVENT_COUNT={len(baseline_events)}")
            print(
                "REPAIR_EVENT_REASONS="
                + json.dumps(
                    Counter(event.reason for event in baseline_events),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            print(f"REPAIR_WINDOW_COUNT={len(repair_windows)}")
            for window in repair_windows:
                print(
                    f"REPAIR_WINDOW INDEX={window.index:02d} "
                    f"SLICE_START={window.slice_start:.3f} "
                    f"SLICE_END={window.slice_end:.3f} "
                    f"CORE_START={window.core_start:.3f} "
                    f"CORE_END={window.core_end:.3f} "
                    f"SEVERITY={window.severity:.2f} "
                    f"REASONS={','.join(window.reasons)}"
                )

            timer.start("selective_large_v3_repairs")
            final_segments = list(baseline_segments)
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
                    repair_core = transcribe_repair_window(
                        repair_model,
                        audio,
                        window,
                        requested_language,
                    )
                    decision = decide_repair(
                        window,
                        baseline_core,
                        repair_core,
                        anchors,
                    )
                    repair_decisions.append(decision)
                    print(
                        f"SELECTIVE_REPAIR_DECISION INDEX={window.index:02d} "
                        f"ACCEPTED={'YES' if decision.accepted else 'NO'} "
                        f"BASELINE_SCORE={decision.baseline_score:.3f} "
                        f"REPAIR_SCORE={decision.repair_score:.3f} "
                        f"COVERAGE_RATIO={decision.coverage_ratio:.3f} "
                        f"BASELINE_SUSPICIOUS={decision.baseline_suspicious_events} "
                        f"REPAIR_SUSPICIOUS={decision.repair_suspicious_events} "
                        f"REASON={decision.reason}"
                    )
                    if decision.accepted:
                        final_segments = apply_repair(
                            final_segments,
                            window,
                            repair_core,
                        )
                del repair_model
                gc.collect()
            timer.finish()

            timer.start("assemble_final_transcript")
            final_segments.sort(key=lambda item: (item.start, item.end))
            final_text = format_transcript(final_segments)
            final_events = detect_suspicion_events(final_segments, anchors)
            final_indicators = transcript_indicators(
                final_text,
                final_segments,
                final_events,
                anchors,
            )
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

            timer.start("write_output_files")
            write_text_atomically(output_paths.transcript, final_text)

            baseline_seconds = timer.seconds_for(
                "primary_full_coverage_transcription"
            )
            repair_seconds = timer.seconds_for("selective_large_v3_repairs")
            accepted_count = sum(decision.accepted for decision in repair_decisions)
            total_seconds = timer.total_seconds
            automatic_reliability_index = max(
                0.0,
                min(
                    100.0,
                    100.0
                    - len(final_events) * 6.0
                    - (4 - int(final_indicators["section_count"])) * 4.0
                    - (12.0 if bool(final_indicators["decoder_loop"]) else 0.0)
                    - int(final_indicators["replacement_characters"]) * 4.0,
                ),
            )

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
                    "mode": "full_coverage_turbo_guarded_repair",
                    "language": arguments.language,
                    "max_repair_windows": arguments.max_repair_windows,
                    "primary_window_seconds": PRIMARY_WINDOW_SECONDS,
                    "primary_window_overlap_seconds": PRIMARY_WINDOW_OVERLAP_SECONDS,
                    "repair_padding_seconds": REPAIR_PADDING_SECONDS,
                    "primary_boundary_policy": "word_timestamp_core_ownership",
                    "repair_boundary_policy": "whole_baseline_segment_core",
                    "repair_acceptance_policy": "coverage_and_content_guards",
                },
                "software": software_metrics,
                "models": {
                    "primary": primary_model_information,
                    "repair": repair_model_information,
                    "repair_model_updated_during_run": repair_model_updated,
                    "kotoba_specialist_status": "disabled_pending_valid_benchmark",
                },
                "audio_seconds": audio_seconds,
                "primary_window_count": len(primary_windows),
                "detected_language_counts": dict(detected_languages),
                "repeated_phrase_anchors": anchors,
                "baseline": baseline_indicators,
                "final": final_indicators,
                "repair_detection": {
                    "event_count": len(baseline_events),
                    "reason_counts": dict(
                        Counter(event.reason for event in baseline_events)
                    ),
                    "window_count": len(repair_windows),
                    "windows": [
                        {
                            "index": window.index,
                            "core_start": round(window.core_start, 3),
                            "core_end": round(window.core_end, 3),
                            "slice_start": round(window.slice_start, 3),
                            "slice_end": round(window.slice_end, 3),
                            "severity": round(window.severity, 3),
                            "reasons": list(window.reasons),
                            "segment_indexes": list(window.segment_indexes),
                        }
                        for window in repair_windows
                    ],
                },
                "repair_decisions": [
                    decision_to_dict(decision)
                    for decision in repair_decisions
                ],
                "accepted_repair_count": accepted_count,
                "reference_accuracy": reference_metrics,
                "automatic_quality": {
                    "scope": "STRUCTURAL_COVERAGE_AND_CROSS_PASS_ONLY",
                    "not_an_accuracy_percentage": True,
                    "reliability_index": automatic_reliability_index,
                },
                "performance": {
                    "baseline_seconds": baseline_seconds,
                    "selective_repair_seconds": repair_seconds,
                    "total_seconds": total_seconds,
                    "baseline_realtime_factor": baseline_seconds / audio_seconds,
                    "total_realtime_factor": total_seconds / audio_seconds,
                    "audio_minutes_per_processing_minute": audio_seconds / max(total_seconds, 0.001),
                    "repair_seconds_per_processed_window": (
                        repair_seconds / len(repair_windows)
                        if repair_windows
                        else 0.0
                    ),
                    "accepted_repairs_per_repair_minute": (
                        accepted_count / max(repair_seconds / 60.0, 0.001)
                        if repair_windows
                        else 0.0
                    ),
                },
                "timings": timer.steps,
            }
            write_json_atomically(output_paths.metrics, metrics)
            timer.finish()

            total_seconds = timer.total_seconds
            baseline_seconds = timer.seconds_for(
                "primary_full_coverage_transcription"
            )
            repair_seconds = timer.seconds_for("selective_large_v3_repairs")
            accepted_count = sum(decision.accepted for decision in repair_decisions)

            print("TRANSCRIPTION_RESULT=PASS")
            print(f"OUTPUT_FILE={output_paths.transcript}")
            print(f"LOG_FILE={output_paths.log}")
            print(f"METRICS_FILE={output_paths.metrics}")
            print(f"BASELINE_SECONDS={baseline_seconds:.3f}")
            print(f"SELECTIVE_REPAIR_SECONDS={repair_seconds:.3f}")
            print(f"TOTAL_SECONDS={total_seconds:.3f}")
            print(f"BASELINE_REALTIME_FACTOR={baseline_seconds / audio_seconds:.4f}")
            print(f"TOTAL_PROCESS_REALTIME_FACTOR={total_seconds / audio_seconds:.4f}")
            print(f"REPAIR_WINDOWS_PROCESSED={len(repair_windows)}")
            print(f"REPAIR_WINDOWS_ACCEPTED={accepted_count}")
            print(f"BASELINE_SUSPICIOUS_EVENTS={len(baseline_events)}")
            print(f"FINAL_SUSPICIOUS_EVENTS={len(final_events)}")
            print(f"BASELINE_SECTION_COUNT={baseline_indicators['section_count']}/4")
            print(f"FINAL_SECTION_COUNT={final_indicators['section_count']}/4")
            print(
                f"BASELINE_ANCHOR_EXACT_HITS="
                f"{baseline_indicators['anchor_exact_hits']}"
            )
            print(
                f"FINAL_ANCHOR_EXACT_HITS="
                f"{final_indicators['anchor_exact_hits']}"
            )
            print(f"REFERENCE_ACCURACY={reference_metrics['status']}")
            print("AUTOMATIC_QUALITY_SCOPE=STRUCTURAL_COVERAGE_AND_CROSS_PASS_ONLY")
            print(f"AUTOMATIC_RELIABILITY_INDEX={automatic_reliability_index:.1f}")
            print("AUTOMATIC_RELIABILITY_INDEX_IS_ACCURACY_PERCENTAGE=NO")
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
    return run_child()


if __name__ == "__main__":
    raise SystemExit(main())
