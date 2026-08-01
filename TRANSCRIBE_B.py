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
from typing import Iterable


DEVICE = "cuda"
COMPUTE_TYPE = "float16"
SAMPLE_RATE = 16000
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
WINDOW_SECONDS = 25.0
WINDOW_OVERLAP_SECONDS = 4.0
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

SECTION_NAMES = (
    "Grammar and Vocabulary",
    "Essential Expressions",
    "Practical Usage",
    "Pronunciation Polish",
)


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repository: str
    directory_name: str
    role: str


MODEL_SPECS = (
    ModelSpec(
        key="turbo",
        repository="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        directory_name="faster-whisper-large-v3-turbo",
        role="primary full-coverage transcription",
    ),
    ModelSpec(
        key="large_v3",
        repository="Systran/faster-whisper-large-v3",
        directory_name="faster-whisper-large-v3",
        role="high-accuracy multilingual recheck",
    ),
    ModelSpec(
        key="kotoba_ja",
        repository="kotoba-tech/kotoba-whisper-v2.0-faster",
        directory_name="kotoba-whisper-v2.0-faster",
        role="Japanese-specialist recheck",
    ),
)


@dataclass(frozen=True)
class WindowPlan:
    index: int
    start: float
    end: float
    keep_start: float
    keep_end: float


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
class WindowCandidate:
    window: WindowPlan
    source: str
    language_mode: str
    segments: list[SegmentRecord]
    text: str
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float
    score: float = -9999.0
    flags: list[str] = field(default_factory=list)


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
            "Create a faithful full transcript of mixed Japanese-English radio audio. "
            "This program does not create or edit an educational lesson document."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
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
    versions: dict[str, str | None] = {}
    for package in RUNTIME_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def check_and_update_runtime_packages(script_directory: Path) -> None:
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


def model_files_are_complete(model_directory: Path) -> bool:
    return all((model_directory / name).is_file() for name in MODEL_REQUIRED_FILES)


def check_and_update_model(
    script_directory: Path,
    spec: ModelSpec,
) -> tuple[Path, str, bool]:
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

    model_directory = script_directory / "models" / spec.directory_name
    revision_file = model_directory / ".model_revision.txt"

    print("MODEL_VERSION_CHECK_START")
    print(f"MODEL_KEY={spec.key}")
    print(f"MODEL_ROLE={spec.role}")
    print(f"MODEL_REPOSITORY={spec.repository}")
    print(f"MODEL_DIRECTORY={model_directory}")

    try:
        information = HfApi().model_info(
            repo_id=spec.repository,
            revision="main",
            timeout=30,
        )
    except Exception as exception:
        raise TranscriptionError(
            f"The latest revision of {spec.repository} could not be checked online. "
            f"{type(exception).__name__}: {exception}"
        ) from exception

    latest_revision = str(information.sha or "").strip()
    if not latest_revision:
        raise TranscriptionError(
            f"The model service returned no revision for {spec.repository}."
        )

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
                repo_id=spec.repository,
                revision=latest_revision,
                local_dir=str(model_directory),
                allow_patterns=list(MODEL_ALLOW_PATTERNS),
            )
        except Exception as exception:
            raise TranscriptionError(
                f"The latest files for {spec.repository} could not be downloaded. "
                f"{type(exception).__name__}: {exception}"
            ) from exception

        if not model_files_are_complete(model_directory):
            missing = [
                name
                for name in MODEL_REQUIRED_FILES
                if not (model_directory / name).is_file()
            ]
            raise TranscriptionError(
                f"The downloaded model {spec.repository} is incomplete. Missing: "
                + ", ".join(missing)
            )

        revision_file.write_text(latest_revision + "\n", encoding="utf-8")

    print("MODEL_VERSION_CHECK_RESULT=PASS")
    return model_directory, latest_revision, download_required


