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
class ReferenceMatchResult:
    probable_matches: tuple[ProbableReferenceMatch, ...]
    missing_citations: tuple[CitationCandidate, ...]
    uncited_references: tuple[ParsedReferenceEntry, ...]


def _build_probable_match(
    citation: CitationCandidate,
    reference: ParsedReferenceEntry,
) -> ProbableReferenceMatch:
    if reference.has_glued_reference:
        return ProbableReferenceMatch(
            citation=citation,
            reference=reference,
            match_type="format_problem",
            confidence=0.94,
        )
    return ProbableReferenceMatch(
        citation=citation,
        reference=reference,
        match_type="year_mismatch",
        confidence=0.88,
    )


def compare_citations_to_references(
    citations: list[CitationCandidate],
    references: list[ParsedReferenceEntry],
) -> ReferenceMatchResult:
    exact_reference_keys = {entry.key for entry in references}
    probable_matches: list[ProbableReferenceMatch] = []
    missing_citations: list[CitationCandidate] = []
    matched_reference_ids: set[int] = set()

    references_by_author: dict[str, list[ParsedReferenceEntry]] = {}
    for entry in references:
        references_by_author.setdefault(entry.author_key, []).append(entry)

    for citation in citations:
        exact_candidates = [entry for entry in references_by_author.get(citation.key[0], []) if entry.key == citation.key]
        if exact_candidates:
            matched_reference_ids.update(id(entry) for entry in exact_candidates)
            glued_exact = next((entry for entry in exact_candidates if entry.has_glued_reference), None)
            if glued_exact is not None:
                probable_matches.append(
                    ProbableReferenceMatch(
                        citation=citation,
                        reference=glued_exact,
                        match_type="format_problem",
                        confidence=0.98,
                    )
                )
            continue

        author_candidates = references_by_author.get(citation.key[0], [])
        if len(author_candidates) == 1:
            matched_reference_ids.add(id(author_candidates[0]))
            probable_matches.append(_build_probable_match(citation, author_candidates[0]))
            continue

        if len(author_candidates) > 1:
            year_hint_candidates = [entry for entry in author_candidates if citation.year in entry.year_candidates]
            if len(year_hint_candidates) == 1:
                matched_reference_ids.add(id(year_hint_candidates[0]))
                probable_matches.append(_build_probable_match(citation, year_hint_candidates[0]))
                continue

        missing_citations.append(citation)

    uncited_references = tuple(entry for entry in references if id(entry) not in matched_reference_ids)
    return ReferenceMatchResult(
        probable_matches=tuple(probable_matches),
        missing_citations=tuple(missing_citations),
        uncited_references=uncited_references,
    )


__all__ = ["ProbableReferenceMatch", "ReferenceMatchResult", "compare_citations_to_references"]
