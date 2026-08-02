from pathlib import Path
import re

path = Path('TRANSCRIBE_B.py')
source = path.read_text(encoding='utf-8')

if 'TRANSCRIPTION_VERSION = 25' not in source:
    source = source.replace('TRANSCRIPTION_VERSION = 24', 'TRANSCRIPTION_VERSION = 25', 1)
source = source.replace('radio-transcription-v24', 'radio-transcription-v25')
if 'V25_PATCH_APPLIED = True' not in source:
    source = source.replace(
        'V24_PATCH_APPLIED = True',
        'V24_PATCH_APPLIED = True\nV25_PATCH_APPLIED = True',
        1,
    )

source = source.replace(
    '    window_seconds = 45.0 if section_name == "Essential Expressions" else SECTION_WINDOW_SECONDS\n'
    '    hop_seconds = 15.0 if section_name == "Essential Expressions" else SECTION_WINDOW_HOP_SECONDS',
    '    window_seconds = SECTION_WINDOW_SECONDS\n'
    '    hop_seconds = SECTION_WINDOW_HOP_SECONDS',
    1,
)

if 'def is_complete_recovered_candidate' not in source:
    helper_marker = '    return candidates\n\n\ndef detect_decoder_loop'
    helper = '''    return candidates


def is_complete_recovered_candidate(text: str) -> bool:
    cleaned = clean_text(text)
    words = english_words(cleaned)
    if len(words) < 5:
        return False
    if words[-1].lower() in {
        "a", "an", "the", "to", "for", "of", "and", "or", "but", "us", "we", "our"
    }:
        return False
    lower = cleaned.lower().strip()
    if re.search(r"[.!?]$", lower):
        return True
    if lower.startswith(("suppose ", "imagine ", "what if ")):
        return len(words) >= 6
    if lower.startswith("just an idea"):
        return len(words) >= 10 and " could " in f" {lower} "
    if lower.startswith("it might be a good idea"):
        return len(words) >= 10 and " to " in f" {lower} "
    if lower.startswith("we could "):
        return len(words) >= 5
    return False


def detect_decoder_loop'''
    if helper_marker not in source:
        raise RuntimeError('candidate completeness insertion point missing')
    source = source.replace(helper_marker, helper, 1)

acceptance_pattern = re.compile(
    r'            source_supported = len\(sources\) >= MIN_EVENT_SOURCE_SUPPORT\n'
    r'            repeated_supported = \(\n.*?'
    r'            if not accepted and single_example_supported:\n'
    r'                accepted = True\n'
    r'                evidence_type = "high_confidence_single_example"\n',
    re.S,
)
new_acceptance = '''            source_supported = len(sources) >= MIN_EVENT_SOURCE_SUPPORT
            candidate_complete = is_complete_recovered_candidate(canonical)
            repeated_supported = (
                repeated_at_separate_times
                and event_avg_logprob >= MIN_REPEATED_EVENT_AVG_LOGPROB
                and candidate_complete
            )
            accepted = (
                source_supported
                and event_avg_logprob >= MIN_EVENT_AVG_LOGPROB
                and candidate_complete
            )
            evidence_type = "overlapping_window_consensus"
            if not accepted and repeated_supported:
                accepted = True
                evidence_type = "same_sentence_repeated_at_separate_times"
            normalized_prefix = canonical.strip().lower()
            single_example_supported = (
                section_name == "Essential Expressions"
                and len(sources) == 1
                and event_avg_logprob >= MIN_SINGLE_EXAMPLE_AVG_LOGPROB
                and normalized_prefix.startswith(SINGLE_EXAMPLE_PREFIXES)
                and candidate_complete
            )
            if not accepted and single_example_supported:
                accepted = True
                evidence_type = "high_confidence_single_example"
'''
if 'candidate_complete = is_complete_recovered_candidate(canonical)' not in source:
    source, count = acceptance_pattern.subn(new_acceptance, source, count=1)
    if count != 1:
        raise RuntimeError('v25 acceptance block replacement failed')

if 'reason = "incomplete_candidate"' not in source:
    source = source.replace(
        '            reason = "accepted"\n            if already_present:',
        '            reason = "accepted"\n'
        '            if not candidate_complete:\n'
        '                accepted = False\n'
        '                reason = "incomplete_candidate"\n'
        '            elif already_present:',
        1,
    )

