from __future__ import annotations

import argparse
import gc
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


MODEL_NAME = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
DEFAULT_BATCH_SIZE = 8
JST = timezone(timedelta(hours=9), name="JST")

INITIAL_PROMPT = (
    "NHKラジオ英会話の音声です。日本語の解説と英語のダイアログ、"
    "例文、発音練習をすべて省略せずに文字起こししてください。"
    "英語は英語表記のまま残し、同じ英文の繰り返しも残してください。"
)

KNOWN_HALLUCINATIONS = (
    "English Subtitles by the Amara.org community",
    "Subtitles by the Amara.org community",
)

EXPECTED_SECTIONS = (
    "Grammar and Vocabulary",
    "Essential Expressions",
    "Practical Usage",
    "Pronunciation Polish",
)


class TranscriptionError(RuntimeError):
    """Raised when a usable transcript cannot be produced."""


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
            "Transcribe mixed Japanese-English lesson audio with a batched "
            "large-v3 configuration tuned for accuracy and speed."
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


def validate_environment(input_file: Path, batch_size: int, ctranslate2: object) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")

    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

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


def transcribe_once(
    model: object,
    BatchedInferencePipeline: object,
    input_file: Path,
    batch_size: int,
) -> tuple[list[object], object]:
    pipeline = BatchedInferencePipeline(model=model)

    segments_iterator, information = pipeline.transcribe(
        str(input_file),
        task="transcribe",

        # The large model already handles Japanese-English code switching.
        # Keeping the base language fixed avoids unstable per-segment language
        # switching while preserving English text in mixed lesson audio.
        language="ja",
        multilingual=False,

        beam_size=5,
        best_of=5,
        patience=1.0,
        length_penalty=1.0,
        repetition_penalty=1.05,
        no_repeat_ngram_size=0,
        temperature=0.0,

        # The prompt describes the audio format without injecting lesson text.
        initial_prompt=INITIAL_PROMPT,
        hotwords=None,

        # Preserve short pronunciation drills while using VAD for batching.
        vad_filter=True,
        vad_parameters={
            "threshold": 0.30,
            "min_speech_duration_ms": 80,
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 450,
        },

        # Segment timestamps are retained, but expensive word-level alignment
        # is disabled because the output is plain text only.
        without_timestamps=False,
        word_timestamps=False,
        hallucination_silence_threshold=None,

        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,

        batch_size=batch_size,
        log_progress=True,
    )

    segments = list(segments_iterator)
    if not segments:
        raise TranscriptionError("Whisper returned no transcription segments.")

    return segments, information


