from __future__ import annotations

from ...review_patterns import _is_illustration_caption, _normalized_text
from .shared import ValidationContext, is_safe_structure_auto_apply, matches_whole_paragraph


def rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    folded_message = ctx.folded_message
    folded_blob = ctx.folded_blob
    block_type = ctx.block_type
    chunks = ctx.chunks

    if "paragrafo" in folded_message:
        return "descartado por regra de verificação"
    if block_type not in {"heading", "caption"} and any(token in folded_message for token in {"nao esta numerada", "deveria ser numerada", "numerar a secao"}):
        return "descartado por regra de verificação"
    issue_text = comment.issue_excerpt or ctx.source_text
    if block_type == "caption" and (_is_illustration_caption(issue_text) or _is_illustration_caption(ctx.source_text)):
        if any(token in folded_blob for token in {"secao", "subsecao", "numerar a secao", "numerar"}):
            return "descartado por regra de verificação"
    if block_type != "heading" and comment.issue_excerpt and not matches_whole_paragraph(comment, chunks):
        if any(token in folded_blob for token in {"titulo", "secao", "subsecao", "numerada", "numerar"}):
            return "descartado por regra de verificação"

    if block_type in {"direct_quote", "reference_entry", "table_cell"}:
        return "descartado por regra de verificação"
    if block_type == "caption":
        if _is_illustration_caption(issue_text) or _is_illustration_caption(ctx.source_text):
            structure_msg = ctx.folded_message
            structure_fix = ctx.folded_fix
            if any(token in structure_msg for token in {"seção", "secao", "subseção", "subsecao", "numerar a seção", "numerar a secao"}):
                return "descartado por regra de verificação"
            if any(token in structure_fix for token in {"seção", "secao"}):
                return "descartado por regra de verificação"
    if block_type != "heading":
        structure_msg = _normalized_text(comment.message)
        if any(token in structure_msg for token in {"não está numerada", "deveria ser numerada", "numerar a seção"}):
            return "descartado por regra de verificação"
    if block_type == "caption":
        structure_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        if _is_illustration_caption(comment.issue_excerpt or "") and any(
            token in structure_blob for token in {"secao", "seção", "subsecao", "subseção"}
        ):
            return "descartado por regra de verificação"
    if block_type == "heading" and comment.issue_excerpt and not matches_whole_paragraph(comment, chunks):
        return "descartado por regra de verificação"
    if block_type != "heading":
        structure_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        title_tokens = {"titulo", "título", "secao", "seção", "subsecao", "subseção", "numerada", "numerar"}
        if comment.issue_excerpt and not matches_whole_paragraph(comment, chunks):
            if any(token in structure_blob for token in title_tokens):
                return "descartado por regra de verificação"
    if comment.auto_apply and not is_safe_structure_auto_apply(comment, chunks):
        return "descartado por regra de verificação"
    return None
