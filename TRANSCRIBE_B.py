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
DEFAULT_CONTEXT_SECONDS = 120
MAX_REPAIR_SEGMENTS = 6
SAMPLE_RATE = 16000
JST = timezone(timedelta(hours=9), name="JST")

FIXED_HOTWORDS = (
    "Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish"
)

KNOWN_HALLUCINATION_PATTERNS = (
    r"English Subtitles by the Amara\.org community",
    r"Subtitles by the Amara\.org community",
    r"日本語の解説と英語のダイアログ.{0,80}文字起こし",
    r"同じ英語の繰り返しも残し(?:てください)?(?:[、, ]*同じ英語の繰り返しも残し(?:てください)?)*",
    r"英語の繰り返しと英語の繰り返しも残し.{0,80}",
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
    """Raised when a usable transcript cannot be produced."""


@dataclass
class SegmentRecord:
    start: float
    end: float
    text: str
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float


class StepTimer:
    """Measure and summarize processing steps."""

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
            "Create an AI-handoff transcript from mixed Japanese-English "
            "lesson audio using adaptive faster-whisper processing."
        )
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_FILE,
        help="Input audio path. Relative paths are resolved from this script.",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIRECTORY,
        help=(
            "Output directory. Relative paths are resolved from this script. "
            "The output file name is created from the JST execution time."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Initial GPU batch size. Default: 8.",
    )
    parser.add_argument(
        "--context-seconds",
        type=int,
        default=DEFAULT_CONTEXT_SECONDS,
        help="Opening audio duration used to learn repeated English phrases.",
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
    ctranslate2: object,
) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")
    if context_seconds < 30:
        raise ValueError("--context-seconds must be at least 30.")

    cuda_device_count = ctranslate2.get_cuda_device_count()
    if cuda_device_count < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")

    supported_compute_types = ctranslate2.get_supported_compute_types("cuda")
    if COMPUTE_TYPE not in supported_compute_types:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is not supported. "
            f"Supported types: {sorted(supported_compute_types)}"
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


def to_segment_record(segment: object, offset_seconds: float = 0.0) -> SegmentRecord:
    return SegmentRecord(
        start=float(getattr(segment, "start", 0.0)) + offset_seconds,
        end=float(getattr(segment, "end", 0.0)) + offset_seconds,
        text=str(getattr(segment, "text", "")).strip(),
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
    )


def normalize_english_for_comparison(text: str) -> str:
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text.lower())
    return " ".join(words)


def english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))


def japanese_character_count(text: str) -> int:
    return len(re.findall(r"[ぁ-んァ-ヶ一-龯々]", text))


def latin_character_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


def extract_english_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    pieces = re.split(r"(?<=[.!?])\s+|[\r\n]+", text)

    for piece in pieces:
        piece = re.sub(r"\s+", " ", piece).strip(" -–—\t")
        word_count = english_word_count(piece)
        if word_count < 4 or word_count > 20:
            continue

        non_space_count = len(re.sub(r"\s+", "", piece))
        if non_space_count == 0:
            continue

        if latin_character_count(piece) / non_space_count < 0.65:
            continue

        candidates.append(piece)

    return candidates


def choose_cluster_representative(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]

    best_item = items[0]
    best_score = -1.0

    for candidate in items:
        candidate_normalized = normalize_english_for_comparison(candidate)
        similarities = [
            SequenceMatcher(
                None,
                candidate_normalized,
                normalize_english_for_comparison(other),
            ).ratio()
            for other in items
        ]
        score = sum(similarities) / len(similarities)
        if score > best_score:
            best_item = candidate
            best_score = score

    return best_item


def extract_repeated_english_phrases(segments: Iterable[SegmentRecord]) -> list[str]:
    candidates: list[str] = []
    for segment in segments:
        candidates.extend(extract_english_candidates(segment.text))

    clusters: list[list[str]] = []

    for candidate in candidates:
        normalized = normalize_english_for_comparison(candidate)
        if not normalized:
            continue

        best_index: int | None = None
        best_ratio = 0.0

        for index, cluster in enumerate(clusters):
            representative = normalize_english_for_comparison(cluster[0])
            ratio = SequenceMatcher(None, normalized, representative).ratio()
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
        representative = choose_cluster_representative(cluster)
        repeated.append((len(cluster), representative))

    repeated.sort(key=lambda item: (-item[0], -english_word_count(item[1])))
    return [phrase for _count, phrase in repeated[:6]]


