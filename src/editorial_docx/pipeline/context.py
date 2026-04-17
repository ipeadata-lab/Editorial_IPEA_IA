from __future__ import annotations

from dataclasses import dataclass, field

from ..config import (
    DEFAULT_REVIEW_FOCUS_EXCERPT_MAX_CHARS,
    DEFAULT_REVIEW_MAX_BATCH_CHARS,
    DEFAULT_REVIEW_MAX_BATCH_CHUNKS,
    DEFAULT_REVIEW_WINDOW_RADIUS,
    DEFAULT_REVIEW_WINDOW_EXCERPT_MAX_CHARS,
    GRAMMAR_CONTEXT_MODE,
    TEXTO_INTEIRO,
)
from ..context_selector import build_excerpt
from ..document_loader import Section
from ..models import DocumentUserComment, ReferencePipelineArtifact
from ..references.analysis import build_reference_pipeline_artifact
from ..token_utils import TokenChunkConfig, chunk_index_windows


@dataclass(slots=True)
class ReviewBatch:
    indexes: list[int]
    focus_excerpt: str
    window_excerpt: str
    headings: list[str] = field(default_factory=list)
    start_idx: int = 0
    end_idx: int = 0


@dataclass(slots=True)
class PreparedReviewDocument:
    chunks: list[str]
    refs: list[str]
    sections: list[Section]
    toc: list[str]
    user_comments: list[DocumentUserComment] = field(default_factory=list)
    agent_batches: dict[str, list[ReviewBatch]] = field(default_factory=dict)
    reference_pipeline: ReferencePipelineArtifact = field(default_factory=ReferencePipelineArtifact)


def _build_batches(
    chunks: list[str],
    refs: list[str],
    indexes: list[int],
    max_chars: int = DEFAULT_REVIEW_MAX_BATCH_CHARS,
    max_chunks: int = DEFAULT_REVIEW_MAX_BATCH_CHUNKS,
) -> list[list[int]]:
    if not chunks or not indexes:
        return []
    items: list[tuple[int, str]] = []
    for idx in indexes:
        if idx < 0 or idx >= len(chunks):
            continue
        ref = refs[idx] if idx < len(refs) else "sem referência"
        items.append((idx, f"[{idx}] ({ref}) {chunks[idx]}"))
    return chunk_index_windows(
        items,
        config=TokenChunkConfig(max_tokens=max(800, max_chars // 4), overlap_tokens=240, max_items=max_chunks),
    )


def _build_agent_batches(
    agent: str,
    *,
    chunks: list[str],
    refs: list[str],
    indexes: list[int],
    max_chars: int,
    max_chunks: int,
) -> list[list[int]]:
    if agent == "gramatica_ortografia" and GRAMMAR_CONTEXT_MODE == TEXTO_INTEIRO:
        filtered = [idx for idx in indexes if 0 <= idx < len(chunks)]
        return [filtered] if filtered else []
    return _build_batches(
        chunks=chunks,
        refs=refs,
        indexes=indexes,
        max_chars=max_chars,
        max_chunks=max_chunks,
    )


def _batch_excerpt_limit(
    agent: str,
    batch_indexes: list[int],
    chunks: list[str],
    refs: list[str],
    default_max_chars: int,
) -> int:
    if agent != "gramatica_ortografia" or GRAMMAR_CONTEXT_MODE != TEXTO_INTEIRO:
        return default_max_chars
    total = 0
    for idx in batch_indexes:
        if idx < 0 or idx >= len(chunks):
            continue
        ref = refs[idx] if idx < len(refs) else "sem referência"
        total += len(f"[{idx}] ({ref}) {chunks[idx]}") + 1
    return max(default_max_chars, total + 1)


def _window_indexes(indexes: list[int], total: int, radius: int = 2) -> list[int]:
    if not indexes or total <= 0:
        return []
    start = max(0, min(indexes) - radius)
    end = min(total - 1, max(indexes) + radius)
    return list(range(start, end + 1))


def _headings_for_batch(sections: list[Section], indexes: list[int]) -> list[str]:
    if not sections or not indexes:
        return []

    start = min(indexes)
    end = max(indexes)
    headings = [section.title for section in sections if not (section.end_idx < start or section.start_idx > end)]
    if headings:
        return headings[:4]

    nearest = [section.title for section in sections if section.start_idx <= start]
    if nearest:
        return nearest[-2:]
    return []


def prepare_review_document(
    chunks: list[str],
    refs: list[str],
    sections: list[Section],
    agent_order: list[str],
    agent_scope_builder,
    user_comments: list[DocumentUserComment] | None = None,
    max_batch_chars: int = DEFAULT_REVIEW_MAX_BATCH_CHARS,
    max_batch_chunks: int = DEFAULT_REVIEW_MAX_BATCH_CHUNKS,
    window_radius: int = DEFAULT_REVIEW_WINDOW_RADIUS,
    focus_excerpt_max_chars: int = DEFAULT_REVIEW_FOCUS_EXCERPT_MAX_CHARS,
    window_excerpt_max_chars: int = DEFAULT_REVIEW_WINDOW_EXCERPT_MAX_CHARS,
) -> PreparedReviewDocument:
    """Prepara lotes, janelas de contexto e TOC para todos os agentes."""
    toc = [f"{section.title} [{section.start_idx}-{section.end_idx}]" for section in sections]
    prepared = PreparedReviewDocument(
        chunks=chunks,
        refs=refs,
        sections=sections,
        toc=toc,
        user_comments=list(user_comments or []),
        reference_pipeline=build_reference_pipeline_artifact(chunks, refs),
    )

    for agent in agent_order:
        scoped_indexes = agent_scope_builder(agent, chunks, refs, sections)
        raw_batches = _build_agent_batches(
            agent,
            chunks=chunks,
            refs=refs,
            indexes=scoped_indexes,
            max_chars=max_batch_chars,
            max_chunks=max_batch_chunks,
        )
        prepared.agent_batches[agent] = [
            ReviewBatch(
                indexes=batch_indexes,
                focus_excerpt=build_excerpt(
                    indexes=batch_indexes,
                    chunks=chunks,
                    refs=refs,
                    max_chars=_batch_excerpt_limit(
                        agent,
                        batch_indexes,
                        chunks,
                        refs,
                        focus_excerpt_max_chars,
                    ),
                ),
                window_excerpt=build_excerpt(
                    indexes=_window_indexes(batch_indexes, total=len(chunks), radius=window_radius),
                    chunks=chunks,
                    refs=refs,
                    max_chars=window_excerpt_max_chars,
                ),
                headings=_headings_for_batch(sections, batch_indexes),
                start_idx=batch_indexes[0],
                end_idx=batch_indexes[-1],
            )
            for batch_indexes in raw_batches
            if batch_indexes
        ]

    return prepared
