from __future__ import annotations

import re

from ...abnt_citation_parser import extract_citation_candidates
from ...abnt_matcher import ProbableReferenceMatch, compare_citations_to_references
from ...abnt_normalizer import (
    canonical_author_key as _abnt_canonical_author_key,
    canonical_reference_key as _abnt_canonical_reference_key,
    citation_label as _abnt_citation_label,
    is_plausible_reference_author as _abnt_is_plausible_reference_author,
    publication_year_from_reference as _abnt_publication_year_from_reference,
)
from ...abnt_reference_parser import parse_reference_entry
from ...abnt_validator import validate_reference_entry
from ...models import AgentComment
from ...review_patterns import _is_non_body_reference_context, _ref_block_type

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


def looks_like_reference_author(author_raw: str) -> bool:
    return _abnt_is_plausible_reference_author(author_raw, extra_blocked_tokens=NON_AUTHOR_REFERENCE_TOKENS)


def canonical_author_key(author_raw: str) -> str | None:
    return _abnt_canonical_author_key(author_raw, extra_blocked_tokens=NON_AUTHOR_REFERENCE_TOKENS)


def reference_citation_key(author_raw: str, year_raw: str) -> tuple[str, str] | None:
    return _abnt_canonical_reference_key(author_raw, year_raw, extra_blocked_tokens=NON_AUTHOR_REFERENCE_TOKENS)


def reference_citation_label(author_raw: str, year_raw: str) -> str:
    return _abnt_citation_label(author_raw, year_raw)


def reference_entry_publication_year(text: str) -> str | None:
    return _abnt_publication_year_from_reference(text)


def reference_body_citation_mentions(
    chunks: list[str],
    refs: list[str],
    body_limit: int,
) -> list[tuple[int, str, tuple[str, str], str]]:
    candidates = extract_citation_candidates(
        chunks,
        refs,
        body_limit,
        is_non_body_context=_is_non_body_reference_context,
        blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS,
    )
    return [(item.paragraph_index, item.excerpt, item.key, item.label) for item in candidates]


def reference_body_citation_keys(chunks: list[str], refs: list[str], body_limit: int) -> set[tuple[str, str]]:
    return {key for _, _, key, _ in reference_body_citation_mentions(chunks, refs, body_limit)}


def reference_entry_key(text: str) -> tuple[str, str] | None:
    parsed = parse_reference_entry(text, blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS)
    return parsed.key if parsed is not None else None


def reference_entry_label(text: str) -> str:
    parsed = parse_reference_entry(text, blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS)
    return parsed.label if parsed is not None else (text or "").strip()[:80]


def summarize_reference_labels(labels: list[str], max_items: int = 6) -> str:
    cleaned = [label.strip() for label in labels if label.strip()]
    if not cleaned:
        return ""
    if len(cleaned) <= max_items:
        return "; ".join(cleaned)
    shown = "; ".join(cleaned[:max_items])
    return f"{shown}; e mais {len(cleaned) - max_items}"


def probable_reference_match_comment(match: ProbableReferenceMatch) -> AgentComment:
    citation = match.citation
    reference = match.reference
    if match.match_type == "format_problem":
        return AgentComment(
            agent="referencias",
            category="citation_match",
            paragraph_index=citation.paragraph_index,
            message="A citação foi localizada na lista final, mas a entrada correspondente está malformada ou concatenada com outra referência.",
            issue_excerpt=citation.excerpt,
            suggested_fix=(
                f"Revisar a referência de {citation.label}: a autoria coincide com `{reference.label}`, "
                "mas a entrada precisa ser separada ou reformatada antes da conferência final."
            ),
        )

    return AgentComment(
        agent="referencias",
        category="citation_match",
        paragraph_index=citation.paragraph_index,
        message="A citação provavelmente corresponde a uma referência já existente, mas há divergência entre os dados autor-data do corpo e da lista final.",
        issue_excerpt=citation.excerpt,
        suggested_fix=(
            f"Conferir {citation.label} no corpo do texto com `{reference.label}` na lista final: "
            "a autoria coincide, mas o ano ou a forma de registro não bate."
        ),
    )


