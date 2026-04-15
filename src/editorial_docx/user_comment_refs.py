from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import DocumentUserComment

_ACTION_TOKENS = ("procure", "procurar", "buscar", "busque", "localize", "localizar", "incluir", "adicione", "adicionar", "insira", "inserir")
_SUBJECT_TOKENS = ("refer", "fonte", "cita", "citation", "bibliogr")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(slots=True)
class ReferenceSearchRequest:
    comment_id: int
    paragraph_index: int
    comment_text: str
    anchor_excerpt: str
    paragraph_text: str
    query_text: str


@dataclass(slots=True)
class ReferenceCandidate:
    title: str
    authors: list[str]
    year: str
    container_title: str
    volume: str
    issue: str
    page: str
    publisher: str
    doi: str
    url: str
    entry_type: str
    score: float


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _normalized_text(value: str) -> str:
    folded = _strip_accents((value or "").casefold())
    return re.sub(r"\s+", " ", folded).strip()


def _significant_tokens(value: str) -> list[str]:
    stop = {
        "de", "da", "do", "das", "dos", "a", "o", "e", "em", "para", "por",
        "with", "from", "the", "and", "of", "on", "in", "um", "uma",
    }
    return [
        token
        for token in re.findall(r"[a-z0-9]+", _normalized_text(value))
        if len(token) >= 4 and token not in stop
    ]


def is_reference_search_request(comment_text: str) -> bool:
    folded = _normalized_text(comment_text)
    if not folded:
        return False
    return any(token in folded for token in _ACTION_TOKENS) and any(token in folded for token in _SUBJECT_TOKENS)