def build_hotwords(repeated_phrases: Iterable[str]) -> str:
    values = [FIXED_HOTWORDS]
    values.extend(repeated_phrases)
    return ", ".join(values)[:600]


def run_context_pass(
    model: object,
    audio: object,
    context_seconds: int,
) -> tuple[list[SegmentRecord], list[str]]:
    sample_count = min(len(audio), context_seconds * SAMPLE_RATE)
    context_audio = audio[:sample_count]

    segments_iterator, _information = model.transcribe(
        context_audio,
        task="transcribe",
        language=None,
        multilingual=True,
        beam_size=3,
        best_of=3,
        patience=1.0,
        temperature=0.0,
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        hotwords=FIXED_HOTWORDS,
        log_progress=False,
    )

    records = [to_segment_record(segment) for segment in segments_iterator]
    phrases = extract_repeated_english_phrases(records)
    return records, phrases


def transcribe_full_once(
    model: object,
    BatchedInferencePipeline: object,
    audio: object,
    batch_size: int,
    hotwords: str,
) -> tuple[list[SegmentRecord], object]:
    pipeline = BatchedInferencePipeline(model=model)

    segments_iterator, information = pipeline.transcribe(
        audio,
        task="transcribe",
        language=None,
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
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        batch_size=batch_size,
        log_progress=True,
    )

    records = [to_segment_record(segment) for segment in segments_iterator]
    if not records:
        raise TranscriptionError("Whisper returned no transcription segments.")

    return records, information


