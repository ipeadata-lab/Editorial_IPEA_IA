from __future__ import annotations

from dataclasses import dataclass

from .abnt_citation_parser import CitationCandidate
from .abnt_reference_parser import ParsedReferenceEntry


@dataclass(frozen=True)
class ProbableReferenceMatch:
    citation: CitationCandidate
    reference: ParsedReferenceEntry
    match_type: str
    confidence: float


@dataclass(frozen=True)
class ExactReferenceMatch:
    citation: CitationCandidate
    reference: ParsedReferenceEntry


@dataclass(frozen=True)
class ReferenceMatchResult:
    exact_matches: tuple[ExactReferenceMatch, ...]
    probable_matches: tuple[ProbableReferenceMatch, ...]
    missing_citations: tuple[CitationCandidate, ...]
    uncited_references: tuple[ParsedReferenceEntry, ...]


def _build_probable_match(
    citation: CitationCandidate,
    reference: ParsedReferenceEntry,
    *,
    match_type: str | None = None,
) -> ProbableReferenceMatch:
    resolved_type = match_type
    if resolved_type is None:
        if reference.has_glued_reference:
            resolved_type = "format_problem"
        else:
            resolved_type = "year_mismatch"

    confidence_by_type = {
        "format_problem": 0.94,
        "year_mismatch": 0.88,
        "partial_author_conflict": 0.79,
    }
    return ProbableReferenceMatch(
        citation=citation,
        reference=reference,
        match_type=resolved_type,
        confidence=confidence_by_type.get(resolved_type, 0.75),
    )


def _is_exact_author_match(citation: CitationCandidate, reference: ParsedReferenceEntry) -> bool:
    if not citation.author_keys or not reference.author_keys:
        return False
    if len(citation.author_keys) == 1:
        return citation.author_keys[0] == reference.author_keys[0]
    return citation.author_keys == reference.author_keys[: len(citation.author_keys)]


def _author_overlap_score(citation: CitationCandidate, reference: ParsedReferenceEntry) -> tuple[int, int]:
    citation_set = set(citation.author_keys)
    reference_set = set(reference.author_keys)
    overlap = len(citation_set & reference_set)
    return overlap, len(citation_set | reference_set)


def compare_citations_to_references(
    citations: list[CitationCandidate],
    references: list[ParsedReferenceEntry],
) -> ReferenceMatchResult:
    exact_matches: list[ExactReferenceMatch] = []
    probable_matches: list[ProbableReferenceMatch] = []
    missing_citations: list[CitationCandidate] = []
    matched_reference_ids: set[int] = set()

    references_by_primary_author: dict[str, list[ParsedReferenceEntry]] = {}
    for entry in references:
        references_by_primary_author.setdefault(entry.author_key, []).append(entry)

    for citation in citations:
        exact_candidates = [
            entry
            for entry in references
            if _is_exact_author_match(citation, entry) and entry.publication_year == citation.year.casefold()
        ]
        if exact_candidates:
            matched_reference_ids.update(id(entry) for entry in exact_candidates)
            exact_matches.extend(ExactReferenceMatch(citation=citation, reference=entry) for entry in exact_candidates)
            glued_exact = next((entry for entry in exact_candidates if entry.has_glued_reference), None)
            if glued_exact is not None:
                probable_matches.append(_build_probable_match(citation, glued_exact, match_type="format_problem"))
            continue

        same_author_candidates = [entry for entry in references if _is_exact_author_match(citation, entry)]
        if len(same_author_candidates) == 1:
            matched_reference_ids.add(id(same_author_candidates[0]))
            probable_matches.append(_build_probable_match(citation, same_author_candidates[0]))
            continue

        if len(same_author_candidates) > 1:
            year_hint_candidates = [entry for entry in same_author_candidates if citation.year.casefold() in entry.year_candidates]
            if len(year_hint_candidates) == 1:
                matched_reference_ids.add(id(year_hint_candidates[0]))
                probable_matches.append(_build_probable_match(citation, year_hint_candidates[0]))
                continue

        overlap_candidates: list[tuple[int, int, ParsedReferenceEntry]] = []
        for entry in references:
            overlap, union = _author_overlap_score(citation, entry)
            if overlap > 0 and not _is_exact_author_match(citation, entry):
                overlap_candidates.append((overlap, union, entry))

        if overlap_candidates:
            overlap_candidates.sort(key=lambda item: (item[0], -item[1], citation.year.casefold() in item[2].year_candidates), reverse=True)
            best_overlap, _, best_entry = overlap_candidates[0]
            if best_overlap > 0:
                matched_reference_ids.add(id(best_entry))
                probable_matches.append(_build_probable_match(citation, best_entry, match_type="partial_author_conflict"))
                continue

        primary_candidates = references_by_primary_author.get(citation.key[0], [])
        if len(primary_candidates) == 1:
            matched_reference_ids.add(id(primary_candidates[0]))
            probable_matches.append(_build_probable_match(citation, primary_candidates[0]))
            continue

        if len(primary_candidates) > 1:
            year_hint_candidates = [entry for entry in primary_candidates if citation.year.casefold() in entry.year_candidates]
            if len(year_hint_candidates) == 1:
                matched_reference_ids.add(id(year_hint_candidates[0]))
                probable_matches.append(_build_probable_match(citation, year_hint_candidates[0]))
                continue

        missing_citations.append(citation)

    uncited_references = tuple(entry for entry in references if id(entry) not in matched_reference_ids)
    return ReferenceMatchResult(
        exact_matches=tuple(exact_matches),
        probable_matches=tuple(probable_matches),
        missing_citations=tuple(missing_citations),
        uncited_references=uncited_references,
    )


__all__ = ["ExactReferenceMatch", "ProbableReferenceMatch", "ReferenceMatchResult", "compare_citations_to_references"]
