from __future__ import annotations

from ..agents.validation import (
    basic_comment_rejection_reason as _basic_comment_rejection_reason,
    build_validation_context,
    detailed_rejection_reason as _agent_detailed_rejection_reason,
    find_excerpt_index as _find_excerpt_index,
    keep_rejection_reason as _agent_keep_rejection_reason,
    limit_auto_apply as _limit_auto_apply,
    matches_whole_paragraph as _matches_whole_paragraph,
    remap_comment_index as _remap_comment_index,
    semantic_comment_key as _semantic_comment_key,
)
from ..models import AgentComment, VerificationDecision, VerificationSummary
from ..prompts import build_comment_review_prompt
from ..review_heuristics import _heuristic_comments_for_agent
from ..review_patterns import (
    _comment_key,
    _comment_review_key,
    _dedupe_comments,
    _is_reference_missing_data_speculation,
    _normalized_text,
    _ref_block_type,
)
from .runtime import (
    LLMConnectionFailure,
    _connection_error_summary,
    _invoke_with_model_fallback,
    _parse_comment_reviews,
    _sanitize_for_llm,
    _serialize_comments,
)


def _should_keep_comment(comment: AgentComment, agent: str, chunks: list[str], refs: list[str]) -> bool:
    """Aplica filtros determinísticos para aceitar só comentários úteis e seguros."""
    reason = _basic_comment_rejection_reason(comment)
    if reason is not None:
        return False

    ctx = build_validation_context(comment, agent=agent, chunks=chunks, refs=refs)
    if (
        comment.issue_excerpt
        and agent in {"gramatica_ortografia", "referencias"}
        and _find_excerpt_index(comment.issue_excerpt, [comment.paragraph_index], chunks) is None
    ):
        return False
    return _agent_keep_rejection_reason(ctx) is None


def _comment_rejection_reason(comment: AgentComment, agent: str, chunks: list[str], refs: list[str]) -> str | None:
    basic_reason = _basic_comment_rejection_reason(comment)
    if basic_reason is not None:
        return basic_reason

    ctx = build_validation_context(comment, agent=agent, chunks=chunks, refs=refs)
    if (
        comment.issue_excerpt
        and agent in {"gramatica_ortografia", "referencias"}
        and _find_excerpt_index(comment.issue_excerpt, [comment.paragraph_index], chunks) is None
    ):
        return "descartado por regra de verificação"

    detailed_reason = _agent_detailed_rejection_reason(ctx)
    if detailed_reason is not None:
        return detailed_reason

    if agent == "referencias" and ctx.block_type in {"reference_entry", "reference_heading"}:
        if _is_reference_missing_data_speculation(comment.message, comment.suggested_fix):
            return "completude bibliográfica sem evidência local"

    keep_reason = _agent_keep_rejection_reason(ctx)
    if keep_reason is not None:
        return keep_reason
    return None


def _summarize_verification(decisions: list[VerificationDecision]) -> VerificationSummary:
    accepted_count = sum(1 for decision in decisions if decision.accepted)
    rejected_count = sum(1 for decision in decisions if not decision.accepted)
    return VerificationSummary(
        decisions=decisions[:],
        accepted_count=accepted_count,
        rejected_count=rejected_count,
    )