def check_and_update_all_models(
    script_directory: Path,
) -> tuple[dict[str, Path], dict[str, str], dict[str, bool]]:
    paths: dict[str, Path] = {}
    revisions: dict[str, str] = {}
    updates: dict[str, bool] = {}

    for spec in MODEL_SPECS:
        model_path, revision, updated = check_and_update_model(
            script_directory, spec
        )
        paths[spec.key] = model_path
        revisions[spec.key] = revision
        updates[spec.key] = updated

    return paths, revisions, updates


def create_output_path(output_directory: Path, created_at: datetime) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.astimezone(JST).strftime("%Y%m%d_%H%M%S")
    candidate = output_directory / f"{timestamp}.txt"
    sequence = 2

    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = output_directory / f"{timestamp}_{sequence:02d}.txt"
        sequence += 1

    return candidate


def validate_environment(input_file: Path, ctranslate2: object) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")

    if ctranslate2.get_cuda_device_count() < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")

    supported = ctranslate2.get_supported_compute_types("cuda")
    if COMPUTE_TYPE not in supported:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is not supported. Supported types: {sorted(supported)}"
        )


def build_windows(audio_seconds: float) -> list[WindowPlan]:
    if audio_seconds <= 0:
        raise TranscriptionError("The decoded audio has zero duration.")

    step = WINDOW_SECONDS - WINDOW_OVERLAP_SECONDS
    raw: list[tuple[float, float]] = []
    start = 0.0

    while start < audio_seconds:
        end = min(audio_seconds, start + WINDOW_SECONDS)
        raw.append((start, end))
        if end >= audio_seconds:
            break
        start += step

    windows: list[WindowPlan] = []
    for index, (window_start, window_end) in enumerate(raw):
        if index == 0:
            keep_start = 0.0
        else:
            previous_end = raw[index - 1][1]
            keep_start = (window_start + previous_end) / 2.0

        if index == len(raw) - 1:
            keep_end = audio_seconds
        else:
            next_start = raw[index + 1][0]
            keep_end = (window_end + next_start) / 2.0

        windows.append(
            WindowPlan(
                index=index,
                start=window_start,
                end=window_end,
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


def release_model(model: object) -> None:
    del model
    gc.collect()
    time.sleep(0.3)


def clean_segment_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def segment_from_object(
    segment: object,
    *,
    offset: float,
    source: str,
) -> SegmentRecord:
    start = max(0.0, float(getattr(segment, "start", 0.0)) + offset)
    end = max(start, float(getattr(segment, "end", start)) + offset)
    return SegmentRecord(
        start=start,
        end=end,
        text=clean_segment_text(str(getattr(segment, "text", ""))),
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
        source=source,
    )


def weighted_average(
    values: Iterable[tuple[float, float]],
    default: float,
) -> float:
    numerator = 0.0
    denominator = 0.0
    for value, weight in values:
        effective_weight = max(weight, 0.01)
        numerator += value * effective_weight
        denominator += effective_weight
    if denominator == 0:
        return default
    return numerator / denominator


def transcribe_window(
    model: object,
    audio: object,
    window: WindowPlan,
    *,
    source: str,
    language: str | None,
    multilingual: bool,
) -> WindowCandidate:
    start_sample = int(window.start * SAMPLE_RATE)
    end_sample = int(window.end * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]

    iterator, _information = model.transcribe(
        audio_slice,
        task="transcribe",
        language=language,
        multilingual=multilingual,
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
        hotwords=TRANSCRIPTION_GLOSSARY,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        log_progress=False,
    )

    segments = [
        segment_from_object(segment, offset=window.start, source=source)
        for segment in iterator
    ]
    segments = [segment for segment in segments if segment.text]
    text = " ".join(segment.text for segment in segments).strip()

    avg_logprob = weighted_average(
        (
            (segment.avg_logprob, max(segment.end - segment.start, 0.1))
            for segment in segments
        ),
        default=-10.0,
    )
    compression_ratio = max(
        (segment.compression_ratio for segment in segments),
        default=0.0,
    )
    no_speech_prob = weighted_average(
        (
            (segment.no_speech_prob, max(segment.end - segment.start, 0.1))
            for segment in segments
        ),
        default=1.0,
    )

    return WindowCandidate(
        window=window,
        source=source,
        language_mode=language or "auto",
        segments=segments,
        text=text,
        avg_logprob=avg_logprob,
        compression_ratio=compression_ratio,
        no_speech_prob=no_speech_prob,
    )


def transcribe_windows(
    model: object,
    audio: object,
    windows: Iterable[WindowPlan],
    *,
    source: str,
    language: str | None,
    multilingual: bool,
) -> dict[int, WindowCandidate]:
    selected_windows = list(windows)
    results: dict[int, WindowCandidate] = {}
    total = len(selected_windows)

    for position, window in enumerate(selected_windows, start=1):
        print(
            f"WINDOW_TRANSCRIPTION={source} "
            f"{position}/{total} "
            f"INDEX={window.index:03d} "
            f"START={window.start:.3f} END={window.end:.3f}"
        )
        results[window.index] = transcribe_window(
            model=model,
            audio=audio,
            window=window,
            source=source,
            language=language,
            multilingual=multilingual,
        )

    return results


def normalize_for_comparison(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龯々]+", "", text.lower())


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


def glossary_hit_count(text: str) -> int:
    glossary_items = (
        "Miller",
        "Theo",
        "Jennifer",
        "ミラー",
        "セオ",
        "ジェニファー",
        "Grammar and Vocabulary",
        "Essential Expressions",
        "Practical Usage",
        "Pronunciation Polish",
        "仮定法",
        "目的格",
        "不定詞",
        "意味上の主語",
        "現在進行形",
        "説明型オーバーラッピング",
        "come out",
        "not exactly",
        "what if",
        "for us",
        "be at this",
    )
    lower_text = text.lower()
    return sum(1 for item in glossary_items if item.lower() in lower_text)


def text_similarity(left: str, right: str) -> float:
    normalized_left = normalize_for_comparison(left)
    normalized_right = normalize_for_comparison(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def primary_profile(candidate: WindowCandidate) -> dict[str, float | int | bool]:
    japanese = japanese_character_count(candidate.text)
    latin = latin_character_count(candidate.text)
    words = len(english_words(candidate.text))
    total_letters = max(japanese + latin, 1)
    return {
        "japanese": japanese,
        "latin": latin,
        "english_words": words,
        "japanese_ratio": japanese / total_letters,
        "latin_ratio": latin / total_letters,
        "mixed": japanese >= 8 and words >= 3,
    }


def candidate_flags(candidate: WindowCandidate) -> list[str]:
    flags: list[str] = []
    text = candidate.text
    stripped_lower = text.strip().lower()

    if not text:
        flags.append("empty")
    if "�" in text:
        flags.append("replacement_character")
    if contains_decoder_loop(text):
        flags.append("decoder_loop")
    if candidate.avg_logprob < -0.65:
        flags.append("low_logprob")
    if candidate.compression_ratio > 2.35:
        flags.append("high_compression")
    if candidate.no_speech_prob > 0.75 and text:
        flags.append("speech_probability_conflict")
    if stripped_lower in {"and", "to", "the", "a", "an", "but"}:
        flags.append("isolated_function_word")
    if len(text) > 0 and len(text) < 3:
        flags.append("very_short")

    return flags


def candidate_score(
    candidate: WindowCandidate,
    candidates: list[WindowCandidate],
    profile: dict[str, float | int | bool],
    primary_length: int,
) -> float:
    flags = candidate_flags(candidate)
    candidate.flags = flags

    score = 0.0
    score += candidate.avg_logprob * 2.8
    score += min(math.log1p(len(candidate.text)), 7.0) * 0.20
    score += glossary_hit_count(candidate.text) * 0.08

    if candidate.source == "large_v3_auto":
        score += 0.40
    elif candidate.source == "large_v3_en":
        score += 0.55 if float(profile["latin_ratio"]) >= 0.55 else -0.55
    elif candidate.source == "kotoba_ja":
        score += 0.50 if float(profile["japanese_ratio"]) >= 0.45 else -1.25
    elif candidate.source == "turbo_auto":
        score += 0.05

    if bool(profile["mixed"]):
        if candidate.source == "large_v3_auto":
            score += 0.45
        if candidate.source == "kotoba_ja":
            score -= 0.25

    if primary_length > 40 and len(candidate.text) < primary_length * 0.50:
        score -= 1.10
    if primary_length > 80 and len(candidate.text) > primary_length * 1.85:
        score -= 0.65

    penalties = {
        "empty": 8.0,
        "replacement_character": 4.0,
        "decoder_loop": 6.0,
        "low_logprob": 1.0,
        "high_compression": 1.0,
        "speech_probability_conflict": 0.8,
        "isolated_function_word": 2.5,
        "very_short": 1.0,
    }
    score -= sum(penalties.get(flag, 0.0) for flag in flags)

    similarities = [
        text_similarity(candidate.text, other.text)
        for other in candidates
        if other is not candidate and other.text
    ]
    if similarities:
        score += max(similarities) * 0.65
        score += (sum(similarities) / len(similarities)) * 0.30

    return score


def choose_recheck_windows(
    primary: dict[int, WindowCandidate],
    windows: list[WindowPlan],
) -> tuple[list[WindowPlan], list[WindowPlan], list[WindowPlan]]:
    accurate_auto: list[WindowPlan] = []
    accurate_english: list[WindowPlan] = []
    japanese_specialist: list[WindowPlan] = []

    for window in windows:
        candidate = primary[window.index]
        profile = primary_profile(candidate)
        flags = candidate_flags(candidate)
        english_words_count = int(profile["english_words"])
        japanese_count = int(profile["japanese"])
        latin_ratio = float(profile["latin_ratio"])

        needs_accurate = (
            english_words_count >= 3
            or bool(profile["mixed"])
            or bool(flags)
            or any(section.lower() in candidate.text.lower() for section in SECTION_NAMES)
        )
        if needs_accurate:
            accurate_auto.append(window)

        if english_words_count >= 7 and latin_ratio >= 0.62 and japanese_count < 20:
            accurate_english.append(window)

        if japanese_count >= 12 or bool(profile["mixed"]) or bool(flags):
            japanese_specialist.append(window)

    return accurate_auto, accurate_english, japanese_specialist


def choose_candidates(
    windows: list[WindowPlan],
    primary: dict[int, WindowCandidate],
    large_auto: dict[int, WindowCandidate],
    large_english: dict[int, WindowCandidate],
    kotoba: dict[int, WindowCandidate],
) -> tuple[dict[int, WindowCandidate], list[int], list[int]]:
    chosen: dict[int, WindowCandidate] = {}
    disagreement_windows: list[int] = []
    low_confidence_windows: list[int] = []

    for window in windows:
        candidates = [primary[window.index]]
        for candidate_map in (large_auto, large_english, kotoba):
            candidate = candidate_map.get(window.index)
            if candidate is not None:
                candidates.append(candidate)

        profile = primary_profile(primary[window.index])
        primary_length = len(primary[window.index].text)
        for candidate in candidates:
            candidate.score = candidate_score(
                candidate,
                candidates,
                profile,
                primary_length,
            )

        candidates.sort(key=lambda item: item.score, reverse=True)
        selected = candidates[0]
        chosen[window.index] = selected

        nonempty = [candidate for candidate in candidates if candidate.text]
        similarities: list[float] = []
        for left_index in range(len(nonempty)):
            for right_index in range(left_index + 1, len(nonempty)):
                similarities.append(
                    text_similarity(
                        nonempty[left_index].text,
                        nonempty[right_index].text,
                    )
                )
        if similarities and max(similarities) < 0.52:
            disagreement_windows.append(window.index)

        if (
            not selected.text
            or selected.avg_logprob < -0.72
            or contains_decoder_loop(selected.text)
            or "replacement_character" in selected.flags
        ):
            low_confidence_windows.append(window.index)

        source_scores = ",".join(
            f"{candidate.source}:{candidate.score:.3f}"
            for candidate in candidates
        )
        print(
            f"WINDOW_SELECTION INDEX={window.index:03d} "
            f"START={window.start:.3f} END={window.end:.3f} "
            f"SELECTED={selected.source} SCORES={source_scores}"
        )

    return chosen, disagreement_windows, low_confidence_windows


def segment_midpoint(segment: SegmentRecord) -> float:
    return (segment.start + segment.end) / 2.0


def keep_segments_owned_by_window(
    candidate: WindowCandidate,
) -> list[SegmentRecord]:
    kept: list[SegmentRecord] = []
    for segment in candidate.segments:
        midpoint = segment_midpoint(segment)
        is_last_boundary = math.isclose(
            candidate.window.keep_end,
            candidate.window.end,
            abs_tol=0.001,
        )
        within = (
            midpoint >= candidate.window.keep_start
            and (
                midpoint < candidate.window.keep_end
                or (is_last_boundary and midpoint <= candidate.window.keep_end)
            )
        )
        if within:
            kept.append(segment)
    return kept


def are_adjacent_duplicates(left: SegmentRecord, right: SegmentRecord) -> bool:
    if right.start - left.end > 1.5:
        return False
    normalized_left = normalize_for_comparison(left.text)
    normalized_right = normalize_for_comparison(right.text)
    if not normalized_left or not normalized_right:
        return False
    similarity = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    return (
        similarity >= 0.88
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    )


def assemble_final_segments(
    windows: list[WindowPlan],
    chosen: dict[int, WindowCandidate],
) -> list[SegmentRecord]:
    ordered: list[SegmentRecord] = []

    for window in windows:
        ordered.extend(keep_segments_owned_by_window(chosen[window.index]))

    ordered.sort(key=lambda segment: (segment.start, segment.end))
    result: list[SegmentRecord] = []

    for segment in ordered:
        if not segment.text:
            continue
        if result and are_adjacent_duplicates(result[-1], segment):
            previous = result[-1]
            previous_quality = previous.avg_logprob + min(len(previous.text), 160) / 1000.0
            current_quality = segment.avg_logprob + min(len(segment.text), 160) / 1000.0
            if current_quality > previous_quality:
                result[-1] = segment
            continue
        result.append(segment)

    if not result:
        raise TranscriptionError("No transcript segments remained after window assembly.")
    return result


def format_faithful_transcript(segments: Iterable[SegmentRecord]) -> str:
    lines: list[str] = []
    previous_end: float | None = None

    for segment in segments:
        text = clean_segment_text(segment.text)
        if not text:
            continue

        if previous_end is not None and segment.start - previous_end >= 1.4:
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
        raise TranscriptionError("The transcription text is empty.")
    return transcript + "\n"


def write_text_atomically(output_file: Path, text: str) -> None:
    temporary = output_file.with_name(output_file.name + ".part")
    if temporary.exists():
        temporary.unlink()
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(output_file)


def print_fidelity_summary(
    *,
    transcript: str,
    windows: list[WindowPlan],
    chosen: dict[int, WindowCandidate],
    disagreement_windows: list[int],
    low_confidence_windows: list[int],
    final_segments: list[SegmentRecord],
) -> None:
    source_counts = Counter(candidate.source for candidate in chosen.values())
    section_count = sum(
        1 for section in SECTION_NAMES if section.lower() in transcript.lower()
    )
    replacement_characters = transcript.count("�")
    decoder_loop = contains_decoder_loop(transcript)

    status = "PASS_CANDIDATE"
    if low_confidence_windows or replacement_characters or decoder_loop:
        status = "REVIEW_REQUIRED"
    elif len(disagreement_windows) > max(2, len(windows) // 8):
        status = "REVIEW_RECOMMENDED"

    print("TRANSCRIPTION_FIDELITY_SUMMARY")
    print("TRANSCRIPTION_MODE=FAITHFUL_FULL_TRANSCRIPT")
    print("EDUCATIONAL_FORMATTING_APPLIED=NO")
    print("CONTENT_REMOVAL_APPLIED=NO")
    print("PARAPHRASING_APPLIED=NO")
    print("TRANSLATION_APPLIED=NO")
    print(f"WINDOW_COUNT={len(windows)}")
    print(f"FINAL_SEGMENT_COUNT={len(final_segments)}")
    print(f"TEXT_CHARACTERS={len(transcript)}")
    print(f"JAPANESE_CHARACTERS={japanese_character_count(transcript)}")
    print(f"LATIN_CHARACTERS={latin_character_count(transcript)}")
    print(f"EXPECTED_SECTION_NAMES_FOUND={section_count}/{len(SECTION_NAMES)}")
    print(
        "SELECTED_MODEL_COUNTS="
        + json.dumps(source_counts, ensure_ascii=False, sort_keys=True)
    )
    print(
        "CROSS_MODEL_DISAGREEMENT_WINDOWS="
        + (",".join(str(index) for index in disagreement_windows) or "NONE")
    )
    print(
        "LOW_CONFIDENCE_WINDOWS="
        + (",".join(str(index) for index in low_confidence_windows) or "NONE")
    )
    print(f"REPLACEMENT_CHARACTERS={replacement_characters}")
    print(f"DECODER_LOOP={'YES' if decoder_loop else 'NO'}")
    print(f"TRANSCRIPTION_FIDELITY_STATUS={status}")
    print("FIDELITY_STATUS_SCOPE=AUTOMATIC_DIAGNOSTIC_NOT_REFERENCE_COMPARISON")


def run() -> int:
    timer = StepTimer()
    overall_result = "FAILED"
    output_file: Path | None = None

    try:
        timer.start("parse_arguments_and_paths")
        arguments = parse_arguments()
        script_directory = Path(__file__).resolve().parent
        created_at = datetime.now(JST)
        input_file = resolve_path(script_directory, arguments.input)
        output_directory = resolve_path(script_directory, arguments.output_dir)
        output_file = create_output_path(output_directory, created_at)
        timer.finish()

        timer.start("check_and_update_software")
        check_and_update_runtime_packages(script_directory)
        timer.finish()

        timer.start("import_transcription_libraries")
        import ctranslate2
        import faster_whisper
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        timer.finish()

        timer.start("check_and_update_models")
        model_paths, model_revisions, model_updates = check_and_update_all_models(
            script_directory
        )
        timer.finish()

        timer.start("validate_environment")
        validate_environment(input_file, ctranslate2)
        timer.finish()

        print("TRANSCRIPTION_CONFIGURATION")
        print(f"PYTHON={sys.executable}")
        print(f"FASTER_WHISPER={faster_whisper.__version__}")
        print(f"CTRANSLATE2={ctranslate2.__version__}")
        print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
        print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
        print(f"WINDOW_SECONDS={WINDOW_SECONDS:.1f}")
        print(f"WINDOW_OVERLAP_SECONDS={WINDOW_OVERLAP_SECONDS:.1f}")
        print("PRIMARY_MODEL=large-v3-turbo")
        print("ACCURATE_RECHECK_MODEL=large-v3")
        print("JAPANESE_RECHECK_MODEL=kotoba-whisper-v2.0-faster")
        print("TRANSCRIPTION_MODE=FAITHFUL_FULL_TRANSCRIPT")
        print("EDUCATIONAL_FORMATTING_APPLIED=NO")
        print("CONTENT_REMOVAL_APPLIED=NO")
        print("AUTOMATIC_SENTENCE_REPLACEMENT=NO")
        print(f"MODEL_REVISIONS={json.dumps(model_revisions, sort_keys=True)}")
        print(f"MODEL_UPDATES={json.dumps(model_updates, sort_keys=True)}")
        print(f"OUTPUT_CREATED_AT={created_at.isoformat()}")
        print(f"INPUT={input_file}")
        print(f"OUTPUT={output_file}")

        timer.start("decode_audio")
        audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
        audio_seconds = len(audio) / SAMPLE_RATE
        windows = build_windows(audio_seconds)
        timer.finish()

        print(f"AUDIO_SECONDS={audio_seconds:.3f}")
        print(f"WINDOW_COUNT={len(windows)}")

        timer.start("primary_full_coverage_transcription")
        primary_model = load_model(
            WhisperModel,
            model_paths["turbo"],
            "turbo_auto",
        )
        primary = transcribe_windows(
            primary_model,
            audio,
            windows,
            source="turbo_auto",
            language=None,
            multilingual=True,
        )
        del primary_model
        gc.collect()
        timer.finish()

        timer.start("plan_accuracy_rechecks")
        accurate_auto_windows, accurate_english_windows, japanese_windows = (
            choose_recheck_windows(primary, windows)
        )
        timer.finish()

        print(f"LARGE_V3_AUTO_RECHECK_WINDOWS={len(accurate_auto_windows)}")
        print(f"LARGE_V3_EN_RECHECK_WINDOWS={len(accurate_english_windows)}")
        print(f"KOTOBA_JA_RECHECK_WINDOWS={len(japanese_windows)}")

        timer.start("large_v3_accuracy_rechecks")
        large_auto: dict[int, WindowCandidate] = {}
        large_english: dict[int, WindowCandidate] = {}
        if accurate_auto_windows or accurate_english_windows:
            accurate_model = load_model(
                WhisperModel,
                model_paths["large_v3"],
                "large_v3",
            )
            if accurate_auto_windows:
                large_auto = transcribe_windows(
                    accurate_model,
                    audio,
                    accurate_auto_windows,
                    source="large_v3_auto",
                    language=None,
                    multilingual=True,
                )
            if accurate_english_windows:
                large_english = transcribe_windows(
                    accurate_model,
                    audio,
                    accurate_english_windows,
                    source="large_v3_en",
                    language="en",
                    multilingual=True,
                )
            del accurate_model
            gc.collect()
        timer.finish()

        timer.start("kotoba_japanese_rechecks")
        kotoba: dict[int, WindowCandidate] = {}
        if japanese_windows:
            japanese_model = load_model(
                WhisperModel,
                model_paths["kotoba_ja"],
                "kotoba_ja",
            )
            kotoba = transcribe_windows(
                japanese_model,
                audio,
                japanese_windows,
                source="kotoba_ja",
                language="ja",
                multilingual=False,
            )
            del japanese_model
            gc.collect()
        timer.finish()

        timer.start("cross_model_candidate_selection")
        chosen, disagreement_windows, low_confidence_windows = choose_candidates(
            windows=windows,
            primary=primary,
            large_auto=large_auto,
            large_english=large_english,
            kotoba=kotoba,
        )
        timer.finish()

        timer.start("assemble_faithful_transcript")
        final_segments = assemble_final_segments(windows, chosen)
        transcript = format_faithful_transcript(final_segments)
        timer.finish()

        timer.start("write_output_file")
        write_text_atomically(output_file, transcript)
        timer.finish()

        print("TRANSCRIPTION_RESULT=PASS")
        print(f"OUTPUT_FILE={output_file}")
        print(f"AUDIO_SECONDS={audio_seconds:.3f}")
        print(
            f"TOTAL_PROCESS_REALTIME_FACTOR="
            f"{timer.total_seconds / audio_seconds:.4f}"
        )

        print_fidelity_summary(
            transcript=transcript,
            windows=windows,
            chosen=chosen,
            disagreement_windows=disagreement_windows,
            low_confidence_windows=low_confidence_windows,
            final_segments=final_segments,
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
