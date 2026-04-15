from __future__ import annotations

from ...review_patterns import _STYLE_BY_BLOCK_TYPE
from .shared import ValidationContext, matches_whole_paragraph


def rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    suggestion = (comment.suggested_fix or "").strip().upper()
    if not matches_whole_paragraph(comment, ctx.chunks):
        return "descartado por regra de verificação"
    allowed = _STYLE_BY_BLOCK_TYPE.get(ctx.block_type)
    if allowed and suggestion and suggestion not in allowed:
        return "descartado por regra de verificação"
    if ctx.block_type == "paragraph" and suggestion in {"TITULO_1", "TÍTULO_1", "TITULO_2", "TÍTULO_2", "TITULO_3", "TÍTULO_3"}:
        return "descartado por regra de verificação"
    return None