def transcribe_full_with_fallback(
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
            segments, information = transcribe_full_once(
                model=model,
                BatchedInferencePipeline=BatchedInferencePipeline,
                audio=audio,
                batch_size=batch_size,
                hotwords=hotwords,
            )
            return segments, information, batch_size

        except Exception as exception:
            last_exception = exception
            if not is_cuda_memory_error(exception):
                raise

            print(f"GPU_MEMORY_RETRY=BATCH_SIZE_{batch_size}")
            gc.collect()

    raise TranscriptionError(
        "Transcription failed even at the smallest batch size."
    ) from last_exception


def remove_known_hallucinations(text: str) -> tuple[str, int]:
    result = text
    removed_count = 0

    for pattern in KNOWN_HALLUCINATION_PATTERNS:
        result, count = re.subn(pattern, "", result, flags=re.IGNORECASE)
        removed_count += count

    return re.sub(r"\s+", " ", result).strip(), removed_count


def canonicalize_repeated_phrase(text: str, phrases: Iterable[str]) -> str:
    text_normalized = normalize_english_for_comparison(text)
    if not text_normalized:
        return text

    text_word_count = english_word_count(text)
    if text_word_count < 4:
        return text

    best_phrase: str | None = None
    best_ratio = 0.0

    for phrase in phrases:
        phrase_normalized = normalize_english_for_comparison(phrase)
        phrase_word_count = english_word_count(phrase)
        if phrase_word_count == 0:
            continue

        size_ratio = text_word_count / phrase_word_count
        if not 0.65 <= size_ratio <= 1.35:
            continue

        ratio = SequenceMatcher(None, text_normalized, phrase_normalized).ratio()
        if ratio > best_ratio:
            best_phrase = phrase
            best_ratio = ratio

    if best_phrase is not None and best_ratio >= 0.76:
        return best_phrase

    return text


def contains_repeated_phrase(text: str, phrases: Iterable[str]) -> bool:
    normalized = normalize_english_for_comparison(text)
    if not normalized:
        return False

    for phrase in phrases:
        phrase_normalized = normalize_english_for_comparison(phrase)
        if phrase_normalized and phrase_normalized in normalized:
            return True

    return False


def should_repair_as_japanese(
    segment: SegmentRecord,
    repeated_phrases: Iterable[str],
) -> bool:
    text = segment.text
    duration = max(0.0, segment.end - segment.start)
    latin = latin_character_count(text)
    japanese = japanese_character_count(text)
    word_count = english_word_count(text)

    if duration < 6.0 or word_count < 18:
        return False
    if japanese > 5:
        return False
    if latin < 60:
        return False
    if segment.avg_logprob > -0.55:
        return False
    if contains_repeated_phrase(text, repeated_phrases):
        return False
    if any(section.lower() in text.lower() for section in SECTION_ALIASES):
        return False

    return True


def transcribe_slice_as_japanese(
    model: object,
    audio: object,
    start_seconds: float,
    end_seconds: float,
) -> tuple[str, float]:
    padded_start = max(0.0, start_seconds - 0.75)
    padded_end = min(len(audio) / SAMPLE_RATE, end_seconds + 0.75)
    start_sample = int(padded_start * SAMPLE_RATE)
    end_sample = int(padded_end * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]

    segments_iterator, _information = model.transcribe(
        audio_slice,
        task="transcribe",
        language="ja",
        multilingual=False,
        beam_size=5,
        best_of=5,
        patience=1.0,
        temperature=(0.0, 0.2, 0.4),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=True,
        word_timestamps=False,
        hotwords=FIXED_HOTWORDS,
        log_progress=False,
    )

    texts: list[str] = []
    logprobs: list[float] = []

    for segment in segments_iterator:
        text = str(getattr(segment, "text", "")).strip()
        if text:
            texts.append(text)
            logprobs.append(float(getattr(segment, "avg_logprob", -1.0)))

    combined = " ".join(texts).strip()
    average_logprob = sum(logprobs) / len(logprobs) if logprobs else -10.0
    return combined, average_logprob


def repair_probable_translations(
    model: object,
    audio: object,
    segments: list[SegmentRecord],
    repeated_phrases: Iterable[str],
) -> tuple[list[SegmentRecord], int]:
    repaired_segments: list[SegmentRecord] = []
    repair_count = 0

    for segment in segments:
        if repair_count >= MAX_REPAIR_SEGMENTS or not should_repair_as_japanese(
            segment,
            repeated_phrases,
        ):
            repaired_segments.append(segment)
            continue

        repaired_text, repaired_logprob = transcribe_slice_as_japanese(
            model=model,
            audio=audio,
            start_seconds=segment.start,
            end_seconds=segment.end,
        )

        original_japanese = japanese_character_count(segment.text)
        repaired_japanese = japanese_character_count(repaired_text)

        if (
            repaired_japanese >= max(12, original_japanese + 10)
            and repaired_logprob >= segment.avg_logprob - 0.20
        ):
            repaired_segments.append(
                SegmentRecord(
                    start=segment.start,
                    end=segment.end,
                    text=repaired_text,
                    avg_logprob=repaired_logprob,
                    compression_ratio=segment.compression_ratio,
                    no_speech_prob=segment.no_speech_prob,
                )
            )
            repair_count += 1
        else:
            repaired_segments.append(segment)

    return repaired_segments, repair_count


def split_section_heading(text: str) -> tuple[str | None, str]:
    stripped = text.strip()

    for canonical, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            match = re.match(re.escape(alias), stripped, flags=re.IGNORECASE)
            if match:
                remainder = stripped[match.end() :].lstrip(" ：:-–—")
                return canonical, remainder

    return None, stripped


def clean_segment_text(
    text: str,
    repeated_phrases: Iterable[str],
) -> tuple[str, int]:
    text = text.replace("\u3000", " ")
    text, removed_count = remove_known_hallucinations(text)
    text = canonicalize_repeated_phrase(text, repeated_phrases)
    return text.strip(), removed_count


def format_transcript(
    segments: Iterable[SegmentRecord],
    repeated_phrases: Iterable[str],
) -> tuple[str, int]:
    paragraphs: list[str] = []
    removed_hallucinations = 0
    last_heading: str | None = None

    for segment in segments:
        text, removed_count = clean_segment_text(segment.text, repeated_phrases)
        removed_hallucinations += removed_count
        if not text:
            continue

        heading, remainder = split_section_heading(text)
        if heading is not None:
            if heading != last_heading:
                paragraphs.append(heading)
                last_heading = heading
            if remainder:
                paragraphs.append(remainder)
            continue

        paragraphs.append(text)

    transcript = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    transcript = re.sub(r"\n{3,}", "\n\n", transcript).strip()

    if not transcript:
        raise TranscriptionError("The transcription text is empty.")

    return transcript + "\n", removed_hallucinations


def detect_decoder_loop(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)

    for unit_length in range(2, 17):
        pattern = re.compile(rf"(.{{{unit_length}}})\1{{9,}}")
        match = pattern.search(compact)
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
    normalized_text = normalize_english_for_comparison(text)
    count = 0

    for phrase in phrases:
        normalized_phrase = normalize_english_for_comparison(phrase)
        if normalized_phrase:
            count += normalized_text.count(normalized_phrase)

    return count


def print_quality_summary(
    text: str,
    audio_seconds: float,
    repeated_phrases: list[str],
    removed_hallucinations: int,
    repaired_segments: int,
) -> bool:
    found_sections = find_sections(text)
    missing_sections = [
        section for section in SECTION_ALIASES if section not in found_sections
    ]
    phrase_occurrences = count_phrase_occurrences(text, repeated_phrases)
    minimum_characters = max(2500, int(audio_seconds * 4.5))
    decoder_loop = detect_decoder_loop(text)

    handoff_ready = (
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
    print(f"PROBABLE_TRANSLATIONS_REPAIRED={repaired_segments}")
    print(f"KNOWN_HALLUCINATIONS_REMOVED={removed_hallucinations}")
    print(f"DECODER_LOOP={'NONE' if decoder_loop is None else decoder_loop}")
    print(f"AI_HANDOFF_READY={'YES' if handoff_ready else 'NO'}")

    return handoff_ready


def write_text_atomically(output_file: Path, text: str) -> None:
    temporary_file = output_file.with_name(output_file.name + ".part")

    if temporary_file.exists():
        temporary_file.unlink()

    temporary_file.write_text(text, encoding="utf-8")
    temporary_file.replace(output_file)


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
        print("LANGUAGE=auto")
        print("MULTILINGUAL=True")
        print("BATCHED_INFERENCE=True")
        print("VAD_FILTER=False")
        print("WORD_TIMESTAMPS=False")
        print("INITIAL_PROMPT=None")
        print(f"CONTEXT_SECONDS={arguments.context_seconds}")
        print("BATCH_CANDIDATES=" + ",".join(str(value) for value in batch_candidates))
        print(f"OUTPUT_CREATED_AT={created_at.isoformat()}")
        print(f"INPUT={input_file}")
        print(f"OUTPUT={output_file}")

        timer.start("decode_audio")
        audio = decode_audio(str(input_file), sampling_rate=SAMPLE_RATE)
        audio_seconds = len(audio) / SAMPLE_RATE
        timer.finish()

        timer.start("load_model")
        model = load_model(WhisperModel)
        timer.finish()

        timer.start("learn_repeated_phrases")
        _context_segments, repeated_phrases = run_context_pass(
            model=model,
            audio=audio,
            context_seconds=arguments.context_seconds,
        )
        hotwords = build_hotwords(repeated_phrases)
        timer.finish()

        print("DISCOVERED_PHRASES")
        if repeated_phrases:
            for index, phrase in enumerate(repeated_phrases, start=1):
                print(f"PHRASE_{index:02d}={phrase}")
        else:
            print("PHRASE_NONE")

        timer.start("transcribe_audio")
        segments, information, used_batch_size = transcribe_full_with_fallback(
            model=model,
            BatchedInferencePipeline=BatchedInferencePipeline,
            audio=audio,
            batch_candidates=batch_candidates,
            hotwords=hotwords,
        )
        timer.finish()

        timer.start("repair_probable_translations")
        segments, repaired_segment_count = repair_probable_translations(
            model=model,
            audio=audio,
            segments=segments,
            repeated_phrases=repeated_phrases,
        )
        timer.finish()

        timer.start("format_and_validate_transcript")
        transcript, removed_hallucinations = format_transcript(
            segments=segments,
            repeated_phrases=repeated_phrases,
        )
        decoder_loop = detect_decoder_loop(transcript)
        if decoder_loop is not None:
            raise TranscriptionError(
                "An obvious decoder repetition loop was detected: "
                f"{decoder_loop!r}"
            )
        timer.finish()

        timer.start("write_output_file")
        write_text_atomically(output_file, transcript)
        timer.finish()

        transcription_seconds = timer.step_seconds("transcribe_audio")
        realtime_factor = (
            transcription_seconds / audio_seconds if audio_seconds > 0 else 0.0
        )

        print("TRANSCRIPTION_RESULT=PASS")
        print(f"OUTPUT_FILE={output_file}")
        print(f"BATCH_SIZE_USED={used_batch_size}")
        print(f"SEGMENT_COUNT={len(segments)}")
        print(f"AUDIO_SECONDS={audio_seconds:.3f}")
        print(f"REALTIME_FACTOR={realtime_factor:.4f}")

        print_quality_summary(
            text=transcript,
            audio_seconds=audio_seconds,
            repeated_phrases=repeated_phrases,
            removed_hallucinations=removed_hallucinations,
            repaired_segments=repaired_segment_count,
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
        print(
            f"ERROR={type(exception).__name__}: {exception}",
            file=sys.stderr,
        )
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