def _verify_batch_comments(
    comments: list[AgentComment],
    agent: str,
    batch_indexes: list[int],
    chunks: list[str],
    refs: list[str],
    existing_comments: list[AgentComment] | None = None,
    batch_index: int | None = None,
) -> tuple[list[AgentComment], list[VerificationDecision]]:
    """Combina saídas do LLM e heurísticas, removendo duplicatas e falsos positivos."""
    candidates: list[tuple[str, AgentComment]] = []
    for comment in comments:
        remapped = _limit_auto_apply(_remap_comment_index(comment, batch_indexes=batch_indexes, chunks=chunks))
        candidates.append(("llm", remapped))
    for comment in _heuristic_comments_for_agent(agent=agent, batch_indexes=batch_indexes, chunks=chunks, refs=refs):
        candidates.append(("heuristic", comment))

    accepted: list[AgentComment] = []
    decisions: list[VerificationDecision] = []
    seen_existing = {_comment_key(item) for item in (existing_comments or [])}
    seen_existing_semantic = {_semantic_comment_key(item) for item in (existing_comments or [])}
    seen_batch: set[tuple[str, str, int | None, str, str, str, bool, str]] = set()
    seen_batch_semantic: set[tuple[str, int | None, str, str]] = set()

    for source, candidate in candidates:
        key = _comment_key(candidate)
        semantic_key = _semantic_comment_key(candidate)
        if key in seen_existing or key in seen_batch or semantic_key in seen_existing_semantic or semantic_key in seen_batch_semantic:
            decisions.append(
                VerificationDecision(
                    comment=candidate,
                    accepted=False,
                    reason="comentário duplicado",
                    source=source,
                    batch_index=batch_index,
                )
            )
            continue

        reason = _basic_comment_rejection_reason(candidate)
        if reason is None and source == "llm":
            reason = _comment_rejection_reason(candidate, agent=agent, chunks=chunks, refs=refs)
        if reason is not None:
            decisions.append(
                VerificationDecision(
                    comment=candidate,
                    accepted=False,
                    reason=reason,
                    source=source,
                    batch_index=batch_index,
                )
            )
            continue

        accepted.append(candidate)
        seen_batch.add(key)
        seen_batch_semantic.add(semantic_key)
        decisions.append(
            VerificationDecision(
                comment=candidate,
                accepted=True,
                reason="aceito",
                source=source,
                batch_index=batch_index,
            )
        )

    return accepted, decisions


def _format_batch_status(status: str, decisions: list[VerificationDecision]) -> str:
    summary = _summarize_verification(decisions)
    base = (status or "").strip()
    suffix = f"verif: {summary.accepted_count} aceitos, {summary.rejected_count} rejeitados"
    return f"{base} | {suffix}" if base else suffix


def _normalize_batch_comments(
    comments: list[AgentComment],
    agent: str,
    batch_indexes: list[int],
    chunks: list[str],
    refs: list[str],
) -> list[AgentComment]:
    """Retorna apenas os comentários aprovados para um lote de revisão."""
    accepted, _ = _verify_batch_comments(
        comments=comments,
        agent=agent,
        batch_indexes=batch_indexes,
        chunks=chunks,
        refs=refs,
        existing_comments=[],
    )
    return accepted


_REVIEWER_ENABLED_AGENTS = {"sinopse_abstract"}


def _review_comments_with_llm(
    comments: list[AgentComment],
    agent: str,
    question: str,
    excerpt: str,
    profile_key: str | None,
) -> tuple[list[AgentComment], str]:
    """Passa os comentários elegíveis por um revisor LLM antes da consolidação."""
    if agent not in _REVIEWER_ENABLED_AGENTS or not comments:
        return comments, "revisor ignorado"

    prompt = build_comment_review_prompt(agent, profile_key=profile_key)
    payload = {
        "question": _sanitize_for_llm(question),
        "document_excerpt": _sanitize_for_llm(excerpt),
        "comments_json": _sanitize_for_llm(_serialize_comments(comments)),
    }
    try:
        response = _invoke_with_model_fallback(prompt, payload, operation=f"revisor {agent}")
        if response is None:
            return comments, "revisor indisponível"
    except LLMConnectionFailure as exc:
        return comments, f"revisor indisponível por conexão: {_connection_error_summary(exc.original)}"
    except Exception:
        return comments, "revisor indisponível"

    raw = response.content if isinstance(response.content, str) else str(response.content)
    reviews, status = _parse_comment_reviews(raw)
    if not reviews:
        return comments, status

    verdict_by_key = {
        _comment_review_key(
            item.get("paragraph_index"),
            str(item.get("issue_excerpt") or ""),
            str(item.get("suggested_fix") or ""),
        ): item
        for item in reviews
    }

    approved: list[AgentComment] = []
    rejected = 0
    for comment in comments:
        review = verdict_by_key.get(_comment_review_key(comment.paragraph_index, comment.issue_excerpt, comment.suggested_fix))
        if review and review.get("decision") == "reject":
            rejected += 1
            continue
        approved.append(comment)

    return approved, f"{status} | revisor: {len(approved)} aprovados, {rejected} rejeitados"


__all__ = [
    "_comment_rejection_reason",
    "_find_excerpt_index",
    "_format_batch_status",
    "_limit_auto_apply",
    "_matches_whole_paragraph",
    "_normalize_batch_comments",
    "_remap_comment_index",
    "_review_comments_with_llm",
    "_should_keep_comment",
    "_summarize_verification",
    "_verify_batch_comments",
]
