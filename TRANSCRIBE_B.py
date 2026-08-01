from __future__ import annotations

import argparse
import gc
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable


MODEL_NAME = "turbo"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_BATCH_SIZE = 8
DEFAULT_CONTEXT_SECONDS = 150
DEFAULT_CLIP_SECONDS = 28.0
CLOSING_REPAIR_SECONDS = 90
MAX_LANGUAGE_REPAIRS = 12
SAMPLE_RATE = 16000
JST = timezone(timedelta(hours=9), name="JST")

FIXED_HOTWORDS = (
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish"
)

KNOWN_HALLUCINATION_PATTERNS = (
    r"English Subtitles by the Amara\.org community",
    r"Subtitles by the Amara\.org community",
    r"日本語の解説と英語のダイアログ.{0,100}文字起こし",
    r"同じ英語の繰り返しも残し(?:てください)?(?:[、, ]*同じ英語の繰り返しも残し(?:てください)?)*",
    r"英語の繰り返しと英語の繰り返しも残し.{0,100}",
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
            "Create a complete AI-handoff transcript from mixed Japanese-English "
            "lesson audio using full-coverage batched transcription and targeted repair."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--context-seconds", type=int, default=DEFAULT_CONTEXT_SECONDS
    )
    parser.add_argument(
        "--clip-seconds", type=float, default=DEFAULT_CLIP_SECONDS
    )
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
    context_seconds: int,
    clip_seconds: float,
    ctranslate2: object,
) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")
    if context_seconds < 60:
        raise ValueError("--context-seconds must be at least 60.")
    if not 10.0 <= clip_seconds <= 29.5:
        raise ValueError("--clip-seconds must be between 10.0 and 29.5.")

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


def load_model(WhisperModel: object) -> object:
    print("MODEL_LOAD_START")
    print(f"MODEL={MODEL_NAME}")
    print(f"COMPUTE_TYPE={COMPUTE_TYPE}")

    model = WhisperModel(
        MODEL_NAME,
        device=DEVICE,
        device_index=0,
        compute_type=COMPUTE_TYPE,
        flash_attention=False,
    )

    print("FLASH_ATTENTION=False")
    print("MODEL_LOAD_COMPLETE")
    return model


def to_record(segment: object, offset: float = 0.0) -> SegmentRecord:
    return SegmentRecord(
        start=float(getattr(segment, "start", 0.0)) + offset,
        end=float(getattr(segment, "end", 0.0)) + offset,
        text=str(getattr(segment, "text", "")).strip(),
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
    )


def normalize_english(text: str) -> str:
    return " ".join(
        re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text.lower())
    )


def english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


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
        repeated.append((len(cluster), representative))

    repeated.sort(key=lambda item: (-item[0], -len(item[1])))
    return [phrase for _count, phrase in repeated[:8]]


def build_hotwords(repeated_phrases: Iterable[str]) -> str:
    values = [FIXED_HOTWORDS]
    values.extend(repeated_phrases)
    return ", ".join(values)[:700]


def transcribe_window(
    model: object,
    audio: object,
    start_seconds: float,
    end_seconds: float,
    hotwords: str,
    language: str | None = None,
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
        log_progress=False,
    )

    return [to_record(segment, start_seconds) for segment in iterator]


def build_full_coverage_clips(
    audio_seconds: float,
    clip_seconds: float,
) -> list[dict[str, float]]:
    clips: list[dict[str, float]] = []
    start = 0.0
    while start < audio_seconds:
        end = min(audio_seconds, start + clip_seconds)
        clips.append({"start": start, "end": end})
        start = end
    return clips


def transcribe_batched_once(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    clips: list[dict[str, float]],
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
        vad_filter=False,
        clip_timestamps=clips,
        chunk_length=30,
        without_timestamps=False,
        word_timestamps=False,
        batch_size=batch_size,
        log_progress=True,
    )

    records = [to_record(segment) for segment in iterator]
    if not records:
        raise TranscriptionError("Whisper returned no transcription segments.")
    return records, information