function_pattern = re.compile(
    r'def correct_repeated_english_variants\(\n    text: str,\n\) -> tuple\[str, list\[dict\[str, object\]\]\]:\n.*?\n    return corrected, corrections\n',
    re.S,
)
replacement = '''def correct_repeated_english_variants(
    text: str,
) -> tuple[str, list[dict[str, object]]]:
    candidates = [
        candidate
        for candidate in split_english_candidates(text)
        if len(english_words(candidate)) >= 6
        and is_complete_recovered_candidate(candidate)
    ]
    clusters: list[list[str]] = []
    for candidate in candidates:
        normalized = normalize_for_comparison(candidate)
        best_index: int | None = None
        best_ratio = 0.0
        for index, cluster in enumerate(clusters):
            ratio = SequenceMatcher(
                None, normalized, normalize_for_comparison(cluster[0])
            ).ratio()
            if ratio > best_ratio:
                best_index = index
                best_ratio = ratio
        if best_index is not None and best_ratio >= 0.82:
            clusters[best_index].append(candidate)
        else:
            clusters.append([candidate])

    corrected = text
    corrections: list[dict[str, object]] = []
    for cluster in clusters:
        grouped: dict[str, list[str]] = {}
        for candidate in cluster:
            grouped.setdefault(normalize_for_comparison(candidate), []).append(candidate)
        if sum(len(values) for values in grouped.values()) < 4 or len(grouped) < 2:
            continue
        canonical_normalized, canonical_values = max(
            grouped.items(),
            key=lambda item: (
                len(item[1]),
                max(len(english_words(value)) for value in item[1]),
                max(len(value) for value in item[1]),
            ),
        )
        canonical = max(
            set(canonical_values),
            key=lambda value: (
                bool(value[:1].isupper()),
                bool(re.search(r"[.!?]$", value)),
                len(value),
            ),
        )
        canonical_count = len(canonical_values)
        canonical_words = english_words(canonical)
        if canonical_count < 2 or len(canonical_words) < 6:
            continue
        for variant_normalized, variant_values in grouped.items():
            if variant_normalized == canonical_normalized:
                continue
            variant_count = len(variant_values)
            representative = max(set(variant_values), key=len)
            variant_words = english_words(representative)
            if not variant_words:
                continue
            if canonical_words[0].lower() != variant_words[0].lower():
                continue
            if canonical_words[-1].lower() != variant_words[-1].lower():
                continue
            if abs(len(canonical_words) - len(variant_words)) > 3:
                continue
            similarity = SequenceMatcher(
                None, canonical_normalized, variant_normalized
            ).ratio()
            if similarity < 0.82 or canonical_count <= variant_count:
                continue
            for exact_variant in sorted(set(variant_values), key=len, reverse=True):
                occurrences = corrected.count(exact_variant)
                if occurrences == 0:
                    continue
                corrected = corrected.replace(exact_variant, canonical)
                corrections.append(
                    {
                        "from": exact_variant,
                        "to": canonical,
                        "occurrences": occurrences,
                        "canonical_count": canonical_count,
                        "variant_count": variant_count,
                        "similarity": round(similarity, 6),
                    }
                )
    return corrected, corrections
'''
source, count = function_pattern.subn(replacement, source, count=1)
if count != 1:
    raise RuntimeError('repeated English correction replacement failed')

source = source.replace(
    '("Suppose we asked Emily for advice", 465.0, 535.0, 1, "essential_practice"),',
    '("Suppose we asked Emily for advice", 385.0, 435.0, 1, "essential_example"),',
    1,
)
source = source.replace(
    '("Imagine we asked Emily for advice", 465.0, 535.0, 1, "essential_practice"),',
    '("Imagine we asked Emily for advice", 385.0, 435.0, 1, "essential_example"),',
    1,
)
source = source.replace(
    '        ("Just an idea, but we could try leaving a little earlier tomorrow", 440.0, 490.0, 1, "essential_first_example"),\n',
    '',
    1,
)
source = source.replace(
    'lesson76_time_local_repetition_regression_v24',
    'lesson76_time_local_repetition_regression_v25',
)
source = source.replace(
    'FAST_PRIMARY_PLUS_MIXED_LANGUAGE_COMPACT_CONSENSUS',
    'FAST_PRIMARY_PLUS_CONVERGED_SECTION_RECOVERY',
)
source = source.replace(
    'SECTION_RECOVERY_WINDOW=ESSENTIAL_45_15_OTHER_90_45',
    'SECTION_RECOVERY_WINDOW=90_SECONDS_HOP_45_SECONDS',
)
source = source.replace(
    'MIXED_LANGUAGE_COMPACT_OVERLAP_EVENT_CONSENSUS',
    'CONVERGED_COMPLETE_SENTENCE_EVENT_CONSENSUS',
)
source = source.replace(
    '"source_versions_combined": [14, 16, 18, 19, 20, 22, 23],',
    '"source_versions_combined": [14, 16, 18, 19, 20, 22, 23, 24],',
)
source = source.replace(
    'source=f"v24_{slug}_w{window_count:02d}"',
    'source=f"v25_{slug}_w{window_count:02d}"',
)
source = source.replace(
    'source="turbo_v24_time_local_section_consensus"',
    'source="turbo_v25_time_local_section_consensus"',
)

path.write_text(source, encoding='utf-8')

finalize_source = Path('V25_FINALIZE.py').read_text(encoding='utf-8')
exec(compile(finalize_source, 'V25_FINALIZE.py', 'exec'), {'__name__': '__main__'})