def transcribe_with_batch_fallback(
    model: object,
    BatchedInferencePipeline: object,
    input_file: Path,
    batch_candidates: Iterable[int],
) -> tuple[list[object], object, int]:
    last_exception: BaseException | None = None

    for batch_size in batch_candidates:
        print()
        print("TRANSCRIPTION_ATTEMPT")
        print(f"BATCH_SIZE={batch_size}")

        try:
            segments, information = transcribe_once(
                model=model,
                BatchedInferencePipeline=BatchedInferencePipeline,
                input_file=input_file,
                batch_size=batch_size,
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


def contains_latin_text(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


def ends_sentence(text: str) -> bool:
    return bool(re.search(r"[。！？!?]$", text.strip()))


def normalize_segment_text(text: str) -> str:
    text = text.replace("\u3000", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)

    for hallucination in KNOWN_HALLUCINATIONS:
        text = text.replace(hallucination, "")

    return text.strip()


def append_text(current: str, new_text: str) -> str:
    if not current:
        return new_text

    if contains_latin_text(current[-1:]) and contains_latin_text(new_text[:1]):
        return current + " " + new_text

    return current + new_text


def format_transcript(segments: Iterable[object]) -> str:
    paragraphs: list[str] = []
    current_paragraph = ""
    previous_end: float | None = None

    for segment in segments:
        text = normalize_segment_text(str(getattr(segment, "text", "")))
        if not text:
            continue

        start = float(getattr(segment, "start", 0.0))
        end = float(getattr(segment, "end", start))
        gap = 0.0 if previous_end is None else max(0.0, start - previous_end)

        if current_paragraph and gap >= 1.25:
            paragraphs.append(current_paragraph.strip())
            current_paragraph = ""

        current_paragraph = append_text(current_paragraph, text)

        if ends_sentence(text) or len(current_paragraph) >= 320:
            paragraphs.append(current_paragraph.strip())
            current_paragraph = ""

        previous_end = end

    if current_paragraph.strip():
        paragraphs.append(current_paragraph.strip())

    transcript = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    transcript = re.sub(r"\n{3,}", "\n\n", transcript).strip()

    if not transcript:
        raise TranscriptionError("The transcription text is empty.")

    return transcript + "\n"


def detect_decoder_loop(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)

    for unit_length in range(2, 17):
        pattern = re.compile(rf"(.{{{unit_length}}})\1{{11,}}")
        match = pattern.search(compact)
        if match:
            return match.group(1)

    return None


def print_quality_summary(text: str) -> None:
    latin_characters = sum(1 for character in text if character.isascii() and character.isalpha())
    found_sections = [section for section in EXPECTED_SECTIONS if section in text]
    missing_sections = [section for section in EXPECTED_SECTIONS if section not in text]

    print("TRANSCRIPT_QUALITY_SUMMARY")
    print(f"TEXT_CHARACTERS={len(text)}")
    print(f"LATIN_CHARACTERS={latin_characters}")
    print(f"EXPECTED_SECTIONS_FOUND={len(found_sections)}/{len(EXPECTED_SECTIONS)}")
    print("FOUND_SECTIONS=" + " | ".join(found_sections))
    print("MISSING_SECTIONS=" + (" | ".join(missing_sections) if missing_sections else "NONE"))


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
        timer.finish()

        timer.start("validate_environment")
        validate_environment(input_file, arguments.batch_size, ctranslate2)
        timer.finish()

        print("TRANSCRIPTION_CONFIGURATION")
        print(f"PYTHON={sys.executable}")
        print(f"FASTER_WHISPER={faster_whisper.__version__}")
        print(f"CTRANSLATE2={ctranslate2.__version__}")
        print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
        print(f"MODEL={MODEL_NAME}")
        print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
        print("LANGUAGE=ja")
        print("MULTILINGUAL=False")
        print("BATCHED_INFERENCE=True")
        print("VAD_FILTER=True")
        print("WORD_TIMESTAMPS=False")
        print("BEAM_SIZE=5")
        print("HOTWORDS=None")
        print("BATCH_CANDIDATES=" + ",".join(str(value) for value in batch_candidates))
        print(f"OUTPUT_CREATED_AT={created_at.isoformat()}")
        print(f"INPUT={input_file}")
        print(f"OUTPUT={output_file}")

        timer.start("load_model")
        model = load_model(WhisperModel)
        timer.finish()

        timer.start("transcribe_audio")
        segments, information, used_batch_size = transcribe_with_batch_fallback(
            model=model,
            BatchedInferencePipeline=BatchedInferencePipeline,
            input_file=input_file,
            batch_candidates=batch_candidates,
        )
        timer.finish()

        timer.start("format_and_validate_transcript")
        transcript = format_transcript(segments)
        repeated_unit = detect_decoder_loop(transcript)
        if repeated_unit is not None:
            raise TranscriptionError(
                "An obvious decoder repetition loop was detected: "
                f"{repeated_unit!r}"
            )
        timer.finish()

        timer.start("write_output_file")
        write_text_atomically(output_file, transcript)
        timer.finish()

        audio_seconds = float(getattr(information, "duration", 0.0))
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
        print_quality_summary(transcript)

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
