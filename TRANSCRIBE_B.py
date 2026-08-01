from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable


MODEL_ALIAS = "turbo"
MODEL_REPOSITORY = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_DIRECTORY_NAME = "faster-whisper-large-v3-turbo"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_BATCH_SIZE = 8
OPENING_CONTEXT_SECONDS = 210
OPENING_WINDOW_SECONDS = 28
OPENING_OVERLAP_SECONDS = 6
MAX_LANGUAGE_REPAIRS = 24
SAMPLE_RATE = 16000
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
    "vocabulary.*",
)

MODEL_REQUIRED_FILES = (
    "config.json",
    "model.bin",
    "tokenizer.json",
)

FIXED_HOTWORDS = (
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish, 仮定法, 目的格, 不定詞, 意味上の主語, 節, "
    "現在進行形, 説明型オーバーラッピング, 接近のニュアンス, "
    "強さを減じる, 控えめな提案"
)

KNOWN_HALLUCINATION_PATTERNS = (
    r"English Subtitles by the Amara\.org community",
    r"Subtitles by the Amara\.org community",
    r"日本語の解説と英語のダイアログ.{0,120}文字起こし",
    r"同じ英語の繰り返しも残し(?:てください)?(?:[、, ]*同じ英語の繰り返しも残し(?:てください)?)*",
    r"英語の繰り返しと英語の繰り返しも残し.{0,120}",
)

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
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
        "プラクティカルユーセージ",
        "プラクティカル・ユーセージ",
    ),
    "Pronunciation Polish": (
        "Pronunciation Polish",
        "プロナンシエーション・ポリッシュ",
        "発音練習",
    ),
}


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


