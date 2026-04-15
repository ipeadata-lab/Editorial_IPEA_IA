from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..models import AgentComment
from ..review_patterns import _normalized_text


def _semantic_text(text: str) -> str:
    return re.sub(r"\s+", " ", _normalized_text(text).casefold()).strip()


def _semantic_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right or left in right or right in left:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _mergeable_comment_key(comment: AgentComment) -> tuple[int | None, str]:
    paragraph_index = comment.paragraph_index if isinstance(comment.paragraph_index, int) else None
    excerpt = _semantic_text(comment.issue_excerpt)
    if excerpt:
        return paragraph_index, excerpt
    return paragraph_index, _semantic_text(comment.suggested_fix)


def consolidate_semantic_comments(comments: list[AgentComment]) -> list[AgentComment]:
    """Funde comentários quase equivalentes emitidos por agentes diferentes."""
    if not comments:
        return []

    merged: list[AgentComment] = []
    for comment in comments:
        current_key = _mergeable_comment_key(comment)
        current_msg = _semantic_text(comment.message)
        current_fix = _semantic_text(comment.suggested_fix)
        replaced = False
        for idx, existing in enumerate(merged):
            existing_key = _mergeable_comment_key(existing)
            if current_key[0] != existing_key[0]:
                continue
            excerpt_similarity = _semantic_similarity(current_key[1], existing_key[1])
            msg_similarity = _semantic_similarity(current_msg, _semantic_text(existing.message))
            fix_similarity = _semantic_similarity(current_fix, _semantic_text(existing.suggested_fix))
            if excerpt_similarity >= 0.9 and max(msg_similarity, fix_similarity) >= 0.72:
                existing_density = len(existing.message or "") + len(existing.suggested_fix or "")
                current_density = len(comment.message or "") + len(comment.suggested_fix or "")
                if current_density > existing_density:
                    merged[idx] = comment
                replaced = True
                break
        if not replaced:
            merged.append(comment)
    return merged


__all__ = ["consolidate_semantic_comments"]
