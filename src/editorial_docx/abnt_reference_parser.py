from __future__ import annotations

from dataclasses import dataclass
import re

from .abnt_document_types import (
    ABNT_TYPE_ARTICLE,
    ABNT_TYPE_BOOK,
    ABNT_TYPE_CHAPTER,
    ABNT_TYPE_GENERIC,
    ABNT_TYPE_INSTITUTIONAL_REPORT,
    ABNT_TYPE_LEGAL,
    ABNT_TYPE_ONLINE,
    ABNT_TYPE_THESIS,
)
from .abnt_normalizer import _REFERENCE_YEAR_RE, canonical_author_key, canonical_author_keys, citation_label, publication_year_from_reference
from .review_patterns import _ascii_fold

_GLUED_REFERENCE_RE = re.compile(r"\.\s*(?=[A-Z][A-Z'`\-]+,\s+[A-ZÀ-Ý])")
_AUTHOR_SEGMENT_RE = re.compile(r"^(?P<author>.+?\.)\s+(?P<body>(?=[A-ZÀ-Ý][a-zà-ÿ]).+)$")


@dataclass(frozen=True)
class ParsedReferenceEntry:
    raw_text: str
    author_raw: str
    author_key: str
    author_keys: tuple[str, ...]
    publication_year: str
    label: str
    year_candidates: tuple[str, ...]
    document_type: str
    title: str
    container_title: str
    place: str
    publisher: str
    institution: str
    has_url: bool
    has_access_date: bool
    has_doi: bool
    has_in: bool
    has_volume: bool
    has_number: bool
    has_pages: bool
    has_glued_reference: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.author_key, self.publication_year


def _primary_reference_segment(text: str) -> str:
    source = (text or "").strip()
    if not source:
        return ""
    segments = _GLUED_REFERENCE_RE.split(source, maxsplit=1)
    return segments[0].strip() if segments else source


def _split_author_and_body(source: str) -> tuple[str, str]:
    match = _AUTHOR_SEGMENT_RE.match(source)
    if match:
        return match.group("author").strip(), match.group("body").strip()
    return source, ""


def _infer_reference_document_type(text: str) -> str:
    source = _ascii_fold(text).casefold()
    if any(marker in source for marker in ("lei ", "decreto ", "portaria ", "resolucao ")):
        return ABNT_TYPE_LEGAL
    if any(marker in source for marker in ("tese", "dissertacao")):
        return ABNT_TYPE_THESIS
    if " in: " in source:
        return ABNT_TYPE_CHAPTER
    if "disponivel em:" in source:
        return ABNT_TYPE_ONLINE
    if "doi" in source or re.search(r"\bv\.\s*\d+", source) or re.search(r"\bn\.\s*\d+", source):
        return ABNT_TYPE_ARTICLE
    if any(marker in source for marker in ("texto para discussao", "relatorio", "ipea")):
        return ABNT_TYPE_INSTITUTIONAL_REPORT
    if re.search(r":[^:.,;]{1,80},\s*(?:19|20)\d{2}", text):
        return ABNT_TYPE_BOOK
    return ABNT_TYPE_GENERIC


def _extract_reference_title(body: str) -> str:
    tail = (body or "").lstrip(" ,.;")
    if not tail:
        return ""
    segments = [segment.strip(" .") for segment in re.split(r"\.\s+", tail) if segment.strip()]
    return segments[0] if segments else ""


def _extract_container_title(source: str, title: str) -> str:
    if " In: " in source or " in: " in source:
        match = re.search(r"\bIn:\s*([^.,]+)", source, flags=re.IGNORECASE)
        return match.group(1).strip() if match else ""
    after_title = source.split(title, 1)[-1].lstrip(" .") if title and title in source else source
    match = re.match(r"([^.,]+)", after_title)
    return match.group(1).strip() if match else ""


def _extract_place_and_publisher(source: str) -> tuple[str, str]:
    match = re.search(r"([A-Z][^:.,;]{1,80}):\s*([^,.;]+)", source)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _extract_institution(source: str, author_raw: str, publisher: str) -> str:
    if author_raw.isupper():
        return author_raw.strip()
    folded = _ascii_fold(source).casefold()
    match = re.search(r"\b(universidade|instituto|instituicao|ministerio|secretaria|ipea)\b", folded)
    if match:
        return match.group(0).strip()
    return publisher


def parse_reference_entry(text: str, *, blocked_author_tokens: set[str] | None = None) -> ParsedReferenceEntry | None:
    source = (text or "").strip()
    if not source:
        return None

    primary_entry = _primary_reference_segment(source)
    publication_year = publication_year_from_reference(primary_entry)
    if not publication_year:
        return None

    author_raw, body = _split_author_and_body(primary_entry)
    author_key = canonical_author_key(author_raw, extra_blocked_tokens=blocked_author_tokens)
    if author_key is None:
        return None
    author_keys = canonical_author_keys(author_raw, extra_blocked_tokens=blocked_author_tokens) or (author_key,)

    document_type = _infer_reference_document_type(primary_entry)
    title = _extract_reference_title(body or primary_entry)
    container_title = _extract_container_title(primary_entry, title)
    place, publisher = _extract_place_and_publisher(primary_entry)
    has_url = bool(re.search(r"https?://\S+", primary_entry, flags=re.IGNORECASE))
    has_access_date = bool(re.search(r"\bAcesso em\s*:", primary_entry, flags=re.IGNORECASE))
    has_doi = bool(re.search(r"\bdoi\b", primary_entry, flags=re.IGNORECASE))
    has_in = bool(re.search(r"\bIn:\s*", primary_entry, flags=re.IGNORECASE))
    has_volume = bool(re.search(r"\bv\.\s*\d+", primary_entry, flags=re.IGNORECASE))
    has_number = bool(re.search(r"\bn\.\s*\d+", primary_entry, flags=re.IGNORECASE))
    has_pages = bool(re.search(r"\bp{1,2}\.\s*\d+", primary_entry, flags=re.IGNORECASE))
    institution = _extract_institution(primary_entry, author_raw, publisher)
    year_candidates = tuple(_REFERENCE_YEAR_RE.findall(primary_entry))

    return ParsedReferenceEntry(
        raw_text=source,
        author_raw=author_raw,
        author_key=author_key,
        author_keys=author_keys,
        publication_year=publication_year,
        label=citation_label(author_raw, publication_year),
        year_candidates=year_candidates,
        document_type=document_type,
        title=title,
        container_title=container_title,
        place=place,
        publisher=publisher,
        institution=institution,
        has_url=has_url,
        has_access_date=has_access_date,
        has_doi=has_doi,
        has_in=has_in,
        has_volume=has_volume,
        has_number=has_number,
        has_pages=has_pages,
        has_glued_reference=bool(_GLUED_REFERENCE_RE.search(source)),
    )


__all__ = ["ParsedReferenceEntry", "parse_reference_entry"]
