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
MAX_REPAIR_SEGMENTS = 8
SAMPLE_RATE = 16000
JST = timezone(timedelta(hours=9), name="JST")

FIXED_HOTWORDS = (
    "Lesson, Grammar and Vocabulary, Essential Expressions, Practical Usage, "
    "Pronunciation Polish"
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
        "プラクティカルユーセージ",
        "プラクティカル・ユーセージ",
    ),
    "Pronunciation Polish": (
        "Pronunciation Polish",
        "プロナンシエーション・ポリッシュ",
        "発音練習",
    ),
}

KNOWN_HALLUCINATION_PATTERNS = (
    r"English Subtitles by the Amara\.org community",
    r"Subtitles by the Amara\.org community",
    r"日本語の解説と英語のダイアログ.{0,120}文字起こし",
    r"同じ英語の繰り返しも残し(?:てください)?(?:[、, ]*同じ英語の繰り返しも残し(?:てください)?)*",
    r"英語の繰り返しと英語の繰り返しも残し.{0,120}",
)


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

    def step_seconds(self, name: str) -> float:
        for step_name, elapsed, _result in self.steps:
            if step_name == name:
                return elapsed
        return 0.0

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
            "Create an AI-handoff transcript from Japanese-English lesson audio."
        )
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--context-seconds",
        type=int,
        default=DEFAULT_CONTEXT_SECONDS,
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
    if ctranslate2.get_cuda_device_count() < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")
    if COMPUTE_TYPE not in ctranslate2.get_supported_compute_types("cuda"):
        raise TranscriptionError(f"{COMPUTE_TYPE} is not supported by the GPU.")


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


def to_record(segment: object, offset_seconds: float = 0.0) -> SegmentRecord:
    return SegmentRecord(
        start=float(getattr(segment, "start", 0.0)) + offset_seconds,
        end=float(getattr(segment, "end", 0.0)) + offset_seconds,
        text=str(getattr(segment, "text", "")).strip(),
        avg_logprob=float(getattr(segment, "avg_logprob", -1.0)),
        compression_ratio=float(getattr(segment, "compression_ratio", 0.0)),
        no_speech_prob=float(getattr(segment, "no_speech_prob", 0.0)),
    )


def normalize_english(text: str) -> str:
    return " ".join(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text.lower()))


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
        if not 4 <= words <= 24:
            continue
        compact_length = len(re.sub(r"\s+", "", piece))
        if compact_length == 0:
            continue
        if latin_character_count(piece) / compact_length < 0.65:
            continue
        candidates.append(piece)
    return candidates


def choose_representative(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]

    best = items[0]
    best_score = -1.0
    for candidate in items:
        normalized = normalize_english(candidate)
        score = sum(
            SequenceMatcher(None, normalized, normalize_english(other)).ratio()
            for other in items
        ) / len(items)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def extract_repeated_phrases(records: Iterable[SegmentRecord]) -> list[str]:
    candidates: list[str] = []
    for record in records:
        candidates.extend(extract_english_candidates(record.text))

    clusters: list[list[str]] = []
    for candidate in candidates:
        normalized = normalize_english(candidate)
        if not normalized:
            continue
        matched_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None,
                normalized,
                normalize_english(cluster[0]),
            ).ratio()
            if ratio > best_ratio:
                matched_index = index
                best_ratio = ratio
        if matched_index is not None and best_ratio >= 0.82:
            clusters[matched_index].append(candidate)
        else:
            clusters.append([candidate])

    repeated: list[tuple[int, str]] = []
    for cluster in clusters:
        if len(cluster) >= 2:
            repeated.append((len(cluster), choose_representative(cluster)))
    repeated.sort(key=lambda item: (-item[0], -english_word_count(item[1])))
    return [phrase for _count, phrase in repeated[:8]]


def build_hotwords(repeated_phrases: Iterable[str]) -> str:
    values = [FIXED_HOTWORDS]
    values.extend(repeated_phrases)
    return ", ".join(values)[:800]


