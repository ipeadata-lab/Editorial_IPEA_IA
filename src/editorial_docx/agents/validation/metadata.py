from __future__ import annotations

from ...review_patterns import _normalized_text
from .shared import ValidationContext


def rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    if ctx.block_type not in {"heading", "paragraph"}:
        return "descartado por regra de verificação"
    if isinstance(comment.paragraph_index, int) and comment.paragraph_index >= 18:
        return "descartado por regra de verificação"
    metadata_excerpt = _normalized_text(comment.issue_excerpt)
    metadata_message = _normalized_text(comment.message)
    if any(term in metadata_excerpt for term in {"não fornecido", "nao fornecido"}) and isinstance(comment.paragraph_index, int) and comment.paragraph_index > 12:
        return "descartado por regra de verificação"
    if "placeholder" in metadata_message and "xxxxx" not in metadata_excerpt and "<td" not in metadata_excerpt:
        return "descartado por regra de verificação"
    return None