def transcribe_batched_with_fallback(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    clips: list[dict[str, float]],
    batch_candidates: Iterable[int],
    hotwords: str,
) -> tuple[list[SegmentRecord], object, int]:
    last_exception: BaseException | None = None

    for batch_size in batch_candidates:
        print()
        print("TRANSCRIPTION_ATTEMPT")
        print(f"BATCH_SIZE={batch_size}")
        try:
            records, information = transcribe_batched_once(
                model=model,
                BatchedInferencePipeline=BatchedInferencePipeline,
                audio=audio,
                clips=clips,
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


def replace_window(
    base: list[SegmentRecord],
    replacement: list[SegmentRecord],
    start: float,
    end: float,
) -> list[SegmentRecord]:
    retained = [
        item
        for item in base
        if item.end <= start or item.start >= end
    ]
    retained.extend(replacement)
    return sorted(retained, key=lambda item: (item.start, item.end))


def deduplicate_overlaps(records: Iterable[SegmentRecord]) -> list[SegmentRecord]:
    result: list[SegmentRecord] = []

    for current in sorted(records, key=lambda item: (item.start, item.end)):
        if not current.text.strip():
            continue
        if not result:
            result.append(current)
            continue

        previous = result[-1]
        overlap = min(previous.end, current.end) - max(previous.start, current.start)
        if overlap <= 0.15:
            result.append(current)
            continue

        left = normalize_english(previous.text) or previous.text
        right = normalize_english(current.text) or current.text
        similarity = SequenceMatcher(None, left, right).ratio()

        if similarity >= 0.88 or left in right or right in left:
            if current.avg_logprob > previous.avg_logprob or len(current.text) > len(previous.text):
                result[-1] = current
            continue

        result.append(current)

    return result


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


def repair_probable_language_errors(
    model: object,
    audio: object,
    records: list[SegmentRecord],
    repeated_phrases: list[str],
    hotwords: str,
) -> tuple[list[SegmentRecord], int]:
    repaired = list(records)
    repair_count = 0

    for index, record in enumerate(list(repaired)):
        if repair_count >= MAX_LANGUAGE_REPAIRS:
            break

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
            and 3 <= words <= 35
            and duration >= 1.8
            and (previous_has_japanese or next_has_japanese)
            and record.avg_logprob <= -0.45
            and not resembles_known_english(text, repeated_phrases)
            and not any(section.lower() in text.lower() for section in SECTION_ALIASES)
        )
        if not candidate:
            continue

        japanese_records = transcribe_window(
            model=model,
            audio=audio,
            start_seconds=max(0.0, record.start - 0.5),
            end_seconds=record.end + 0.5,
            hotwords=hotwords,
            language="ja",
        )
        if not japanese_records:
            continue

        repaired_text = " ".join(item.text for item in japanese_records).strip()
        repaired_japanese = japanese_character_count(repaired_text)
        repaired_logprob = sum(item.avg_logprob for item in japanese_records) / len(japanese_records)

        if repaired_japanese >= 6 and repaired_logprob >= record.avg_logprob - 0.05:
            repaired[index] = SegmentRecord(
                start=record.start,
                end=record.end,
                text=repaired_text,
                avg_logprob=repaired_logprob,
                compression_ratio=record.compression_ratio,
                no_speech_prob=record.no_speech_prob,
            )
            repair_count += 1

    return repaired, repair_count


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
    if english_word_count(text) < 4 or not normalized:
        return text

    best_phrase: str | None = None
    best_ratio = 0.0
    for phrase in repeated_phrases:
        ratio = SequenceMatcher(None, normalized, normalize_english(phrase)).ratio()
        if ratio > best_ratio:
            best_phrase = phrase
            best_ratio = ratio

    if best_phrase is not None and best_ratio >= 0.80:
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
        if gap >= 0.85 and lines and lines[-1] != "":
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

        timer.start("import_transcription_libraries")
        import ctranslate2
        import faster_whisper
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        from faster_whisper.audio import decode_audio
        timer.finish()

        timer.start("validate_environment")
        validate_environment(
            input_file=input_file,
            batch_size=arguments.batch_size,
            context_seconds=arguments.context_seconds,
            clip_seconds=arguments.clip_seconds,
            ctranslate2=ctranslate2,
        )
        timer.finish()

        print("TRANSCRIPTION_CONFIGURATION")
        print(f"PYTHON={sys.executable}")
        print(f"FASTER_WHISPER={faster_whisper.__version__}")
        print(f"CTRANSLATE2={ctranslate2.__version__}")
        print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
        print(f"MODEL={MODEL_NAME}")
        print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
        print("LANGUAGE_BASE=ja")
        print("MULTILINGUAL=True")
        print("BATCHED_INFERENCE=True")
        print("VAD_FILTER=False")
        print("FULL_AUDIO_CLIPS=True")
        print(f"CLIP_SECONDS={arguments.clip_seconds:.1f}")
        print(f"CONTEXT_SECONDS={arguments.context_seconds}")
        print("BATCH_CANDIDATES=" + ",".join(str(value) for value in batch_candidates))
        print(f"OUTPUT_CREATED_AT={created_at.isoformat()}")
        print(f"INPUT={input_file}")
        print(f"OUTPUT={output_file}")

        timer.start("decode_audio")
        audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
        audio_seconds = len(audio) / SAMPLE_RATE
        clips = build_full_coverage_clips(audio_seconds, arguments.clip_seconds)
        timer.finish()

        timer.start("load_model")
        model = load_model(WhisperModel)
        timer.finish()

        timer.start("learn_opening_context")
        opening_end = min(audio_seconds, float(arguments.context_seconds))
        opening_records = transcribe_window(
            model=model,
            audio=audio,
            start_seconds=0.0,
            end_seconds=opening_end,
            hotwords=FIXED_HOTWORDS,
            language=None,
        )
        repeated_phrases = extract_repeated_phrases(opening_records)
        hotwords = build_hotwords(repeated_phrases)
        timer.finish()

        print("DISCOVERED_PHRASES")
        if repeated_phrases:
            for index, phrase in enumerate(repeated_phrases, start=1):
                print(f"PHRASE_{index:02d}={phrase}")
        else:
            print("PHRASE_NONE")

        timer.start("transcribe_full_audio")
        records, information, used_batch_size = transcribe_batched_with_fallback(
            model=model,
            BatchedInferencePipeline=BatchedInferencePipeline,
            audio=audio,
            clips=clips,
            batch_candidates=batch_candidates,
            hotwords=hotwords,
        )
        timer.finish()

        timer.start("replace_opening_with_accurate_pass")
        records = replace_window(
            base=records,
            replacement=opening_records,
            start=0.0,
            end=opening_end,
        )
        timer.finish()

        timer.start("replace_closing_with_accurate_pass")
        closing_start = max(opening_end, audio_seconds - CLOSING_REPAIR_SECONDS)
        closing_records = transcribe_window(
            model=model,
            audio=audio,
            start_seconds=closing_start,
            end_seconds=audio_seconds,
            hotwords=hotwords,
            language=None,
        )
        records = replace_window(
            base=records,
            replacement=closing_records,
            start=closing_start,
            end=audio_seconds,
        )
        records = deduplicate_overlaps(records)
        timer.finish()

        timer.start("repair_probable_language_errors")
        records, language_repairs = repair_probable_language_errors(
            model=model,
            audio=audio,
            records=records,
            repeated_phrases=repeated_phrases,
            hotwords=hotwords,
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

        transcription_seconds = timer.step_seconds("transcribe_full_audio")
        realtime_factor = (
            transcription_seconds / audio_seconds if audio_seconds > 0 else 0.0
        )

        print("TRANSCRIPTION_RESULT=PASS")
        print(f"OUTPUT_FILE={output_file}")
        print(f"BATCH_SIZE_USED={used_batch_size}")
        print(f"FULL_AUDIO_CLIP_COUNT={len(clips)}")
        print(f"SEGMENT_COUNT={len(records)}")
        print(f"AUDIO_SECONDS={audio_seconds:.3f}")
        print(f"REALTIME_FACTOR={realtime_factor:.4f}")

        print_quality_summary(
            text=transcript,
            audio_seconds=audio_seconds,
            repeated_phrases=repeated_phrases,
            removed_hallucinations=removed_hallucinations,
            repaired_languages=language_repairs,
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
