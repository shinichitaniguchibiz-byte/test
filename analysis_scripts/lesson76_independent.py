from __future__ import annotations

import gc
import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from faster_whisper import WhisperModel

ROOT = Path("independent_audio_analysis")
HOTWORDS = (
    "NHK ラジオ英会話 Lesson Grammar and Vocabulary Essential Expressions "
    "Practical Usage Pronunciation Polish Theo Jennifer Miller Dad Mom birthday "
    "coming up booked dinner cake homemade fancy exactly burnt practice "
    "What if Suppose Imagine It might be a good idea Just an idea "
    "take a break be at this linking weakened H sound 仮定法 目的格 不定詞 "
    "意味上の主語 現在進行形 説明型オーバーラッピング"
)
EXPECTED = [
    "Dad's birthday is coming up next week.",
    "We should do something special.",
    "Yeah, but Mom already booked dinner, right?",
    "True, but what if we baked him a cake ourselves?",
    "Us? You know we're not exactly great in the kitchen.",
    "Just an idea, but he'd probably love something homemade more than anything fancy.",
    "Okay, but if it comes out burnt, you're explaining it.",
    "Let's start with a practice cake.",
    "What if we baked him a cake ourselves?",
    "Suppose we asked Emily for advice.",
    "Imagine we asked Emily for advice.",
    "It might be a good idea for us to discuss this together.",
    "Just an idea, but we could try leaving a little earlier tomorrow.",
    "It might be a good idea for us to take a break for a while.",
    "We've been at this for a long time.",
    "We're all a bit tired.",
    "In today's practice, we'll focus on linking as well as the weakened H sound in him.",
    "It might be a good idea for us to take a lunch break. I'm so hungry.",
]


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def serialize_segment(segment, offset: float = 0.0) -> dict[str, object]:
    words = []
    for word in getattr(segment, "words", None) or []:
        words.append(
            {
                "start": round(float(word.start) + offset, 3),
                "end": round(float(word.end) + offset, 3),
                "word": str(word.word),
                "probability": round(float(word.probability), 6),
            }
        )
    return {
        "start": round(float(segment.start) + offset, 3),
        "end": round(float(segment.end) + offset, 3),
        "text": str(segment.text).strip(),
        "avg_logprob": round(float(segment.avg_logprob), 6),
        "compression_ratio": round(float(segment.compression_ratio), 6),
        "no_speech_prob": round(float(segment.no_speech_prob), 6),
        "words": words,
    }


def run_pass(
    model,
    audio: Path,
    name: str,
    *,
    language,
    multilingual: bool,
    offset: float = 0.0,
    beam_size: int = 5,
):
    started = time.perf_counter()
    iterator, info = model.transcribe(
        str(audio),
        task="transcribe",
        language=language,
        multilingual=multilingual,
        beam_size=beam_size,
        best_of=beam_size,
        patience=1.0,
        temperature=(0.0, 0.2, 0.4),
        vad_filter=False,
        condition_on_previous_text=False,
        without_timestamps=False,
        word_timestamps=True,
        hallucination_silence_threshold=2.0,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        hotwords=HOTWORDS,
        log_progress=True,
    )
    records = [
        serialize_segment(segment, offset)
        for segment in iterator
        if str(segment.text).strip()
    ]
    elapsed = time.perf_counter() - started
    text = "\n".join(str(record["text"]) for record in records).strip() + "\n"
    (ROOT / f"{name}.txt").write_text(text, encoding="utf-8")
    (ROOT / f"{name}.segments.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "name": name,
        "elapsed_seconds": elapsed,
        "detected_language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "segment_count": len(records),
        "text_characters": len(text),
    }
    return text, records, summary


def main() -> None:
    summaries = []

    turbo = WhisperModel(
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
    )
    turbo_full_text, _, summary = run_pass(
        turbo,
        ROOT / "b.m4a",
        "turbo_full_no_vad_ja",
        language="ja",
        multilingual=True,
        beam_size=5,
    )
    summaries.append(summary)
    turbo_essential_text, _, summary = run_pass(
        turbo,
        ROOT / "essential.wav",
        "turbo_essential_forced_en",
        language="en",
        multilingual=False,
        offset=315.0,
        beam_size=5,
    )
    summaries.append(summary)
    del turbo
    gc.collect()

    large = WhisperModel(
        "Systran/faster-whisper-large-v3",
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
    )
    large_essential_text, _, summary = run_pass(
        large,
        ROOT / "essential.wav",
        "large_v3_essential_forced_en",
        language="en",
        multilingual=False,
        offset=315.0,
        beam_size=5,
    )
    summaries.append(summary)
    large_grammar_text, _, summary = run_pass(
        large,
        ROOT / "grammar.wav",
        "large_v3_grammar_mixed_ja",
        language="ja",
        multilingual=True,
        offset=185.0,
        beam_size=5,
    )
    summaries.append(summary)
    large_practical_text, _, summary = run_pass(
        large,
        ROOT / "practical.wav",
        "large_v3_practical_mixed_ja",
        language="ja",
        multilingual=True,
        offset=540.0,
        beam_size=5,
    )
    summaries.append(summary)
    del large
    gc.collect()

    combined = "\n".join(
        [
            turbo_full_text,
            turbo_essential_text,
            large_essential_text,
            large_grammar_text,
            large_practical_text,
        ]
    )
    normalized_combined = normalize(combined)
    candidate_lines = [line.strip() for line in combined.splitlines() if line.strip()]
    phrase_results = []
    for phrase in EXPECTED:
        target = normalize(phrase)
        exact = target in normalized_combined
        best_line = ""
        best_similarity = 0.0
        for line in candidate_lines:
            similarity = SequenceMatcher(None, target, normalize(line)).ratio()
            if similarity > best_similarity:
                best_similarity = similarity
                best_line = line
        phrase_results.append(
            {
                "expected": phrase,
                "exact_normalized_match": exact,
                "best_similarity": round(best_similarity, 6),
                "best_candidate": best_line,
            }
        )

    report = {
        "passes": summaries,
        "expected_phrase_results": phrase_results,
        "exact_phrase_count": sum(
            bool(item["exact_normalized_match"]) for item in phrase_results
        ),
        "expected_phrase_count": len(phrase_results),
        "notes": [
            "No educational formatting, deletion, or inferred sentence insertion was applied.",
            "The full pass uses no VAD so uncertain speech is still emitted rather than intentionally skipped.",
            "The English audit is an independent forced-English decode of the Essential Expressions interval.",
        ],
    }
    (ROOT / "independent_analysis_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
