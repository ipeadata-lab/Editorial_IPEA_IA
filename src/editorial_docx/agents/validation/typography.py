from __future__ import annotations

from ...review_patterns import _ALLOWED_TYPOGRAPHY_KEYS, _is_relevant_typography_spec, _normalized_text, _parse_format_spec
from .shared import ValidationContext, matches_whole_paragraph


def rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    spec = _parse_format_spec(comment.format_spec)
    if not spec:
        return "descartado por regra de verificação"
    if any(key not in _ALLOWED_TYPOGRAPHY_KEYS for key in spec):
        return "descartado por regra de verificação"
    if not _is_relevant_typography_spec(spec):
        return "descartado por regra de verificação"
    if comment.issue_excerpt and not matches_whole_paragraph(comment, ctx.chunks):
        return "descartado por regra de verificação"
    if ctx.block_type == "paragraph" and isinstance(comment.paragraph_index, int) and comment.paragraph_index >= 24:
        return "descartado por regra de verificação"
    if ctx.block_type in {"reference_entry", "reference_heading"}:
        return "descartado por regra de verificação"
    if ctx.block_type not in {"heading", "caption", "paragraph"}:
        return "descartado por regra de verificação"
    fix = (comment.suggested_fix or "").casefold()
    if "alterar para '" in fix or 'alterar para "' in fix:
        return "descartado por regra de verificação"
    if any(token in _normalized_text(comment.suggested_fix) for token in {"reescrever", "substituir texto", "alterar conteúdo"}):
        return "descartado por regra de verificação"
    return None
