from __future__ import annotations

import re
from dataclasses import dataclass

from ...comment_localizer import locate_comment_in_document
from ...models import AgentComment
from ...review_patterns import _folded_text, _normalized_text, _ref_block_type


@dataclass(frozen=True)
class ValidationContext:
    comment: AgentComment
    agent: str
    chunks: list[str]
    refs: list[str]
    ref: str
    block_type: str
    folded_message: str
    folded_fix: str
    folded_blob: str
    source_text: str


def build_validation_context(comment: AgentComment, agent: str, chunks: list[str], refs: list[str]) -> ValidationContext:
    ref = ""
    if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(refs):
        ref = refs[comment.paragraph_index]
    block_type = _ref_block_type(ref)
    source_text = ""
    if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(chunks):
        source_text = chunks[comment.paragraph_index] or ""
    return ValidationContext(
        comment=comment,
        agent=agent,
        chunks=chunks,
        refs=refs,
        ref=ref,
        block_type=block_type,
        folded_message=_folded_text(comment.message),
        folded_fix=_folded_text(comment.suggested_fix),
        folded_blob=_folded_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""])),
        source_text=source_text,
    )


def has_neighbor_with_prefix(paragraph_index: int, refs: list[str], chunks: list[str], prefixes: tuple[str, ...], radius: int = 2) -> bool:
    for candidate in range(max(0, paragraph_index - radius), min(len(chunks), paragraph_index + radius + 1)):
        text = (chunks[candidate] or "").strip().casefold()
        if any(text.startswith(prefix.casefold()) for prefix in prefixes):
            return True
    return False


def find_excerpt_index(excerpt: str, candidate_indexes: list[int], chunks: list[str]) -> int | None:
    needle = _normalized_text(excerpt)
    if not needle:
        return None

    for idx in candidate_indexes:
        if not isinstance(idx, int):
            continue
        if 0 <= idx < len(chunks) and needle in _normalized_text(chunks[idx]):
            return idx

    window_chunks = [chunks[idx] for idx in candidate_indexes if isinstance(idx, int) and 0 <= idx < len(chunks)]
    localized = locate_comment_in_document(excerpt, window_chunks)
    if localized is not None and 0 <= localized < len(candidate_indexes):
        return candidate_indexes[localized]
    return None


def semantic_comment_key(item: AgentComment) -> tuple[str, int | None, str, str]:
    return (
        item.agent,
        item.paragraph_index if isinstance(item.paragraph_index, int) else None,
        _folded_text(item.issue_excerpt),
        _folded_text(item.suggested_fix),
    )


def remap_comment_index(comment: AgentComment, batch_indexes: list[int], chunks: list[str]) -> AgentComment:
    paragraph_index = comment.paragraph_index

    if paragraph_index is None:
        paragraph_index = find_excerpt_index(comment.issue_excerpt, batch_indexes, chunks)
        if paragraph_index is None and batch_indexes:
            paragraph_index = batch_indexes[0]
    elif paragraph_index not in batch_indexes and 0 <= paragraph_index < len(batch_indexes):
        paragraph_index = batch_indexes[paragraph_index]

    if paragraph_index is not None and batch_indexes and paragraph_index not in batch_indexes:
        matched = find_excerpt_index(comment.issue_excerpt, batch_indexes, chunks)
        if matched is not None:
            paragraph_index = matched

    matched = find_excerpt_index(comment.issue_excerpt, batch_indexes, chunks)
    if matched is not None:
        paragraph_index = matched

    return AgentComment(
        agent=comment.agent,
        category=comment.category,
        message=comment.message,
        paragraph_index=paragraph_index,
        issue_excerpt=comment.issue_excerpt,
        suggested_fix=comment.suggested_fix,
        auto_apply=comment.auto_apply,
        format_spec=comment.format_spec,
        review_status=comment.review_status,
        approved_text=comment.approved_text,
        reviewer_note=comment.reviewer_note,
    )


def limit_auto_apply(comment: AgentComment) -> AgentComment:
    if not comment.auto_apply:
        return comment
    return AgentComment(
        agent=comment.agent,
        category=comment.category,
        message=comment.message,
        paragraph_index=comment.paragraph_index,
        issue_excerpt=comment.issue_excerpt,
        suggested_fix=comment.suggested_fix,
        auto_apply=False,
        format_spec=comment.format_spec,
        review_status=comment.review_status,
        approved_text=comment.approved_text,
        reviewer_note=comment.reviewer_note,
    )


def tokenize_structure_text(value: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", (value or "").casefold())


def is_safe_structure_auto_apply(comment: AgentComment, chunks: list[str]) -> bool:
    if not isinstance(comment.paragraph_index, int) or not (0 <= comment.paragraph_index < len(chunks)):
        return False
    issue = (comment.issue_excerpt or "").strip()
    suggestion = (comment.suggested_fix or "").strip()
    source = (chunks[comment.paragraph_index] or "").strip()
    if not issue or not suggestion or not source:
        return False
    if _normalized_text(issue) != _normalized_text(source):
        return False
    return tokenize_structure_text(issue) == tokenize_structure_text(suggestion) == tokenize_structure_text(source)


def is_safe_text_normalization_auto_apply(comment: AgentComment, chunks: list[str]) -> bool:
    if not isinstance(comment.paragraph_index, int) or not (0 <= comment.paragraph_index < len(chunks)):
        return False
    issue = (comment.issue_excerpt or "").strip()
    suggestion = (comment.suggested_fix or "").strip()
    source = (chunks[comment.paragraph_index] or "").strip()
    if not issue or not suggestion or not source:
        return False
    if _normalized_text(issue) != _normalized_text(source):
        return False
    return tokenize_structure_text(issue) == tokenize_structure_text(suggestion) == tokenize_structure_text(source)


def matches_whole_paragraph(comment: AgentComment, chunks: list[str]) -> bool:
    if not isinstance(comment.paragraph_index, int) or not (0 <= comment.paragraph_index < len(chunks)):
        return False
    issue = (comment.issue_excerpt or "").strip()
    source = (chunks[comment.paragraph_index] or "").strip()
    if not issue or not source:
        return False
    return _normalized_text(issue) == _normalized_text(source)


def basic_comment_rejection_reason(comment: AgentComment) -> str | None:
    if not (comment.message or "").strip():
        return "mensagem vazia"

    if comment.issue_excerpt and comment.suggested_fix and not comment.auto_apply:
        if _normalized_text(comment.issue_excerpt) == _normalized_text(comment.suggested_fix):
            return "sugestão idêntica ao trecho"
    return None
