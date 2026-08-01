from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import ctranslate2
import faster_whisper
from faster_whisper import WhisperModel


MODEL_NAME = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
DEFAULT_INPUT_FILE = "b.m4a"
DEFAULT_OUTPUT_DIRECTORY = "stt_test"
JST = timezone(timedelta(hours=9), name="JST")

# Vocabulary hints only. These are intentionally generic so that the program
# can be used for other Japanese-English lessons as well.
HOTWORDS = (
    "NHK ラジオ英会話 Lesson Grammar and Vocabulary Essential Expressions "
    "Practical Usage Pronunciation Polish dialogue key sentence pronunciation "
    "英語 日本語 文法 語彙 発音 音読 練習"
)


class TranscriptionError(RuntimeError):
    """Raised when a usable transcript cannot be produced."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe mixed Japanese-English audio locally with an "
            "accuracy-oriented faster-whisper configuration."
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
    return parser.parse_args()


def resolve_path(script_directory: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (script_directory / path).resolve()


def create_output_path(output_directory: Path, created_at: datetime) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.astimezone(JST).strftime("%Y%m%d_%H%M%S")
    candidate = output_directory / f"{timestamp}.txt"
    sequence = 2

    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = output_directory / f"{timestamp}_{sequence:02d}.txt"
        sequence += 1

    return candidate


def validate_environment(input_file: Path) -> None:
    if not input_file.is_file():
        raise FileNotFoundError(f"Input file was not found: {input_file}")

    cuda_device_count = ctranslate2.get_cuda_device_count()
    if cuda_device_count < 1:
        raise TranscriptionError("CTranslate2 cannot detect a CUDA GPU.")

    supported_compute_types = ctranslate2.get_supported_compute_types("cuda")
    if COMPUTE_TYPE not in supported_compute_types:
        raise TranscriptionError(
            f"{COMPUTE_TYPE} is not supported. "
            f"Supported types: {sorted(supported_compute_types)}"
        )


def load_model() -> WhisperModel:
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


def transcribe_audio(model: WhisperModel, input_file: Path) -> tuple[list[object], object]:
    print("TRANSCRIPTION_START")

    segments_iterator, information = model.transcribe(
        str(input_file),
        task="transcribe",

        # Detect the language automatically and re-evaluate it for internal
        # segments. This is required for Japanese explanations alternating
        # with English dialogue and pronunciation exercises.
        language=None,
        multilingual=True,

        # Accuracy-oriented decoding with fallback for difficult sections.
        beam_size=5,
        best_of=5,
        patience=1.0,
        temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        length_penalty=1.0,

        # Retain short phrases and pauses used in pronunciation drills.
        vad_filter=False,

        # This reduces the decoder repetition loop seen in the earlier test.
        condition_on_previous_text=False,
        prompt_reset_on_temperature=0.5,

        # Timestamps are used internally to create readable paragraphs, but
        # only a plain UTF-8 text file is written.
        without_timestamps=False,
        word_timestamps=True,
        hallucination_silence_threshold=2.0,

        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        hotwords=HOTWORDS,
        log_progress=True,
    )

    segments = list(segments_iterator)
    if not segments:
        raise TranscriptionError("Whisper returned no transcription segments.")

    return segments, information


def contains_latin_text(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


def ends_sentence(text: str) -> bool:
    return bool(re.search(r"[。！？!?]$", text.strip()))


def normalize_segment_text(text: str) -> str:
    text = text.replace("\u3000", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text


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

        # A clear pause starts a new paragraph. Short pauses remain in the same
        # paragraph so repeated English practice phrases are not discarded.
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

    # Only flag very long, obvious loops. Normal repetition exercises such as
    # repeating one sentence two or three times are preserved.
    for unit_length in range(2, 17):
        pattern = re.compile(rf"(.{{{unit_length}}})\1{{11,}}")
        match = pattern.search(compact)
        if match:
            return match.group(1)

    return None


def write_text_atomically(output_file: Path, text: str) -> None:
    temporary_file = output_file.with_name(output_file.name + ".part")

    if temporary_file.exists():
        temporary_file.unlink()

    temporary_file.write_text(text, encoding="utf-8")
    temporary_file.replace(output_file)


def main() -> int:
    arguments = parse_arguments()
    script_directory = Path(__file__).resolve().parent
    created_at = datetime.now(JST)

    input_file = resolve_path(script_directory, arguments.input)
    output_directory = resolve_path(script_directory, arguments.output_dir)
    output_file = create_output_path(output_directory, created_at)

    try:
        validate_environment(input_file)

        print("TRANSCRIPTION_CONFIGURATION")
        print(f"PYTHON={sys.executable}")
        print(f"FASTER_WHISPER={faster_whisper.__version__}")
        print(f"CTRANSLATE2={ctranslate2.__version__}")
        print(f"CUDA_DEVICES={ctranslate2.get_cuda_device_count()}")
        print(f"MODEL={MODEL_NAME}")
        print(f"COMPUTE_TYPE={COMPUTE_TYPE}")
        print("LANGUAGE=auto")
        print("MULTILINGUAL=True")
        print("VAD_FILTER=False")
        print("BEAM_SIZE=5")
        print("CONDITION_ON_PREVIOUS_TEXT=False")
        print(f"OUTPUT_CREATED_AT={created_at.isoformat()}")
        print(f"INPUT={input_file}")
        print(f"OUTPUT={output_file}")

        model = load_model()
        started_at = time.perf_counter()

        segments, information = transcribe_audio(model, input_file)
        transcript = format_transcript(segments)

        repeated_unit = detect_decoder_loop(transcript)
        if repeated_unit is not None:
            raise TranscriptionError(
                "An obvious decoder repetition loop was detected: "
                f"{repeated_unit!r}"
            )

        write_text_atomically(output_file, transcript)

        elapsed_seconds = time.perf_counter() - started_at
        audio_seconds = float(getattr(information, "duration", 0.0))
        realtime_factor = (
            elapsed_seconds / audio_seconds if audio_seconds > 0 else 0.0
        )

    except KeyboardInterrupt:
        print("TRANSCRIPTION_RESULT=CANCELLED", file=sys.stderr)
        return 130
    except Exception as exception:
        print("TRANSCRIPTION_RESULT=FAILED", file=sys.stderr)
        print(
            f"ERROR={type(exception).__name__}: {exception}",
            file=sys.stderr,
        )
        return 1

    print("TRANSCRIPTION_RESULT=PASS")
    print(f"OUTPUT_FILE={output_file}")
    print(f"SEGMENT_COUNT={len(segments)}")
    print(f"AUDIO_SECONDS={audio_seconds:.3f}")
    print(f"ELAPSED_SECONDS={elapsed_seconds:.3f}")
    print(f"REALTIME_FACTOR={realtime_factor:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