def reference_body_format_comments(
    chunks: list[str],
    refs: list[str],
    body_limit: int,
    *,
    batch_indexes: list[int],
) -> list[AgentComment]:
    comments: list[AgentComment] = []
    batch_set = set(batch_indexes)
    for idx, text in enumerate(chunks[:body_limit]):
        if idx not in batch_set or idx >= len(refs):
            continue
        if _is_non_body_reference_context(refs[idx], text, index=idx, chunks=chunks, refs=refs):
            continue
        for match in re.finditer(r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ'â€™`\-]+)\((\d{4}[a-z]?)\)", text or ""):
            comments.append(
                AgentComment(
                    agent="referencias",
                    category="citation_format",
                    message="Falta um espaço antes do ano em citação autor-data.",
                    paragraph_index=idx,
                    issue_excerpt=match.group(0),
                    suggested_fix=f"{match.group(1)} ({match.group(2)})",
                )
            )
    return comments


def find_reference_citation_indexes(chunks: list[str], refs: list[str], body_limit: int) -> list[int]:
    return sorted({idx for idx, _, _, _ in reference_body_citation_mentions(chunks, refs, body_limit)})


def heuristic_reference_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    seen: set[tuple[int, str, str]] = set()

    def add(idx: int, issue: str, fix: str, message: str, category: str = "reference_format") -> None:
        key = (idx, issue, fix)
        if key in seen:
            return
        seen.add(key)
        comments.append(
            AgentComment(
                agent="referencias",
                category=category,
                message=message,
                paragraph_index=idx,
                issue_excerpt=issue,
                suggested_fix=fix,
            )
        )

    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        block_type = _ref_block_type(refs[idx])
        text = chunks[idx] or ""
        parsed_entry = parse_reference_entry(text, blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS) if block_type == "reference_entry" else None

        if block_type == "paragraph":
            for match in re.finditer(r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ'â€™`\-]+)\((\d{4}[a-z]?)\)", text):
                add(idx, match.group(0), f"{match.group(1)} ({match.group(2)})", "Falta um espaço antes do ano em citação autor-data.", "citation_format")
        if block_type == "reference_entry" and re.search(r"\bIn:\S", text, flags=re.IGNORECASE):
            add(idx, "In:", "In: ", "Inserir espaço após 'In:' na referência.", "reference_format")
        if parsed_entry is not None and parsed_entry.document_type == "online" and "Acesso em:" not in text and "Acesso em :" not in text:
            add(
                idx,
                "Disponível em:",
                "Inserir `Acesso em:` com a data de consulta após a URL.",
                "A referência online informa a URL, mas não traz `Acesso em:` ao final.",
                "reference_format",
            )
        if block_type == "reference_entry" and re.search(r"([A-ZÀ-Ý][^.]*)\b(\d{4})\.(\D+[A-ZÀ-Ý])", text):
            match = re.search(r"(\d{4}\.[A-ZÀ-Ý])", text)
            if match:
                add(idx, match.group(1), match.group(1).replace(".", ". "), "Há duas referências coladas sem espaço entre elas.", "reference_format")
        if block_type == "reference_entry" and re.search(r"\bp\.(\d+[-â€“]\d+)", text, flags=re.IGNORECASE):
            match = re.search(r"(p\.\d+[-â€“]\d+)", text, flags=re.IGNORECASE)
            if match:
                add(idx, match.group(1), match.group(1).replace("p.", "p. "), "Falta espaço após a abreviatura de página.", "reference_format")
        if block_type == "reference_entry" and re.search(r"[A-Za-z0-9)\]]\s*$", text):
            add(idx, text, text.rstrip() + ".", "A referência termina sem ponto final.", "reference_format")
        duplicated_place = re.search(r"([A-ZÀ-Ý][A-Za-zÀ-ÿ\s]+):\s*\1,\s*\d{4}", text)
        if block_type == "reference_entry" and duplicated_place:
            add(idx, duplicated_place.group(0), duplicated_place.group(0), "Há duplicação de local e editora no trecho final da referência.", "reference_format")
        if parsed_entry is not None:
            for issue in validate_reference_entry(parsed_entry):
                add(idx, parsed_entry.raw_text, issue.suggested_fix, issue.message, issue.category)
        if block_type == "reference_entry":
            year_matches = re.findall(r"\b(?:19|20)\d{2}\b", text)
            if len(year_matches) >= 2 and year_matches[0] != year_matches[-1]:
                leading_match = re.search(r"\b(?:19|20)\d{2}\b", text)
                trailing_match = re.search(rf"\b{re.escape(year_matches[-1])}\b(?!.*\b(?:19|20)\d{{2}}\b)", text)
                if leading_match and trailing_match and leading_match.start() <= 60:
                    add(
                        idx,
                        trailing_match.group(0),
                        year_matches[0],
                        "O ano final da referência diverge do ano informado na abertura do registro.",
                        "reference_format",
                    )

    return comments


def heuristic_reference_global_comments(chunks: list[str], refs: list[str], batch_indexes: list[int]) -> list[AgentComment]:
    reference_heading_idx = next((idx for idx, ref in enumerate(refs) if _ref_block_type(ref) == "reference_heading"), None)
    if reference_heading_idx is None:
        return []

    body_limit = reference_heading_idx
    citation_candidates = extract_citation_candidates(
        chunks,
        refs,
        body_limit,
        is_non_body_context=_is_non_body_reference_context,
        blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS,
    )
    comments = reference_body_format_comments(chunks, refs, body_limit, batch_indexes=batch_indexes)
    batch_set = set(batch_indexes)

    reference_entries = [
        (idx, parsed)
        for idx, (chunk, ref) in enumerate(zip(chunks[reference_heading_idx + 1 :], refs[reference_heading_idx + 1 :]), start=reference_heading_idx + 1)
        if _ref_block_type(ref) == "reference_entry"
        for parsed in [parse_reference_entry(chunk, blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS)]
        if parsed is not None
    ]

    match_result = compare_citations_to_references(citation_candidates, [parsed for _, parsed in reference_entries])
    uncited_labels = [parsed.label for _, parsed in reference_entries if parsed in match_result.uncited_references]
    missing_labels = [candidate.label for candidate in match_result.missing_citations]

    for probable_match in match_result.probable_matches:
        if probable_match.citation.paragraph_index not in batch_set:
            continue
        comments.append(probable_reference_match_comment(probable_match))

    for candidate in match_result.missing_citations:
        if candidate.paragraph_index not in batch_set:
            continue
        comments.append(
            AgentComment(
                agent="referencias",
                category="citation_match",
                paragraph_index=candidate.paragraph_index,
                message="Esta citação no corpo do texto não tem correspondência clara na lista de referências.",
                issue_excerpt=candidate.excerpt,
                suggested_fix=f"Incluir ou revisar a referência correspondente a {candidate.label} na lista final.",
            )
        )

    if uncited_labels and reference_heading_idx in batch_set:
        comments.append(
            AgentComment(
                agent="referencias",
                category="inconsistency",
                paragraph_index=reference_heading_idx,
                message="Há referências na lista que não foram localizadas nas citações do corpo do texto.",
                issue_excerpt=chunks[reference_heading_idx],
                suggested_fix=f"Verificar estas obras: {summarize_reference_labels(uncited_labels)}.",
            )
        )

    if missing_labels and reference_heading_idx in batch_set:
        comments.append(
            AgentComment(
                agent="referencias",
                category="inconsistency",
                paragraph_index=reference_heading_idx,
                message="Há citações no corpo do texto sem correspondência clara na lista de referências.",
                issue_excerpt=chunks[reference_heading_idx],
                suggested_fix=f"Incluir ou revisar as referências correspondentes a: {summarize_reference_labels(missing_labels)}.",
            )
        )

    return comments


__all__ = [
    "NON_AUTHOR_REFERENCE_TOKENS",
    "canonical_author_key",
    "find_reference_citation_indexes",
    "heuristic_reference_comments",
    "heuristic_reference_global_comments",
    "looks_like_reference_author",
    "reference_body_citation_keys",
    "reference_body_citation_mentions",
    "reference_body_format_comments",
    "reference_citation_key",
    "reference_citation_label",
    "reference_entry_key",
    "reference_entry_label",
    "reference_entry_publication_year",
]
