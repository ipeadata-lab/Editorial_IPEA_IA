from __future__ import annotations

from dataclasses import dataclass
import re

from .abnt_normalizer import (
    canonical_author_keys,
    canonical_reference_key,
    citation_label,
    is_plausible_reference_author,
    strip_leading_citation_context,
)


@dataclass(frozen=True)
class CitationCandidate:
    paragraph_index: int
    excerpt: str
    author_raw: str
    year: str
    author_keys: tuple[str, ...]
    key: tuple[str, str]
    label: str


_NARRATIVE_PATTERN = re.compile(
    r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ'’`\-]+(?:\s+(?:de|da|do|das|dos)\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’`\-]+|\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’`\-]+|\s+(?:e|and|&)\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’`\-]+|\s+et\s+al\.?)*)\s*\((\d{4}[a-z]?)\)"
)
_PARENTHETICAL_PATTERN = re.compile(r"\(([^)]*\d{4}[a-z]?[^)]*)\)")
_PARENTHETICAL_SEGMENT_PATTERN = re.compile(
    r"([A-ZÀ-Ý][^()]*)\s*,\s*(\d{4}[a-z]?)(?:\s*,\s*p\.\s*\d+(?:[-–]\d+)?)?$"
)


def extract_citation_candidates(
    chunks: list[str],
    refs: list[str],
    body_limit: int,
    *,
    is_non_body_context,
    blocked_author_tokens: set[str] | None = None,
) -> list[CitationCandidate]:
    candidates: list[CitationCandidate] = []
    seen: set[tuple[int, str, tuple[str, str], str]] = set()

    def add_candidate(idx: int, excerpt: str, author_raw: str, year_raw: str) -> None:
        key = canonical_reference_key(author_raw, year_raw, extra_blocked_tokens=blocked_author_tokens)
        if key is None:
            return
        author_keys = canonical_author_keys(author_raw, extra_blocked_tokens=blocked_author_tokens)
        if not author_keys:
            return
        clean_excerpt = strip_leading_citation_context(excerpt)
        label = citation_label(author_raw, year_raw)
        identity = (idx, clean_excerpt, key, label)
        if identity in seen:
            return
        seen.add(identity)
        candidates.append(
            CitationCandidate(
                paragraph_index=idx,
                excerpt=clean_excerpt,
                author_raw=author_raw.strip(),
                year=(year_raw or "").strip(),
                author_keys=author_keys,
                key=key,
                label=label,
            )
        )

    for idx, (chunk, ref) in enumerate(zip(chunks[:body_limit], refs[:body_limit])):
        if is_non_body_context(ref, chunk, index=idx, chunks=chunks, refs=refs):
            continue
        text = chunk or ""

        for match in _NARRATIVE_PATTERN.finditer(text):
            author_raw = match.group(1)
            if is_plausible_reference_author(author_raw, extra_blocked_tokens=blocked_author_tokens):
                add_candidate(idx, match.group(0), author_raw, match.group(2))

        for parenthetical_match in _PARENTHETICAL_PATTERN.finditer(text):
            for segment in re.split(r";", parenthetical_match.group(1)):
                piece = segment.strip()
                match = _PARENTHETICAL_SEGMENT_PATTERN.search(piece)
                if match and is_plausible_reference_author(match.group(1), extra_blocked_tokens=blocked_author_tokens):
                    add_candidate(idx, piece, match.group(1), match.group(2))

    return candidates


__all__ = ["CitationCandidate", "extract_citation_candidates"]
