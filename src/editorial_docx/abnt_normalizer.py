from __future__ import annotations

import re

from .review_patterns import _ascii_fold

_REFERENCE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}(?:[/-]\d{2,4})?[a-z]?\b", flags=re.IGNORECASE)
_GLUED_ENTRY_SPLIT_RE = re.compile(r"\.\s*(?=[A-Z][A-Z'`\-]+,\s+[A-ZÀ-Ý])")

NON_AUTHOR_REFERENCE_TOKENS = {
    "periodo",
    "ano",
    "anos",
    "mes",
    "meses",
    "pagina",
    "paginas",
    "secao",
    "secoes",
    "capitulo",
    "capitulos",
    "figura",
    "grafico",
    "quadro",
    "tabela",
    "nota",
    "anexo",
    "apendice",
    "parte",
    "texto",
    "documento",
    "disponivel",
    "versao",
    "edicao",
    "serie",
    "volume",
    "numero",
    "lei",
    "decreto",
    "constituicao",
    "formulario",
    "relacao",
    "tercos",
    "salarial",
    "estabelecimento",
    "sexo",
    "pis",
    "fgts",
}

AUTHOR_PARTICLES = {"de", "da", "do", "das", "dos", "del", "della", "di"}
LEADING_CITATION_CONTEXT_TOKENS = {"segundo", "conforme", "cf", "veja", "ver"}


def strip_leading_citation_context(text: str) -> str:
    return re.sub(r"^\s*(?:Segundo|Conforme|Cf\.?|Veja|Ver)\s+", "", (text or "").strip(), flags=re.IGNORECASE)


def split_author_fragments(author_raw: str) -> tuple[str, ...]:
    author = strip_leading_citation_context(author_raw).strip().strip(".,;: ")
    if not author:
        return ()

    cleaned = re.sub(r"\bet\s+al\.?\b", "", author, flags=re.IGNORECASE).strip().strip(".,;: ")
    if not cleaned:
        return ()

    fragments = [piece.strip(" .,;:") for piece in re.split(r"\s*;\s*|\s+(?:e|and|&)\s+", cleaned) if piece.strip(" .,;:")]
    return tuple(fragments)


def author_short_labels(author_raw: str) -> tuple[str, ...]:
    labels: list[str] = []
    for fragment in split_author_fragments(author_raw):
        label = fragment.split(",", 1)[0].strip().strip(".,;: ")
        if label:
            labels.append(label)
    return tuple(labels)


def canonical_author_keys(author_raw: str, extra_blocked_tokens: set[str] | None = None) -> tuple[str, ...]:
    keys: list[str] = []
    for fragment in split_author_fragments(author_raw):
        key = canonical_author_key(fragment, extra_blocked_tokens=extra_blocked_tokens)
        if key is not None:
            keys.append(key)
    deduped = tuple(dict.fromkeys(keys))
    return deduped


def canonical_author_key(author_raw: str, extra_blocked_tokens: set[str] | None = None) -> str | None:
    author = _ascii_fold(strip_leading_citation_context(author_raw)).casefold()
    if not author:
        return None

    author = re.sub(r"\bet\s+al\.?\b", "", author, flags=re.IGNORECASE)
    primary = re.split(r"\s+(?:e|and|&)\s+", author, maxsplit=1)[0].strip()
    tokens = re.findall(r"[a-z0-9]+", primary)
    if not tokens:
        return None

    idx = 0
    while idx < len(tokens) and tokens[idx] in LEADING_CITATION_CONTEXT_TOKENS:
        idx += 1
    while idx < len(tokens) and tokens[idx] in AUTHOR_PARTICLES:
        idx += 1
    if idx >= len(tokens):
        return None

    blocked = set(NON_AUTHOR_REFERENCE_TOKENS)
    if extra_blocked_tokens:
        blocked.update(extra_blocked_tokens)
    token = tokens[idx]
    if token in blocked:
        return None
    return token


def is_plausible_reference_author(author_raw: str, extra_blocked_tokens: set[str] | None = None) -> bool:
    author = (author_raw or "").strip()
    if not author:
        return False
    first_alpha = next((char for char in author if char.isalpha()), "")
    if not first_alpha or not first_alpha.isupper():
        return False
    return canonical_author_key(author_raw, extra_blocked_tokens=extra_blocked_tokens) is not None


def canonical_reference_key(
    author_raw: str,
    year_raw: str,
    extra_blocked_tokens: set[str] | None = None,
) -> tuple[str, str] | None:
    year = (year_raw or "").strip().casefold()
    if not year:
        return None
    author = canonical_author_key(author_raw, extra_blocked_tokens=extra_blocked_tokens)
    if author is None:
        return None
    return author, year


def citation_label(author_raw: str, year_raw: str) -> str:
    labels = author_short_labels(author_raw)
    if not labels:
        author = (author_raw or "").strip()
    elif len(labels) == 1:
        author = labels[0]
    elif len(labels) == 2:
        author = f"{labels[0]} e {labels[1]}"
    else:
        author = f"{labels[0]} et al."
    return f"{author} ({(year_raw or '').strip()})".strip()


def publication_year_from_reference(text: str) -> str | None:
    source = (text or "").strip()
    if not source:
        return None

    bibliographic_body = re.split(r"\b(?:Dispon[iÃ­]vel em|Acesso em)\s*:", source, maxsplit=1, flags=re.IGNORECASE)[0]
    first_entry = _GLUED_ENTRY_SPLIT_RE.split(bibliographic_body, maxsplit=1)[0]
    year_matches = _REFERENCE_YEAR_RE.findall(first_entry)
    if not year_matches:
        return None
    return year_matches[-1].casefold()


__all__ = [
    "AUTHOR_PARTICLES",
    "LEADING_CITATION_CONTEXT_TOKENS",
    "NON_AUTHOR_REFERENCE_TOKENS",
    "canonical_author_key",
    "canonical_author_keys",
    "canonical_reference_key",
    "author_short_labels",
    "citation_label",
    "is_plausible_reference_author",
    "publication_year_from_reference",
    "_GLUED_ENTRY_SPLIT_RE",
    "_REFERENCE_YEAR_RE",
    "split_author_fragments",
    "strip_leading_citation_context",
]
