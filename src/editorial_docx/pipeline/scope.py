from __future__ import annotations

from ..agents.scopes import scope_indexes_for_agent
from ..config import DEFAULT_REVIEW_MAX_BATCH_CHARS, DEFAULT_REVIEW_MAX_BATCH_CHUNKS
from ..document_loader import Section
from ..models import AgentComment, DocumentUserComment, agent_short_label
from ..prompts import AGENT_ORDER
from ..review_patterns import _normalized_text, _ref_block_type
from .consolidation import consolidate_semantic_comments
from .context import PreparedReviewDocument, prepare_review_document as _prepare_review_document

_USER_REFERENCE_AGENT = "comentarios_usuario_referencias"


def _build_batches(
    chunks: list[str],
    refs: list[str],
    indexes: list[int],
    max_chars: int = DEFAULT_REVIEW_MAX_BATCH_CHARS,
    max_chunks: int = DEFAULT_REVIEW_MAX_BATCH_CHUNKS,
) -> list[list[int]]:
    if not chunks or not indexes:
        return []

    batches: list[list[int]] = []
    current: list[int] = []
    current_chars = 0

    for idx in indexes:
        if idx < 0 or idx >= len(chunks):
            continue
        chunk = chunks[idx]
        ref = refs[idx] if idx < len(refs) else "sem referência"
        line = f"[{idx}] ({ref}) {chunk}"
        line_len = len(line) + 1

        if current and (len(current) >= max_chunks or current_chars + line_len > max_chars):
            batches.append(current)
            current = []
            current_chars = 0

        current.append(idx)
        current_chars += line_len

    if current:
        batches.append(current)

    return batches


def _agent_scope_indexes(agent: str, chunks: list[str], refs: list[str], sections: list[Section]) -> list[int]:
    """Seleciona os índices mais relevantes do documento para cada agente."""
    total = len(chunks)
    if total == 0:
        return []
    if agent == _USER_REFERENCE_AGENT:
        return []
    return scope_indexes_for_agent(agent=agent, chunks=chunks, refs=refs, sections=sections, total=total)


def prepare_review_batches(
    paragraphs: list[str],
    refs: list[str],
    sections: list[Section],
    selected_agents: list[str] | None = None,
    user_comments: list[DocumentUserComment] | None = None,
) -> PreparedReviewDocument:
    """Gera o documento preparado com os lotes que cada agente deve revisar."""
    agent_order = [agent for agent in (selected_agents or AGENT_ORDER) if agent in AGENT_ORDER]
    return _prepare_review_document(
        chunks=paragraphs,
        refs=refs,
        sections=sections,
        user_comments=user_comments or [],
        agent_order=agent_order,
        agent_scope_builder=_agent_scope_indexes,
    )


def _comment_priority(comment: AgentComment, refs: list[str]) -> tuple[int, int, int]:
    block_type = ""
    if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(refs):
        block_type = _ref_block_type(refs[comment.paragraph_index])
    specificity = 0
    if comment.category == "citation_match":
        specificity += 3
    if block_type == "reference_entry":
        specificity += 2
    if block_type == "reference_heading":
        specificity -= 2
    density = len((comment.issue_excerpt or "").strip()) + len((comment.suggested_fix or "").strip())
    return specificity, density, len((comment.message or "").strip())


def _comment_sort_key(comment: AgentComment) -> tuple[int, str, str]:
    paragraph_index = comment.paragraph_index if isinstance(comment.paragraph_index, int) else 10**9
    return paragraph_index, agent_short_label(comment.agent), _normalized_text(comment.message)


def _consolidate_final_comments(comments: list[AgentComment], refs: list[str]) -> list[AgentComment]:
    """Deduplica e prioriza os comentários antes da saída final do review."""
    if not comments:
        return []

    best_by_key: dict[tuple[str, str, int | None, str, str], AgentComment] = {}
    for comment in comments:
        key = (
            comment.agent,
            comment.category,
            comment.paragraph_index if isinstance(comment.paragraph_index, int) else None,
            _normalized_text(comment.issue_excerpt),
            _normalized_text(comment.suggested_fix),
        )
        existing = best_by_key.get(key)
        if existing is None or _comment_priority(comment, refs) > _comment_priority(existing, refs):
            best_by_key[key] = comment

    deduped = list(best_by_key.values())
    deduped = consolidate_semantic_comments(deduped)
    has_reference_body_matches = any(item.agent == "referencias" and item.category == "citation_match" for item in deduped)
    filtered: list[AgentComment] = []
    suppressed_heading_messages = {_normalized_text("Há citações no corpo do texto sem correspondência clara na lista de referências.")}

    for comment in deduped:
        if (
            has_reference_body_matches
            and comment.agent == "referencias"
            and isinstance(comment.paragraph_index, int)
            and 0 <= comment.paragraph_index < len(refs)
            and _ref_block_type(refs[comment.paragraph_index]) == "reference_heading"
            and _normalized_text(comment.message) in suppressed_heading_messages
        ):
            continue
        filtered.append(comment)

    filtered.sort(key=_comment_sort_key)
    return filtered


__all__ = [
    "_agent_scope_indexes",
    "_build_batches",
    "_consolidate_final_comments",
    "prepare_review_batches",
]
