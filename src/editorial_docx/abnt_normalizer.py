from __future__ import annotations

import re

from .review_patterns import _ascii_fold

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
    author = strip_leading_citation_context(author_raw)
    author = re.split(r"\s+(?:et\s+al\.?|e|and|&)\b", author, maxsplit=1)[0].strip()
    if not author:
        author = (author_raw or "").strip()
    return f"{author} ({(year_raw or '').strip()})".strip()


def publication_year_from_reference(text: str) -> str | None:
    source = (text or "").strip()
    if not source:
        return None

    bibliographic_body = re.split(r"\b(?:Dispon[iÃ­]vel em|Acesso em)\s*:", source, maxsplit=1, flags=re.IGNORECASE)[0]
    first_entry = re.split(r"\.\s*(?=[A-Z][A-Z'`\-]+,\s)", bibliographic_body, maxsplit=1)[0]
    year_matches = re.findall(r"\b(?:19|20)\d{2}[a-z]?\b", first_entry, flags=re.IGNORECASE)
    if not year_matches:
        return None
    return year_matches[-1].casefold()


__all__ = [
    "AUTHOR_PARTICLES",
    "LEADING_CITATION_CONTEXT_TOKENS",
    "NON_AUTHOR_REFERENCE_TOKENS",
    "canonical_author_key",
    "canonical_reference_key",
    "citation_label",
    "is_plausible_reference_author",
    "publication_year_from_reference",
    "strip_leading_citation_context",
]
