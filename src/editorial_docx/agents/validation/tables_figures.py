from __future__ import annotations

import re

from ...review_patterns import _folded_text, _normalized_text
from .shared import ValidationContext, has_neighbor_with_prefix, is_safe_text_normalization_auto_apply


def rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    block_type = ctx.block_type
    issue_excerpt = _normalized_text(comment.issue_excerpt)
    issue_excerpt_folded = _folded_text(comment.issue_excerpt)
    table_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
    table_blob_folded = _folded_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))

    if not (comment.issue_excerpt or "").strip():
        return "descartado por regra de verificação"
    if block_type == "caption" and re.match(r"^(tabela|figura|quadro|grafico)\s+\d+\s*$", issue_excerpt_folded):
        return "descartado por regra de verificação"
    if block_type == "caption" and comment.auto_apply:
        return "descartado por regra de verificação"
    if block_type == "caption" and re.match(r"^(tabela|figura|quadro|grafico)\s+\d+[:\s]", issue_excerpt_folded):
        if any(token in table_blob_folded for token in {"identificador", "titulo", "subtitulo"}):
            if not any(token in table_blob_folded for token in {"mesma linha", "fundidos", "linha da legenda", "linha propria"}):
                return "descartado por regra de verificação"
    if re.match(r"^(tabela|figura|quadro|grafico)\s+\d+", issue_excerpt_folded) and "fonte" in table_blob_folded:
        if not any(token in table_blob_folded for token in {"abaixo do bloco", "linha propria"}):
            return "descartado por regra de verificação"
    if "fonte" in ctx.folded_message and isinstance(comment.paragraph_index, int):
        if has_neighbor_with_prefix(comment.paragraph_index, ctx.refs, ctx.chunks, ("Fonte:", "Elaboração:", "Elaboracao:"), radius=2):
            return "descartado por regra de verificação"
    if re.match(r"^(tabela|figura|quadro|grafico)\s+\d+[:\s]", issue_excerpt_folded) and any(
        token in table_blob_folded for token in {"falta identificador", "falta o identificador", "nao possui um identificador"}
    ):
        return "descartado por regra de verificação"
    if block_type == "table_cell" and any(token in table_blob for token in {"subtitulo", "subtítulo", "fonte", "identificador", "legenda"}):
        return "descartado por regra de verificação"
    if block_type != "caption" and any(token in table_blob for token in {"subtitulo", "subtítulo", "fonte"}):
        return "descartado por regra de verificação"
    if block_type == "caption" and re.match(r"^(tabela|figura|quadro|gr[aá]fico)\s+\d+[:\s]", issue_excerpt):
        if any(token in table_blob for token in {"identificador", "titulo", "título", "subtitulo", "subtítulo"}):
            if not any(token in table_blob for token in {"mesma linha", "fundidos", "linha da legenda", "linha propria", "linha própria"}):
                return "descartado por regra de verificação"
    if re.match(r"^(tabela|figura|quadro)\s+\d+", issue_excerpt):
        source_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        if "fonte" in source_blob and not ("abaixo do bloco" in source_blob or "linha propria" in source_blob or "linha própria" in source_blob):
            return "descartado por regra de verificação"
    if "fonte" in _normalized_text(comment.message) and isinstance(comment.paragraph_index, int):
        if has_neighbor_with_prefix(comment.paragraph_index, ctx.refs, ctx.chunks, ("Fonte:", "Elaboração:"), radius=2):
            return "descartado por regra de verificação"
    if re.match(r"^(tabela|figura|quadro|gr[aá]fico)\s+\d+[:\s]", issue_excerpt) and any(
        token in table_blob for token in {"falta o identificador", "nao possui um identificador", "não possui um identificador"}
    ):
        return "descartado por regra de verificação"
    if comment.auto_apply and not is_safe_text_normalization_auto_apply(comment, ctx.chunks):
        return "descartado por regra de verificação"
    return None