def _best_query_text(user_comment: DocumentUserComment) -> str:
    for source in (user_comment.text, user_comment.anchor_excerpt, user_comment.paragraph_text):
        match = re.search(r"[\"“](.+?)[\"”]", source or "")
        if match and len(match.group(1).strip()) >= 12:
            return match.group(1).strip()

    comment_core = re.sub(r"\s+", " ", (user_comment.text or "").strip())
    paragraph_core = re.sub(r"\s+", " ", (user_comment.anchor_excerpt or user_comment.paragraph_text or "").strip())
    query_parts = [part for part in [comment_core, paragraph_core] if part]
    query = " | ".join(query_parts)
    query = re.sub(r"\b(?:procure|buscar|busque|adicione|adicionar|incluir|insira|inserir)\b", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\b(?:refer[eê]ncia|fonte|cita[cç][aã]o)\b", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip(" |")
    return query[:280]


def build_reference_search_requests(user_comments: list[DocumentUserComment]) -> list[ReferenceSearchRequest]:
    requests: list[ReferenceSearchRequest] = []
    seen: set[tuple[int, str]] = set()
    for item in user_comments:
        if item.paragraph_index is None:
            continue
        if not is_reference_search_request(item.text):
            continue
        query_text = _best_query_text(item)
        if not query_text:
            continue
        key = (item.paragraph_index, _normalized_text(query_text))
        if key in seen:
            continue
        seen.add(key)
        requests.append(
            ReferenceSearchRequest(
                comment_id=item.comment_id,
                paragraph_index=item.paragraph_index,
                comment_text=item.text,
                anchor_excerpt=item.anchor_excerpt,
                paragraph_text=item.paragraph_text,
                query_text=query_text,
            )
        )
    return requests


def _http_get_json(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": "lang-ipea-editorial/0.2 (reference lookup)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _person_name(person: dict[str, Any]) -> str:
    family = str(person.get("family") or "").strip()
    given = str(person.get("given") or "").strip()
    if family and given:
        return f"{family}, {given}"
    return family or given


def _year_from_crossref(item: dict[str, Any]) -> str:
    for key in ("issued", "published-print", "published-online", "created"):
        block = item.get(key) or {}
        date_parts = block.get("date-parts") or []
        if date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            year = str(date_parts[0][0]).strip()
            if year:
                return year
    return ""


def _candidate_from_crossref(item: dict[str, Any]) -> ReferenceCandidate:
    title = " ".join(str(part).strip() for part in (item.get("title") or []) if str(part).strip()).strip()
    container_title = " ".join(str(part).strip() for part in (item.get("container-title") or []) if str(part).strip()).strip()
    authors = [_person_name(person) for person in (item.get("author") or []) if _person_name(person)]
    doi = str(item.get("DOI") or "").strip()
    url = str(item.get("URL") or "").strip()
    return ReferenceCandidate(
        title=title,
        authors=authors,
        year=_year_from_crossref(item),
        container_title=container_title,
        volume=str(item.get("volume") or "").strip(),
        issue=str(item.get("issue") or "").strip(),
        page=str(item.get("page") or "").strip(),
        publisher=str(item.get("publisher") or "").strip(),
        doi=doi,
        url=url,
        entry_type=str(item.get("type") or "").strip(),
        score=float(item.get("score") or 0.0),
    )


def search_reference_candidates(request: ReferenceSearchRequest, rows: int = 5) -> list[ReferenceCandidate]:
    query = (request.query_text or "").strip()
    if not query:
        return []

    doi_match = _DOI_RE.search(query) or _DOI_RE.search(request.comment_text) or _DOI_RE.search(request.paragraph_text)
    try:
        if doi_match:
            doi = doi_match.group(0).strip()
            payload = _http_get_json(f"https://api.crossref.org/works/{quote(doi, safe='')}")
            message = payload.get("message") or {}
            return [_candidate_from_crossref(message)] if message else []

        payload = _http_get_json(
            "https://api.crossref.org/works"
            f"?rows={max(1, rows)}&query.bibliographic={quote(query)}"
        )
    except Exception:
        return []

    message = payload.get("message") or {}
    items = message.get("items") or []
    candidates = [_candidate_from_crossref(item) for item in items]
    return [candidate for candidate in candidates if candidate.title]


def format_reference_candidate(candidate: ReferenceCandidate) -> str:
    authors = "; ".join(candidate.authors) if candidate.authors else ""
    title = candidate.title.strip()
    year = candidate.year.strip()
    container_title = candidate.container_title.strip()
    volume = candidate.volume.strip()
    issue = candidate.issue.strip()
    page = candidate.page.strip()
    publisher = candidate.publisher.strip()
    doi = candidate.doi.strip()
    url = candidate.url.strip()

    parts: list[str] = []
    if authors:
        parts.append(f"{authors}.")
    if title:
        parts.append(f"{title}.")
    if container_title:
        container_bits = [container_title]
        if volume:
            container_bits.append(f"v. {volume}")
        if issue:
            container_bits.append(f"n. {issue}")
        if page:
            container_bits.append(f"p. {page}")
        if year:
            container_bits.append(year)
        parts.append(", ".join(container_bits) + ".")
    else:
        trailing_bits = [bit for bit in [publisher, year] if bit]
        if trailing_bits:
            parts.append(", ".join(trailing_bits) + ".")
    if doi:
        parts.append(f"DOI: {doi}.")
    elif url:
        parts.append(f"Disponível em: {url}.")
    return " ".join(part.strip() for part in parts if part.strip()).strip()


def reference_already_present(reference_text: str, existing_reference_entries: list[str]) -> bool:
    candidate_norm = _normalized_text(reference_text)
    if not candidate_norm:
        return False

    doi_match = _DOI_RE.search(reference_text)
    candidate_doi = _normalized_text(doi_match.group(0)) if doi_match else ""
    title_tokens = _significant_tokens(reference_text)
    title_probe = title_tokens[:8]

    for entry in existing_reference_entries:
        entry_norm = _normalized_text(entry)
        if not entry_norm:
            continue
        if candidate_doi and candidate_doi in entry_norm:
            return True
        if candidate_norm == entry_norm:
            return True
        if title_probe:
            overlap = sum(1 for token in title_probe if token in entry_norm)
            if overlap >= min(5, len(title_probe)):
                return True
    return False


def candidates_as_json(candidates: list[ReferenceCandidate]) -> str:
    payload = [
        {
            "title": item.title,
            "authors": item.authors,
            "year": item.year,
            "container_title": item.container_title,
            "volume": item.volume,
            "issue": item.issue,
            "page": item.page,
            "publisher": item.publisher,
            "doi": item.doi,
            "url": item.url,
            "type": item.entry_type,
            "score": item.score,
            "formatted_reference": format_reference_candidate(item),
        }
        for item in candidates
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
