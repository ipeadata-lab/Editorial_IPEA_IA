from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import AgentComment
from .review_patterns import _folded_text


def _normalize_for_match(text: str) -> str:
    normalized = _folded_text(text)
    normalized = normalized.replace("|", " ").replace("*", " ").replace("_", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _quote_coverage(quote: str, window: str) -> float:
    if not quote:
        return 0.0
    matcher = SequenceMatcher(None, quote, window, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(quote)


def locate_comment_in_document(
    quote: str,
    paragraphs: list[str],
    *,
    threshold: float = 0.35,
) -> int | None:
    """Localiza o índice mais provável para um trecho usando substring + similaridade."""
    if not quote or not paragraphs:
        return None

    quote_norm = _normalize_for_match(quote)[:1200]
    best_idx: int | None = None
    best_score = 0.0

    for idx, paragraph in enumerate(paragraphs):
        para_norm = _normalize_for_match(paragraph)
        if not para_norm:
            continue
        if quote_norm and quote_norm in para_norm:
            return idx

        if len(para_norm) <= len(quote_norm) + 220:
            windows = [para_norm]
        else:
            window_size = min(len(para_norm), max(len(quote_norm) + 220, 420))
            step = max(window_size // 2, 120)
            windows = [para_norm[start : start + window_size] for start in range(0, len(para_norm) - window_size + 1, step)]
            if windows and windows[-1] != para_norm[-window_size:]:
                windows.append(para_norm[-window_size:])

        score = max(_quote_coverage(quote_norm, window) for window in windows)
        if score > best_score:
            best_idx = idx
            best_score = score

    return best_idx if best_score >= threshold else None


def locate_comments_in_window(
    comments: list[AgentComment],
    candidate_indexes: list[int],
    chunks: list[str],
) -> list[AgentComment]:
    """Preenche índices ausentes tentando localizar cada comentário na janela candidata."""
    if not comments or not candidate_indexes:
        return comments[:]

    local_chunks = [chunks[idx] for idx in candidate_indexes if 0 <= idx < len(chunks)]
    remapped: list[AgentComment] = []
    for comment in comments:
        resolved_index = comment.paragraph_index
        if resolved_index is None or resolved_index not in candidate_indexes:
            located = locate_comment_in_document(comment.issue_excerpt, local_chunks)
            if located is not None and 0 <= located < len(candidate_indexes):
                resolved_index = candidate_indexes[located]
        remapped.append(
            AgentComment(
                agent=comment.agent,
                category=comment.category,
                message=comment.message,
                paragraph_index=resolved_index,
                issue_excerpt=comment.issue_excerpt,
                suggested_fix=comment.suggested_fix,
                auto_apply=comment.auto_apply,
                format_spec=comment.format_spec,
                review_status=comment.review_status,
                approved_text=comment.approved_text,
                reviewer_note=comment.reviewer_note,
            )
        )
    return remapped


__all__ = ["locate_comment_in_document", "locate_comments_in_window"]