def run_context_pass(
    model: object,
    audio: object,
    context_seconds: int,
) -> list[str]:
    sample_count = min(len(audio), context_seconds * SAMPLE_RATE)
    context_audio = audio[:sample_count]
    segments_iterator, _information = model.transcribe(
        context_audio,
        task="transcribe",
        language=None,
        multilingual=True,
        beam_size=3,
        best_of=3,
        temperature=0.0,
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        hotwords=FIXED_HOTWORDS,
        log_progress=False,
    )
    records = [to_record(segment) for segment in segments_iterator]
    return extract_repeated_phrases(records)


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
        vad_filter=True,
        vad_parameters={
            "threshold": 0.20,
            "min_speech_duration_ms": 50,
            "min_silence_duration_ms": 700,
            "speech_pad_ms": 500,
        },
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=False,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        batch_size=batch_size,
        log_progress=True,
    )

    records = [to_record(segment) for segment in segments_iterator]
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
            records, information = transcribe_full_once(
                model,
                BatchedInferencePipeline,
                audio,
                batch_size,
                hotwords,
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


def transcribe_slice(
    model: object,
    audio: object,
    start_seconds: float,
    end_seconds: float,
    language: str | None,
    hotwords: str,
) -> SegmentRecord:
    padded_start = max(0.0, start_seconds - 0.75)
    padded_end = min(len(audio) / SAMPLE_RATE, end_seconds + 0.75)
    start_sample = int(padded_start * SAMPLE_RATE)
    end_sample = int(padded_end * SAMPLE_RATE)
    audio_slice = audio[start_sample:end_sample]

    segments_iterator, _information = model.transcribe(
        audio_slice,
        task="transcribe",
        language=language,
        multilingual=(language is None),
        beam_size=5,
        best_of=5,
        patience=1.0,
        temperature=(0.0, 0.2, 0.4),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=True,
        word_timestamps=False,
        hotwords=hotwords,
        log_progress=False,
    )

    texts: list[str] = []
    logprobs: list[float] = []
    for segment in segments_iterator:
        text = str(getattr(segment, "text", "")).strip()
        if text:
            texts.append(text)
            logprobs.append(float(getattr(segment, "avg_logprob", -1.0)))

    return SegmentRecord(
        start=start_seconds,
        end=end_seconds,
        text=" ".join(texts).strip(),
        avg_logprob=(sum(logprobs) / len(logprobs) if logprobs else -10.0),
        compression_ratio=0.0,
        no_speech_prob=0.0,
    )


def canonicalize_learned_phrase(
    text: str,
    repeated_phrases: Iterable[str],
) -> str:
    normalized = normalize_english(text)
    if not normalized or english_word_count(text) < 4:
        return text

    best_phrase: str | None = None
    best_ratio = 0.0
    for phrase in repeated_phrases:
        ratio = SequenceMatcher(
            None,
            normalized,
            normalize_english(phrase),
        ).ratio()
        if ratio > best_ratio:
            best_phrase = phrase
            best_ratio = ratio

    if best_phrase is not None and best_ratio >= 0.76:
        return best_phrase
    return text


def is_probable_wrong_language(record: SegmentRecord) -> bool:
    duration = max(0.0, record.end - record.start)
    return (
        duration >= 6.0
        and english_word_count(record.text) >= 16
        and japanese_character_count(record.text) <= 4
        and record.avg_logprob <= -0.55
    )


def should_repair(record: SegmentRecord) -> bool:
    lowered = record.text.lower()
    return (
        record.avg_logprob <= -0.80
        or record.compression_ratio >= 2.35
        or is_probable_wrong_language(record)
        or "macaque" in lowered
    )


def choose_repair_candidate(
    original: SegmentRecord,
    automatic: SegmentRecord,
    japanese: SegmentRecord,
    repeated_phrases: Iterable[str],
) -> SegmentRecord:
    automatic.text = canonicalize_learned_phrase(
        automatic.text,
        repeated_phrases,
    )

    if is_probable_wrong_language(original):
        if (
            japanese_character_count(japanese.text)
            >= max(10, japanese_character_count(original.text) + 8)
            and japanese.avg_logprob >= original.avg_logprob - 0.25
        ):
            return japanese

    if "macaque" in original.text.lower() and "macaque" not in automatic.text.lower():
        if automatic.text:
            return automatic

    candidates = [original, automatic]
    candidates = [candidate for candidate in candidates if candidate.text.strip()]
    return max(candidates, key=lambda candidate: candidate.avg_logprob)


def repair_suspicious_segments(
    model: object,
    audio: object,
    records: list[SegmentRecord],
    hotwords: str,
    repeated_phrases: Iterable[str],
) -> tuple[list[SegmentRecord], int]:
    repaired: list[SegmentRecord] = []
    repair_count = 0

    for record in records:
        if repair_count >= MAX_REPAIR_SEGMENTS or not should_repair(record):
            record.text = canonicalize_learned_phrase(
                record.text,
                repeated_phrases,
            )
            repaired.append(record)
            continue

        automatic = transcribe_slice(
            model,
            audio,
            record.start,
            record.end,
            None,
            hotwords,
        )
        japanese = transcribe_slice(
            model,
            audio,
            record.start,
            record.end,
            "ja",
            hotwords,
        )
        selected = choose_repair_candidate(
            record,
            automatic,
            japanese,
            repeated_phrases,
        )
        repaired.append(selected)
        if selected.text != record.text:
            repair_count += 1

    return repaired, repair_count


def remove_known_hallucinations(text: str) -> tuple[str, int]:
    result = text
    removed = 0
    for pattern in KNOWN_HALLUCINATION_PATTERNS:
        result, count = re.subn(pattern, "", result, flags=re.IGNORECASE)
        removed += count
    return re.sub(r"[ \t]+", " ", result).strip(), removed


def split_section_heading(text: str) -> tuple[str | None, str]:
    stripped = text.strip()
    for canonical, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            match = re.match(re.escape(alias), stripped, flags=re.IGNORECASE)
            if match:
                remainder = stripped[match.end() :].lstrip(" ：:-–—")
                return canonical, remainder
    return None, stripped


def format_transcript(records: Iterable[SegmentRecord]) -> tuple[str, int]:
    paragraphs: list[str] = []
    removed_hallucinations = 0
    last_heading: str | None = None

    for record in records:
        text = record.text.replace("\u3000", " ").strip()
        text, removed = remove_known_hallucinations(text)
        removed_hallucinations += removed
        if not text:
            continue

        heading, remainder = split_section_heading(text)
        if heading is not None:
            if heading != last_heading:
                paragraphs.append(heading)
                last_heading = heading
            if remainder:
                paragraphs.append(remainder)
        else:
            paragraphs.append(text)

    transcript = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    transcript = re.sub(r"\n{3,}", "\n\n", transcript).strip()
    if not transcript:
        raise TranscriptionError("The transcription text is empty.")
    return transcript + "\n", removed_hallucinations


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


def count_learned_phrase_occurrences(
    text: str,
    repeated_phrases: Iterable[str],
) -> int:
    normalized_text = normalize_english(text)
    count = 0
    for phrase in repeated_phrases:
        normalized_phrase = normalize_english(phrase)
        if normalized_phrase:
            count += normalized_text.count(normalized_phrase)
    return count


def print_quality_summary(
    text: str,
    audio_seconds: float,
    repeated_phrases: list[str],
    repaired_segments: int,
    removed_hallucinations: int,
) -> bool:
    found_sections = find_sections(text)
    missing_sections = [
        section for section in SECTION_ALIASES if section not in found_sections
    ]
    minimum_characters = max(2500, int(audio_seconds * 4.5))
    decoder_loop = detect_decoder_loop(text)
    phrase_occurrences = count_learned_phrase_occurrences(
        text,
        repeated_phrases,
    )
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
    print(f"SUSPICIOUS_SEGMENTS_REPAIRED={repaired_segments}")
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
            input_file,
            arguments.batch_size,
            arguments.context_seconds,
            ctranslate2,
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
        print("VAD_FILTER=True")
        print("VAD_THRESHOLD=0.20")
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
        repeated_phrases = run_context_pass(
            model,
            audio,
            arguments.context_seconds,
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
        records, information, used_batch_size = transcribe_full_with_fallback(
            model,
            BatchedInferencePipeline,
            audio,
            batch_candidates,
            hotwords,
        )
        timer.finish()

        timer.start("repair_suspicious_segments")
        records, repair_count = repair_suspicious_segments(
            model,
            audio,
            records,
            hotwords,
            repeated_phrases,
        )
        timer.finish()

        timer.start("format_and_validate_transcript")
        transcript, removed_hallucinations = format_transcript(records)
        decoder_loop = detect_decoder_loop(transcript)
        if decoder_loop is not None:
            raise TranscriptionError(
                f"An obvious decoder repetition loop was detected: {decoder_loop!r}"
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
        print(f"SEGMENT_COUNT={len(records)}")
        print(f"AUDIO_SECONDS={audio_seconds:.3f}")
        print(f"REALTIME_FACTOR={realtime_factor:.4f}")
        print_quality_summary(
            transcript,
            audio_seconds,
            repeated_phrases,
            repair_count,
            removed_hallucinations,
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
