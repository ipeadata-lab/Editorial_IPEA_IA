from __future__ import annotations

from ...models import AgentComment
from ...review_patterns import _is_illustration_caption, _ref_block_type, _ref_has_flag


def heuristic_typography_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    for idx in batch_indexes:
        if not (0 <= idx < len(refs)) or idx >= len(chunks):
            continue
        block_type = _ref_block_type(refs[idx])
        text = chunks[idx] or ""
        if block_type == "heading" and _ref_has_flag(refs[idx], "italico"):
            comments.append(
                AgentComment(
                    agent="tipografia",
                    category="heading",
                    message="Remover o itálico deste subtítulo e manter o negrito.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix="Remover itálico do título.",
                    auto_apply=True,
                    format_spec="italic=false",
                )
            )
        if block_type == "caption" and not _is_illustration_caption(text):
            comments.append(
                AgentComment(
                    agent="tipografia",
                    category="caption",
                    message="A legenda deve começar pelo identificador visual do elemento.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix=text,
                    auto_apply=False,
                )
            )
    return comments


__all__ = ["heuristic_typography_comments"]
