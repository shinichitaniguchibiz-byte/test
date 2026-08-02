from pathlib import Path
import re

path = Path('TRANSCRIBE_B.py')
source = path.read_text(encoding='utf-8')
pattern = re.compile(
    r'def correct_repeated_english_variants\(.*?\n(?=def apply_corrections_to_segments)',
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
        if best_index is not None and best_ratio >= 0.74:
            clusters[best_index].append(candidate)
        else:
            clusters.append([candidate])

    corrected = text
    normalized_text = normalize_for_comparison(text)
    corrections: list[dict[str, object]] = []
    for cluster in clusters:
        representatives: dict[str, str] = {}
        for candidate in cluster:
            normalized = normalize_for_comparison(candidate)
            current = representatives.get(normalized)
            if current is None or len(candidate) > len(current):
                representatives[normalized] = candidate
        if len(representatives) < 2:
            continue
        occurrence_counts = {
            normalized: normalized_text.count(normalized)
            for normalized in representatives
        }
        if sum(occurrence_counts.values()) < 4:
            continue
        canonical_normalized = max(
            representatives,
            key=lambda normalized: (
                occurrence_counts[normalized],
                len(english_words(representatives[normalized])),
                len(representatives[normalized]),
            ),
        )
        canonical = representatives[canonical_normalized]
        canonical_count = occurrence_counts[canonical_normalized]
        canonical_words = english_words(canonical)
        if canonical_count < 2 or len(canonical_words) < 6:
            continue
        for variant_normalized, variant in representatives.items():
            if variant_normalized == canonical_normalized:
                continue
            variant_count = occurrence_counts[variant_normalized]
            variant_words = english_words(variant)
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
            if similarity < 0.74 or canonical_count <= variant_count:
                continue
            word_pattern = r"\\b" + r"[\\s,;:!?'.-]*".join(
                re.escape(word) for word in variant_words
            ) + r"\\b[.!?]?"
            corrected, occurrences = re.subn(
                word_pattern,
                canonical,
                corrected,
                flags=re.IGNORECASE,
            )
            if occurrences:
                corrections.append(
                    {
                        "from": variant,
                        "to": canonical,
                        "occurrences": occurrences,
                        "canonical_count": canonical_count,
                        "variant_count": variant_count,
                        "similarity": round(similarity, 6),
                    }
                )
    return corrected, corrections


'''
source, count = pattern.subn(replacement, source, count=1)
if count != 1:
    raise RuntimeError('final repeated correction replacement failed')
path.write_text(source, encoding='utf-8')
