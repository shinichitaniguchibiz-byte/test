from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def patch_source(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    if "TRANSCRIPTION_VERSION = 24" not in source:
        source = source.replace("TRANSCRIPTION_VERSION = 23", "TRANSCRIPTION_VERSION = 24", 1)
    source = source.replace("radio-transcription-v23", "radio-transcription-v24")
    if "V24_PATCH_APPLIED = True" not in source:
        source = source.replace(
            "V23_PATCH_APPLIED = True",
            "V23_PATCH_APPLIED = True\nV24_PATCH_APPLIED = True",
            1,
        )

    start = source.index("def split_english_candidates(text: str) -> list[str]:")
    end = source.index("\n\ndef detect_decoder_loop", start)
    replacement = r'''def split_english_candidates(text: str) -> list[str]:
    normalized_text = clean_text(text.replace("’", "'"))
    english_runs = re.findall(
        r"[A-Za-z][A-Za-z0-9'.,!?;:()\- ]{2,}",
        normalized_text,
    )
    sentence_starters = (
        r"Suppose|Imagine|What if|It might|Just an idea|Dad's|Yeah|True|Us|"
        r"You know|Okay|Hmm|Let's|We've|We're|We should|In today's|Today's|"
        r"You ready|One more time|Excellent work|Great work"
    )
    candidates: list[str] = []
    seen: set[str] = set()
    for english_run in english_runs:
        english_run = clean_text(english_run).strip(" ,;:")
        if not english_run:
            continue
        english_run = re.sub(r"\b(Suppose|Imagine),\s*", r"\1 ", english_run)
        english_run = re.sub(
            rf",\s+(?=(?:{sentence_starters})\b)",
            ".\n",
            english_run,
        )
        for piece in re.split(r"(?<=[.!?])\s+|[\r\n]+", english_run):
            piece = clean_text(piece).strip(" ,;:")
            words = english_words(piece)
            compact = re.sub(r"\s+", "", piece)
            if not compact or not 4 <= len(words) <= 40:
                continue
            if latin_character_count(piece) / len(compact) < 0.82:
                continue
            if len({word.lower() for word in words}) / len(words) < 0.42:
                continue
            key = normalize_for_comparison(piece)
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(piece)
    return candidates
'''
    source = source[:start] + replacement + source[end:]

    start = source.index(
        "def candidate_observations(segments: list[SegmentRecord]) -> list[dict[str, object]]:"
    )
    end = source.index("\n\ndef local_baseline_candidates", start)
    replacement = r'''def candidate_observations(segments: list[SegmentRecord]) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for segment in segments:
        candidates = split_english_candidates(segment.text)
        if not candidates:
            continue
        duration = max(segment.end - segment.start, 0.001)
        candidate_count = len(candidates)
        for candidate_index, candidate in enumerate(candidates):
            if segment.avg_logprob < -0.72 or detect_decoder_loop(candidate):
                continue
            candidate_start = segment.start + duration * candidate_index / candidate_count
            candidate_end = segment.start + duration * (candidate_index + 1) / candidate_count
            observations.append(
                {
                    "text": candidate,
                    "normalized": normalize_for_comparison(candidate),
                    "start": candidate_start,
                    "end": candidate_end,
                    "midpoint": (candidate_start + candidate_end) / 2.0,
                    "avg_logprob": segment.avg_logprob,
                    "source": segment.source,
                }
            )
    return observations
'''
    source = source[:start] + replacement + source[end:]

    replacements = {
        'source=f"v23_{slug}_w{window_count:02d}"': 'source=f"v24_{slug}_w{window_count:02d}"',
        'source="turbo_v23_time_local_section_consensus"': 'source="turbo_v24_time_local_section_consensus"',
        "lesson76_time_local_repetition_regression_v23": "lesson76_time_local_repetition_regression_v24",
        "FAST_PRIMARY_PLUS_COMPACT_SECTION_CONSENSUS": "FAST_PRIMARY_PLUS_MIXED_LANGUAGE_COMPACT_CONSENSUS",
        "SECTION_RECOVERY_CONSENSUS=COMPACT_OVERLAP_EVENT_CONSENSUS": "SECTION_RECOVERY_CONSENSUS=MIXED_LANGUAGE_COMPACT_OVERLAP_EVENT_CONSENSUS",
        '"source_versions_combined": [14, 16, 18, 19, 20, 22],': '"source_versions_combined": [14, 16, 18, 19, 20, 22, 23],',
    }
    for old, new in replacements.items():
        source = source.replace(old, new)

    marker = '            print("SECTION_RECOVERY_CONSENSUS=MIXED_LANGUAGE_COMPACT_OVERLAP_EVENT_CONSENSUS")\n'
    if marker in source and "MIXED_LANGUAGE_ENGLISH_EXTRACTION=ENABLED" not in source:
        source = source.replace(
            marker,
            marker + '            print("MIXED_LANGUAGE_ENGLISH_EXTRACTION=ENABLED")\n',
            1,
        )

    config_marker = '                    "section_window_hop_seconds": SECTION_WINDOW_HOP_SECONDS,\n'
    if config_marker in source and '"mixed_language_english_extraction": True' not in source:
        source = source.replace(
            config_marker,
            config_marker + '                    "mixed_language_english_extraction": True,\n',
            1,
        )

    path.write_text(source, encoding="utf-8")


def validate_source(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    required = [
        "TRANSCRIPTION_VERSION = 24",
        "V24_PATCH_APPLIED = True",
        "MIXED_LANGUAGE_ENGLISH_EXTRACTION=ENABLED",
        "turbo_v24_time_local_section_consensus",
        "lesson76_time_local_repetition_regression_v24",
        "mixed_language_english_extraction",
        "candidate_start = segment.start + duration * candidate_index / candidate_count",
    ]
    missing = [item for item in required if item not in source]
    if missing:
        raise RuntimeError(f"missing v24 markers: {missing}")

    spec = importlib.util.spec_from_file_location("transcribe_v24", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load patched module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    mixed = (
        "Suppose, we asked Emily for advice, Imagine, we asked Emily for advice, "
        "エミリーにアドバイスを求めるとしたらどうかな?"
    )
    extracted = module.split_english_candidates(mixed)
    normalized = [module.normalize_for_comparison(item) for item in extracted]
    assert module.normalize_for_comparison("Suppose we asked Emily for advice") in normalized, extracted
    assert module.normalize_for_comparison("Imagine we asked Emily for advice") in normalized, extracted

    mixed_just = (
        "ちょっと思いつきなんだけど Just an idea, but we could try leaving "
        "a little earlier tomorrow. 控えめな提案ですね"
    )
    extracted_just = module.split_english_candidates(mixed_just)
    assert any(
        "leavingalittleearliertomorrow" in module.normalize_for_comparison(item)
        for item in extracted_just
    ), extracted_just

    segment = module.SegmentRecord(
        start=400.0,
        end=412.0,
        text=mixed,
        avg_logprob=-0.18,
        compression_ratio=1.0,
        no_speech_prob=0.0,
        source="test_window",
    )
    observations = module.candidate_observations([segment])
    assert len(observations) == 2, observations
    assert observations[0]["start"] < observations[1]["start"], observations
    assert observations[0]["end"] <= observations[1]["start"], observations
    assert module.clean_text("Sentence....") == "Sentence."
    assert module.SECTION_WINDOW_SECONDS == 90.0
    assert module.SECTION_WINDOW_HOP_SECONDS == 45.0


if __name__ == "__main__":
    source_path = Path("TRANSCRIBE_B.py")
    patch_source(source_path)
    validate_source(source_path)
    print("V24_VALIDATION=PASS")