class StepTimer:
    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.steps: list[tuple[str, float, str]] = []
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
        self.steps.append((self.current_name, elapsed, result))
        self.current_name = None
        self.current_started_at = None

    def fail_current(self) -> None:
        self.finish("FAILED")

    @property
    def total_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def step_seconds(self, name: str) -> float:
        for step_name, elapsed, _result in self.steps:
            if step_name == name:
                return elapsed
        return 0.0

    def print_summary(self, overall_result: str) -> None:
        if self.current_name is not None:
            self.finish("FAILED" if overall_result == "FAILED" else overall_result)

        print()
        print("PROCESS_TIME_SUMMARY")
        for index, (name, elapsed, result) in enumerate(self.steps, start=1):
            print(
                f"STEP_{index:02d}={name} | RESULT={result} | "
                f"ELAPSED_SECONDS={elapsed:.3f}"
            )
        print(f"TOTAL_ELAPSED_SECONDS={self.total_seconds:.3f}")
        print(f"OVERALL_RESULT={overall_result}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe a Japanese-English radio lesson locally after checking "
            "for newer transcription software and model files."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
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

    completed = subprocess.run(
        [str(expected_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(script_directory),
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
    result: dict[str, str | None] = {}
    for package in RUNTIME_PACKAGES:
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def check_and_update_runtime_packages(script_directory: Path) -> None:
    print("SOFTWARE_VERSION_CHECK_START")
    before = installed_package_versions()
    print(
        "SOFTWARE_VERSIONS_BEFORE="
        + json.dumps(before, ensure_ascii=False, sort_keys=True)
    )

    command = [
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
    ]
    completed = run_command(
        command,
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


def model_files_are_complete(model_directory: Path) -> bool:
    return all((model_directory / name).is_file() for name in MODEL_REQUIRED_FILES)


def check_and_update_model(script_directory: Path) -> tuple[Path, str, bool]:
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

    models_directory = script_directory / "models"
    model_directory = models_directory / MODEL_DIRECTORY_NAME
    revision_file = model_directory / ".model_revision.txt"

    print("MODEL_VERSION_CHECK_START")
    print(f"MODEL_REPOSITORY={MODEL_REPOSITORY}")
    print(f"MODEL_DIRECTORY={model_directory}")

    try:
        model_information = HfApi().model_info(
            repo_id=MODEL_REPOSITORY,
            revision="main",
            timeout=30,
        )
    except Exception as exception:
        raise TranscriptionError(
            "The latest model version could not be checked online. "
            f"{type(exception).__name__}: {exception}"
        ) from exception

    latest_revision = str(model_information.sha or "").strip()
    if not latest_revision:
        raise TranscriptionError("The model service returned no revision identifier.")

    installed_revision = ""
    if revision_file.is_file():
        installed_revision = revision_file.read_text(encoding="utf-8").strip()

    download_required = (
        installed_revision != latest_revision
        or not model_files_are_complete(model_directory)
    )

    print(f"MODEL_INSTALLED_REVISION={installed_revision or 'NONE'}")
    print(f"MODEL_LATEST_REVISION={latest_revision}")
    print(f"MODEL_DOWNLOAD_REQUIRED={'YES' if download_required else 'NO'}")

    if download_required:
        model_directory.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_download(
                repo_id=MODEL_REPOSITORY,
                revision=latest_revision,
                local_dir=str(model_directory),
                allow_patterns=list(MODEL_ALLOW_PATTERNS),
            )
        except Exception as exception:
            raise TranscriptionError(
                "The latest model files could not be downloaded. "
                f"{type(exception).__name__}: {exception}"
            ) from exception

        if not model_files_are_complete(model_directory):
            missing = [
                name
                for name in MODEL_REQUIRED_FILES
                if not (model_directory / name).is_file()
            ]
            raise TranscriptionError(
                "The downloaded model is incomplete. Missing: " + ", ".join(missing)
            )

        revision_file.write_text(latest_revision + "\n", encoding="utf-8")

    print("MODEL_VERSION_CHECK_RESULT=PASS")
    return model_directory, latest_revision, download_required


def create_output_path(output_directory: Path, created_at: datetime) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.astimezone(JST).strftime("%Y%m%d_%H%M%S")
    candidate = output_directory / f"{timestamp}.txt"
    sequence = 2

    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = output_directory / f"{timestamp}_{sequence:02d}.txt"
        sequence += 1

    return candidate


def validate_environment(
    input_file: Path,
    batch_size: int,
    ctranslate2: object,
) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

    if ctranslate2.get_cuda_device_count() < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")

    supported = ctranslate2.get_supported_compute_types("cuda")
    if COMPUTE_TYPE not in supported:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is not supported. Supported types: {sorted(supported)}"
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
        term in message
        for term in (
            "out of memory",
            "cuda_error_out_of_memory",
            "failed to allocate",
            "memory allocation",
        )
    )


def load_model(WhisperModel: object, model_directory: Path) -> object:
    print("MODEL_LOAD_START")
    print(f"MODEL_PATH={model_directory}")
    print(f"COMPUTE_TYPE={COMPUTE_TYPE}")

    model = WhisperModel(
        str(model_directory),
        device=DEVICE,
        device_index=0,
        compute_type=COMPUTE_TYPE,
        flash_attention=False,
        local_files_only=True,
    )

    print("FLASH_ATTENTION=False")
    print("MODEL_LOAD_COMPLETE")
    return model


def to_record(segment: object, offset: float = 0.0) -> SegmentRecord:
    start = max(0.0, float(getattr(segment, "start", 0.0)) + offset)
    end = max(start, float(getattr(segment, "end", start)) + offset)
    return SegmentRecord(
        start=start,
        end=end,
        text=str(getattr(segment, "text", "")).strip(),
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
    )


def normalize_for_comparison(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龯々]+", "", text.lower())


def normalize_english(text: str) -> str:
    return " ".join(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text.lower()))


def english_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)


def english_word_count(text: str) -> int:
    return len(english_words(text))


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


def average_logprob(records: Iterable[SegmentRecord]) -> float:
    values = [record.avg_logprob for record in records]
    return sum(values) / len(values) if values else -10.0


def record_quality(record: SegmentRecord) -> float:
    text_length_score = min(len(record.text), 220) / 1000.0
    language_score = 0.05 if japanese_character_count(record.text) > 0 else 0.0
    return record.avg_logprob + text_length_score + language_score


def core_repeated_phrase(text: str) -> str:
    match = re.search(r"\bwhat\s+if\b", text, flags=re.IGNORECASE)
    if match is not None and match.start() > 0:
        return text[match.start():].strip(" ,")
    return text.strip()


def extract_english_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for piece in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        piece = re.sub(r"\s+", " ", piece).strip(" -–—\t")
        words = english_word_count(piece)
        if words < 4 or words > 24:
            continue
        compact_length = len(re.sub(r"\s+", "", piece))
        if compact_length == 0:
            continue
        if latin_character_count(piece) / compact_length < 0.65:
            continue
        candidates.append(piece)
    return candidates


def extract_repeated_phrases(records: Iterable[SegmentRecord]) -> list[str]:
    candidates: list[str] = []
    for record in records:
        candidates.extend(extract_english_candidates(record.text))

    clusters: list[list[str]] = []
    for candidate in candidates:
        normalized = normalize_english(candidate)
        if not normalized:
            continue

        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None,
                normalized,
                normalize_english(cluster[0]),
            ).ratio()
            if ratio > best_ratio:
                best_index = index
                best_ratio = ratio

        if best_index is not None and best_ratio >= 0.82:
            clusters[best_index].append(candidate)
        else:
            clusters.append([candidate])

    repeated: list[tuple[int, str]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        representative = max(cluster, key=lambda value: len(normalize_english(value)))
        representative = core_repeated_phrase(representative)
        repeated.append((len(cluster), representative))

    repeated.sort(key=lambda item: (-item[0], -len(item[1])))
    return [phrase for _count, phrase in repeated[:8]]


def build_hotwords(repeated_phrases: Iterable[str]) -> str:
    values = [FIXED_HOTWORDS]
    values.extend(repeated_phrases)
    return ", ".join(values)[:900]


def are_probable_duplicates(left: SegmentRecord, right: SegmentRecord) -> bool:
    left_text = normalize_for_comparison(left.text)
    right_text = normalize_for_comparison(right.text)
    if not left_text or not right_text:
        return False

    overlap = min(left.end, right.end) - max(left.start, right.start)
    gap = max(0.0, max(left.start, right.start) - min(left.end, right.end))
    if overlap <= 0.05 and gap > 2.0:
        return False

    similarity = SequenceMatcher(None, left_text, right_text).ratio()
    contained = left_text in right_text or right_text in left_text
    return similarity >= 0.80 or contained


def deduplicate_nearby(records: Iterable[SegmentRecord]) -> list[SegmentRecord]:
    result: list[SegmentRecord] = []

    for current in sorted(records, key=lambda item: (item.start, item.end)):
        if not current.text.strip():
            continue

        duplicate_index: int | None = None
        for index in range(len(result) - 1, max(-1, len(result) - 5), -1):
            previous = result[index]
            if current.start - previous.end > 3.0:
                break
            if are_probable_duplicates(previous, current):
                duplicate_index = index
                break

        if duplicate_index is None:
            result.append(current)
            continue

        if record_quality(current) > record_quality(result[duplicate_index]):
            result[duplicate_index] = current

    return sorted(result, key=lambda item: (item.start, item.end))


def transcribe_window(
    model: object,
    audio: object,
    start_seconds: float,
    end_seconds: float,
    hotwords: str,
    language: str | None,
) -> list[SegmentRecord]:
    audio_duration = len(audio) / SAMPLE_RATE
    start_seconds = max(0.0, start_seconds)
    end_seconds = min(audio_duration, end_seconds)
    if end_seconds <= start_seconds:
        return []

    start_sample = int(start_seconds * SAMPLE_RATE)
    end_sample = int(end_seconds * SAMPLE_RATE)
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
        word_timestamps=False,
        hotwords=hotwords,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        log_progress=False,
    )

    return [to_record(segment, start_seconds) for segment in iterator]


def transcribe_opening_context(
    model: object,
    audio: object,
    end_seconds: float,
    hotwords: str,
    language: str | None,
) -> list[SegmentRecord]:
    records: list[SegmentRecord] = []
    current = 0.0
    step = OPENING_WINDOW_SECONDS - OPENING_OVERLAP_SECONDS

    while current < end_seconds:
        window_end = min(end_seconds, current + OPENING_WINDOW_SECONDS)
        records.extend(
            transcribe_window(
                model=model,
                audio=audio,
                start_seconds=current,
                end_seconds=window_end,
                hotwords=hotwords,
                language=language,
            )
        )
        if window_end >= end_seconds:
            break
        current += step

    return deduplicate_nearby(records)


def transcribe_full_audio_once(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    batch_size: int,
    hotwords: str,
) -> tuple[list[SegmentRecord], object]:
    pipeline = BatchedInferencePipeline(model=model)

    iterator, information = pipeline.transcribe(
        audio,
        task="transcribe",
        language="ja",
        multilingual=True,
        beam_size=5,
        best_of=5,
        patience=1.0,
        length_penalty=1.0,
        repetition_penalty=1.05,
        no_repeat_ngram_size=0,
        temperature=0.0,
        hotwords=hotwords,
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

    records = [to_record(segment) for segment in iterator]
    if not records:
        raise TranscriptionError("Whisper returned no transcription segments.")
    return records, information


def transcribe_full_audio_with_fallback(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    batch_candidates: Iterable[int],
    hotwords: str,
) -> tuple[list[SegmentRecord], object, int]:
    last_exception: BaseException | None = None

    for batch_size in batch_candidates:
        print()
        print("TRANSCRIPTION_ATTEMPT")
        print(f"BATCH_SIZE={batch_size}")
        try:
            records, information = transcribe_full_audio_once(
                model=model,
                BatchedInferencePipeline=BatchedInferencePipeline,
                audio=audio,
                batch_size=batch_size,
                hotwords=hotwords,
            )
            return records, information, batch_size
        except Exception as exception:
            last_exception = exception
            if not is_cuda_memory_error(exception):
                raise
            print(f"GPU_MEMORY_RETRY=BATCH_SIZE_{batch_size}")
            gc.collect()

    raise TranscriptionError(
        "Transcription failed even at the smallest batch size."
    ) from last_exception


def merge_opening_records(
    full_records: list[SegmentRecord],
    opening_auto: list[SegmentRecord],
    opening_ja: list[SegmentRecord],
    opening_end: float,
) -> list[SegmentRecord]:
    retained = [record for record in full_records if record.start >= opening_end]
    opening_from_full = [record for record in full_records if record.start < opening_end]
    merged_opening = deduplicate_nearby(
        [*opening_from_full, *opening_auto, *opening_ja]
    )
    return deduplicate_nearby([*merged_opening, *retained])


def resembles_known_english(text: str, repeated_phrases: Iterable[str]) -> bool:
    normalized = normalize_english(text)
    if not normalized:
        return False
    for phrase in repeated_phrases:
        phrase_normalized = normalize_english(phrase)
        if not phrase_normalized:
            continue
        if SequenceMatcher(None, normalized, phrase_normalized).ratio() >= 0.72:
            return True
    return False


def choose_japanese_repair(
    model: object,
    audio: object,
    record: SegmentRecord,
    hotwords: str,
    audio_seconds: float,
) -> tuple[str, float] | None:
    attempts = ((0.9, 0.9), (1.8, 1.8))
    best: tuple[str, float] | None = None

    for before, after in attempts:
        candidates = transcribe_window(
            model=model,
            audio=audio,
            start_seconds=max(0.0, record.start - before),
            end_seconds=min(audio_seconds, record.end + after),
            hotwords=hotwords,
            language="ja",
        )
        if not candidates:
            continue

        candidate_text = " ".join(item.text for item in candidates).strip()
        candidate_japanese = japanese_character_count(candidate_text)
        candidate_logprob = average_logprob(candidates)
        if candidate_japanese < 4:
            continue

        if best is None or candidate_logprob > best[1]:
            best = (candidate_text, candidate_logprob)

    return best


def repair_probable_language_errors(
    model: object,
    audio: object,
    records: list[SegmentRecord],
    repeated_phrases: list[str],
    hotwords: str,
    audio_seconds: float,
) -> tuple[list[SegmentRecord], int, int]:
    repaired = list(records)
    repair_count = 0

    for index, record in enumerate(list(repaired)):
        text = record.text.strip()
        words = english_word_count(text)
        japanese = japanese_character_count(text)
        duration = max(0.0, record.end - record.start)

        previous_has_japanese = (
            index > 0 and japanese_character_count(repaired[index - 1].text) >= 4
        )
        next_has_japanese = (
            index + 1 < len(repaired)
            and japanese_character_count(repaired[index + 1].text) >= 4
        )

        candidate = (
            japanese == 0
            and 1 <= words <= 40
            and duration >= 0.5
            and (previous_has_japanese or next_has_japanese)
            and not resembles_known_english(text, repeated_phrases)
            and not any(section.lower() in text.lower() for section in SECTION_ALIASES)
            and (record.avg_logprob <= -0.25 or words <= 6)
        )
        if not candidate or repair_count >= MAX_LANGUAGE_REPAIRS:
            continue

        selected = choose_japanese_repair(
            model=model,
            audio=audio,
            record=record,
            hotwords=hotwords,
            audio_seconds=audio_seconds,
        )
        if selected is None:
            continue

        repaired_text, repaired_logprob = selected
        if (
            repaired_logprob >= record.avg_logprob - 0.25
            and normalize_for_comparison(repaired_text) != normalize_for_comparison(text)
        ):
            repaired[index] = SegmentRecord(
                start=record.start,
                end=record.end,
                text=repaired_text,
                avg_logprob=repaired_logprob,
                compression_ratio=record.compression_ratio,
                no_speech_prob=record.no_speech_prob,
            )
            repair_count += 1

    repaired = deduplicate_nearby(repaired)
    remaining_suspicious = count_suspicious_language_fragments(
        repaired, repeated_phrases
    )
    return repaired, repair_count, remaining_suspicious


def count_suspicious_language_fragments(
    records: list[SegmentRecord],
    repeated_phrases: list[str],
) -> int:
    count = 0
    for index, record in enumerate(records):
        text = record.text.strip()
        words = english_word_count(text)
        if japanese_character_count(text) != 0 or not 1 <= words <= 8:
            continue

        previous_has_japanese = (
            index > 0 and japanese_character_count(records[index - 1].text) >= 4
        )
        next_has_japanese = (
            index + 1 < len(records)
            and japanese_character_count(records[index + 1].text) >= 4
        )
        if (
            (previous_has_japanese or next_has_japanese)
            and not resembles_known_english(text, repeated_phrases)
            and not any(section.lower() in text.lower() for section in SECTION_ALIASES)
        ):
            count += 1
    return count


def remove_known_hallucinations(text: str) -> tuple[str, int]:
    result = text
    count_total = 0
    for pattern in KNOWN_HALLUCINATION_PATTERNS:
        result, count = re.subn(pattern, "", result, flags=re.IGNORECASE)
        count_total += count
    return re.sub(r"[ \t]+", " ", result).strip(), count_total


def canonicalize_repeated_phrase(
    text: str,
    repeated_phrases: Iterable[str],
) -> str:
    normalized = normalize_english(text)
    original_words = english_words(text)
    if len(original_words) < 4 or not normalized:
        return text

    best_phrase: str | None = None
    best_ratio = 0.0
    for phrase in repeated_phrases:
        phrase_words = english_words(phrase)
        if len(phrase_words) > len(original_words) + 1:
            continue
        if len(phrase_words) < max(4, len(original_words) - 3):
            continue

        phrase_normalized = normalize_english(phrase)
        ratio = SequenceMatcher(None, normalized, phrase_normalized).ratio()
        if ratio > best_ratio:
            best_phrase = phrase
            best_ratio = ratio

    if best_phrase is not None and best_ratio >= 0.86:
        original_first = original_words[0].lower()
        replacement_first = english_words(best_phrase)[0].lower()
        if original_first != replacement_first and normalize_english(text) in normalize_english(best_phrase):
            return text
        return best_phrase
    return text


def split_heading(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    for canonical, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            match = re.match(re.escape(alias), stripped, flags=re.IGNORECASE)
            if match:
                remainder = stripped[match.end():].lstrip(" ：:-–—")
                return canonical, remainder
    return None, stripped


def format_transcript(
    records: Iterable[SegmentRecord],
    repeated_phrases: list[str],
) -> tuple[str, int]:
    lines: list[str] = []
    previous_end: float | None = None
    removed_total = 0
    last_heading: str | None = None

    for record in records:
        text, removed = remove_known_hallucinations(record.text)
        removed_total += removed
        if not text:
            continue

        text = canonicalize_repeated_phrase(text, repeated_phrases)
        heading, remainder = split_heading(text)

        if heading is not None:
            if lines and lines[-1] != "":
                lines.append("")
            if heading != last_heading:
                lines.append(heading)
                lines.append("")
                last_heading = heading
            if remainder:
                lines.append(remainder)
            previous_end = record.end
            continue

        gap = 0.0 if previous_end is None else max(0.0, record.start - previous_end)
        if gap >= 1.10 and lines and lines[-1] != "":
            lines.append("")

        lines.append(text)
        previous_end = record.end

    compact_lines: list[str] = []
    for line in lines:
        if line == "" and compact_lines and compact_lines[-1] == "":
            continue
        compact_lines.append(line)

    transcript = "\n".join(compact_lines).strip()
    if not transcript:
        raise TranscriptionError("The transcription text is empty.")
    return transcript + "\n", removed_total


def detect_decoder_loop(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    for unit_length in range(2, 17):
        match = re.search(rf"(.{{{unit_length}}})\1{{9,}}", compact)
        if match:
            return match.group(1)
    return None


def find_sections(text: str) -> list[str]:
    found: list[str] = []
    for canonical, aliases in SECTION_ALIASES.items():
        if any(alias.lower() in text.lower() for alias in aliases):
            found.append(canonical)
    return found


def count_phrase_occurrences(text: str, phrases: Iterable[str]) -> int:
    normalized_text = normalize_english(text)
    return sum(
        normalized_text.count(normalize_english(phrase))
        for phrase in phrases
        if normalize_english(phrase)
    )


def print_quality_summary(
    text: str,
    audio_seconds: float,
    repeated_phrases: list[str],
    removed_hallucinations: int,
    repaired_languages: int,
    remaining_suspicious_fragments: int,
) -> bool:
    found_sections = find_sections(text)
    missing_sections = [
        section for section in SECTION_ALIASES if section not in found_sections
    ]
    phrase_occurrences = count_phrase_occurrences(text, repeated_phrases)
    minimum_characters = max(3500, int(audio_seconds * 5.0))
    decoder_loop = detect_decoder_loop(text)

    ready = (
        len(found_sections) == len(SECTION_ALIASES)
        and len(text) >= minimum_characters
        and decoder_loop is None
        and remaining_suspicious_fragments <= 1
        and (not repeated_phrases or phrase_occurrences >= 2)
    )

    print("TRANSCRIPT_QUALITY_SUMMARY")
    print(f"TEXT_CHARACTERS={len(text)}")
    print(f"MINIMUM_EXPECTED_CHARACTERS={minimum_characters}")
    print(f"JAPANESE_CHARACTERS={japanese_character_count(text)}")
    print(f"LATIN_CHARACTERS={latin_character_count(text)}")
    print(f"EXPECTED_SECTIONS_FOUND={len(found_sections)}/{len(SECTION_ALIASES)}")
    print("FOUND_SECTIONS=" + (" | ".join(found_sections) if found_sections else "NONE"))
    print("MISSING_SECTIONS=" + (" | ".join(missing_sections) if missing_sections else "NONE"))
    print(f"DISCOVERED_REPEATED_PHRASES={len(repeated_phrases)}")
    print(f"REPEATED_PHRASE_OCCURRENCES={phrase_occurrences}")
    print(f"LANGUAGE_ERRORS_REPAIRED={repaired_languages}")
    print(f"SUSPICIOUS_LANGUAGE_FRAGMENTS={remaining_suspicious_fragments}")
    print(f"KNOWN_HALLUCINATIONS_REMOVED={removed_hallucinations}")
    print(f"DECODER_LOOP={'NONE' if decoder_loop is None else decoder_loop}")
    print(f"AI_HANDOFF_READY={'YES' if ready else 'NO'}")
    return ready


def write_text_atomically(output_file: Path, text: str) -> None:
    temporary = output_file.with_name(output_file.name + ".part")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(output_file)


def run() -> int:
    timer = StepTimer()
    overall_result = "FAILED"
    output_file: Path | None = None
    audio_seconds = 0.0

    try:
        timer.start("parse_arguments_and_paths")
        arguments = parse_arguments()
        script_directory = Path(__file__).resolve().parent
        created_at = datetime.now(JST)
        input_file = resolve_path(script_directory, arguments.input)
        output_directory = resolve_path(script_directory, arguments.output_dir)
        output_file = create_output_path(output_directory, created_at)
        batch_candidates = build_batch_candidates(arguments.batch_size)
        timer.finish()

        timer.start("check_and_update_software")
        check_and_update_runtime_packages(script_directory)
        timer.finish()

        timer.start("import_transcription_libraries")
        import ctranslate2
        import faster_whisper
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        from faster_whisper.audio import decode_audio
        timer.finish()

        timer.start("check_and_update_model")
        model_directory, model_revision, model_updated = check_and_update_model(
            script_directory
        )
        timer.finish()

        timer.start("validate_environment")
        validate_environment(
            input_file=input_file,
            batch_size=arguments.batch_size,
            ctranslate2=ctranslate2,
        )
        timer.finish()

        print("TRANSCRIPTION_CONFIGURATION")
        print(f"PYTHON={sys.executable}")
        print(f"FASTER_WHISPER={faster_whisper.__version__}")
        print(f"CTRANSLATE2={ctranslate2.__version__}")
        print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
        print(f"MODEL_ALIAS={MODEL_ALIAS}")
        print(f"MODEL_REVISION={model_revision}")
        print(f"MODEL_UPDATED={'YES' if model_updated else 'NO'}")
        print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
        print("LANGUAGE_BASE=ja")
        print("MULTILINGUAL=True")
        print("BATCHED_INFERENCE=True")
        print("VAD_FILTER=True")
        print(f"OPENING_CONTEXT_SECONDS={OPENING_CONTEXT_SECONDS}")
        print("OPENING_DUAL_LANGUAGE_PASS=True")
        print("BATCH_CANDIDATES=" + ",".join(str(value) for value in batch_candidates))
        print(f"OUTPUT_CREATED_AT={created_at.isoformat()}")
        print(f"INPUT={input_file}")
        print(f"OUTPUT={output_file}")

        timer.start("decode_audio")
        audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
        audio_seconds = len(audio) / SAMPLE_RATE
        timer.finish()

        timer.start("load_model")
        model = load_model(WhisperModel, model_directory)
        timer.finish()

        timer.start("learn_opening_context_auto")
        opening_end = min(audio_seconds, float(OPENING_CONTEXT_SECONDS))
        opening_auto = transcribe_opening_context(
            model=model,
            audio=audio,
            end_seconds=opening_end,
            hotwords=FIXED_HOTWORDS,
            language=None,
        )
        repeated_phrases = extract_repeated_phrases(opening_auto)
        hotwords = build_hotwords(repeated_phrases)
        timer.finish()

        timer.start("transcribe_opening_context_ja")
        opening_ja = transcribe_opening_context(
            model=model,
            audio=audio,
            end_seconds=opening_end,
            hotwords=hotwords,
            language="ja",
        )
        timer.finish()

        print("DISCOVERED_PHRASES")
        if repeated_phrases:
            for index, phrase in enumerate(repeated_phrases, start=1):
                print(f"PHRASE_{index:02d}={phrase}")
        else:
            print("PHRASE_NONE")
        print(f"OPENING_AUTO_SEGMENTS={len(opening_auto)}")
        print(f"OPENING_JA_SEGMENTS={len(opening_ja)}")

        timer.start("transcribe_full_audio")
        full_records, information, used_batch_size = transcribe_full_audio_with_fallback(
            model=model,
            BatchedInferencePipeline=BatchedInferencePipeline,
            audio=audio,
            batch_candidates=batch_candidates,
            hotwords=hotwords,
        )
        timer.finish()

        timer.start("merge_opening_context")
        records = merge_opening_records(
            full_records=full_records,
            opening_auto=opening_auto,
            opening_ja=opening_ja,
            opening_end=opening_end,
        )
        timer.finish()

        timer.start("repair_probable_language_errors")
        records, language_repairs, remaining_suspicious = repair_probable_language_errors(
            model=model,
            audio=audio,
            records=records,
            repeated_phrases=repeated_phrases,
            hotwords=hotwords,
            audio_seconds=audio_seconds,
        )
        timer.finish()

        timer.start("format_and_validate_transcript")
        transcript, removed_hallucinations = format_transcript(
            records=records,
            repeated_phrases=repeated_phrases,
        )
        decoder_loop = detect_decoder_loop(transcript)
        if decoder_loop is not None:
            raise TranscriptionError(
                f"An obvious decoder repetition loop was detected: {decoder_loop!r}"
            )
        timer.finish()

        timer.start("write_output_file")
        write_text_atomically(output_file, transcript)
        timer.finish()

        core_seconds = timer.step_seconds("transcribe_full_audio")
        core_realtime_factor = core_seconds / audio_seconds if audio_seconds > 0 else 0.0
        total_realtime_factor = timer.total_seconds / audio_seconds if audio_seconds > 0 else 0.0

        print("TRANSCRIPTION_RESULT=PASS")
        print(f"OUTPUT_FILE={output_file}")
        print(f"BATCH_SIZE_USED={used_batch_size}")
        print(f"FULL_SEGMENT_COUNT={len(full_records)}")
        print(f"FINAL_SEGMENT_COUNT={len(records)}")
        print(f"AUDIO_SECONDS={audio_seconds:.3f}")
        print(f"CORE_TRANSCRIPTION_REALTIME_FACTOR={core_realtime_factor:.4f}")
        print(f"TOTAL_PROCESS_REALTIME_FACTOR={total_realtime_factor:.4f}")

        print_quality_summary(
            text=transcript,
            audio_seconds=audio_seconds,
            repeated_phrases=repeated_phrases,
            removed_hallucinations=removed_hallucinations,
            repaired_languages=language_repairs,
            remaining_suspicious_fragments=remaining_suspicious,
        )

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
        if output_file is not None:
            print(f"OUTPUT_FILE_NOT_CREATED={output_file}", file=sys.stderr)
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
