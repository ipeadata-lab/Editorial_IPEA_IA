from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..agents import USER_REFERENCE_AGENT, run_user_reference_agent
from ..config import DEFAULT_REVIEW_SUMMARY_UPDATE_INTERVAL
from ..document_loader import Section
from ..llm import get_chat_model
from ..models import AgentComment, ConversationResult, DocumentUserComment, VerificationDecision
from ..prompts import build_agent_prompt
from ..review_patterns import _folded_text
from .context import PreparedReviewDocument
from .coordinator import build_coordinator_excerpt, coordinate_answer
from .runtime import (
    LLMConnectionFailure,
    _build_batch_review_excerpt,
    _comment_memory_lines,
    _connection_error_summary,
    _invoke_with_model_fallback,
    _is_json_body_error,
    _parse_comments_with_status,
    _partial_answer_from_comments,
    _sanitize_for_llm,
    _truncate_progressive_summary,
    _update_running_summary,
)
from .scope import prepare_review_batches, _consolidate_final_comments
from .validation import _format_batch_status, _review_comments_with_llm, _summarize_verification, _verify_batch_comments


class ChatState(TypedDict, total=False):
    question: str
    document_excerpt: str
    running_summary: str
    profile_key: str
    comments: list[AgentComment]
    answer: str
    batch_status: str
    llm_raw_comment_count: int
    llm_post_review_comment_count: int


def _agent_node(agent: str):
    def run(state: ChatState) -> ChatState:
        if get_chat_model() is None:
            return {
                "comments": state.get("comments", []),
                "batch_status": "modelo indisponível",
                "llm_raw_comment_count": 0,
                "llm_post_review_comment_count": 0,
            }

        prompt = build_agent_prompt(agent, profile_key=state.get("profile_key"))
        payload = {
            "question": _sanitize_for_llm(state["question"]),
            "document_excerpt": _sanitize_for_llm(state["document_excerpt"]),
        }
        try:
            response = _invoke_with_model_fallback(prompt, payload, operation=f"agente {agent}")
            if response is None:
                return {
                    "comments": state.get("comments", []),
                    "batch_status": "modelo indisponível",
                    "llm_raw_comment_count": 0,
                    "llm_post_review_comment_count": 0,
                }
        except LLMConnectionFailure as exc:
            return {
                "comments": state.get("comments", []),
                "batch_status": f"falha de conexão da LLM após retries: {_connection_error_summary(exc.original)}",
                "llm_raw_comment_count": 0,
                "llm_post_review_comment_count": 0,
            }
        except Exception as exc:
            if _is_json_body_error(exc):
                return {
                    "comments": state.get("comments", []),
                    "batch_status": "falha de payload da LLM",
                    "llm_raw_comment_count": 0,
                    "llm_post_review_comment_count": 0,
                }
            raise
        raw = response.content if isinstance(response.content, str) else str(response.content)
        items, status = _parse_comments_with_status(raw, agent=agent)
        reviewed_items, review_status = _review_comments_with_llm(
            items,
            agent=agent,
            question=state["question"],
            excerpt=state["document_excerpt"],
            profile_key=state.get("profile_key"),
        )
        merged = [*state.get("comments", []), *reviewed_items]
        combined_status = status if review_status == "revisor ignorado" else f"{status} | {review_status}"
        return {
            "comments": merged,
            "batch_status": combined_status,
            "llm_raw_comment_count": len(items),
            "llm_post_review_comment_count": len(reviewed_items),
        }

    return run


def _build_graph(agent_order: list[str], include_coordinator: bool = False):
    graph = StateGraph(ChatState)

    for agent in agent_order:
        graph.add_node(agent, _agent_node(agent))

    if not agent_order:
        graph.add_edge(START, END)
        return graph.compile()

    graph.add_edge(START, agent_order[0])
    for idx in range(len(agent_order) - 1):
        graph.add_edge(agent_order[idx], agent_order[idx + 1])
    graph.add_edge(agent_order[-1], END)
    return graph.compile()


def run_prepared_review(
    prepared_document: PreparedReviewDocument,
    question: str,
    selected_agents: list[str] | None = None,
    on_agent_done: Callable[[str, int, int], None] | None = None,
    on_agent_progress: Callable[[str, int, int, int, int], None] | None = None,
    on_agent_batch_status: Callable[[str, int, int, str], None] | None = None,
    profile_key: str = "GENERIC",
) -> ConversationResult:
    """Executa a revisão lote a lote sobre um documento já preparado."""
    agent_order = [agent for agent in (selected_agents or list(prepared_document.agent_batches.keys())) if agent in prepared_document.agent_batches]
    if not prepared_document.chunks:
        return ConversationResult(answer="Documento vazio ou sem texto extraído.", comments=[])

    agent_apps = {agent: _build_graph([agent], include_coordinator=False) for agent in agent_order}
    final_comments: list[AgentComment] = []
    verification_decisions: list[VerificationDecision] = []
    running_summaries = {agent: "" for agent in agent_order}
    failed_agents: list[tuple[str, str]] = []

    for agent in agent_order:
        if agent == USER_REFERENCE_AGENT:
            added_comments, agent_decisions = run_user_reference_agent(
                prepared_document=prepared_document,
                question=question,
                profile_key=profile_key,
                existing_comments=final_comments,
                on_agent_done=on_agent_done,
                on_agent_progress=on_agent_progress,
                on_agent_batch_status=on_agent_batch_status,
            )
            final_comments.extend(added_comments)
            verification_decisions.extend(agent_decisions)
            running_summaries[agent] = _truncate_progressive_summary(
                _comment_memory_lines(added_comments) if added_comments else "(nenhuma referência adicional foi inserida a partir de comentários do usuário)"
            )
            continue

        batches = prepared_document.agent_batches.get(agent, [])
        if not batches:
            continue

        for batch_idx, batch in enumerate(batches, start=1):
            excerpt = _build_batch_review_excerpt(
                prepared=prepared_document,
                batch=batch,
                running_summary=running_summaries.get(agent, ""),
                agent=agent,
            )
            comments_before_batch = len(final_comments)
            accepted_in_batch: list[AgentComment] = []
            batch_failed = False
            initial_state: ChatState = {
                "question": question,
                "document_excerpt": excerpt,
                "running_summary": running_summaries.get(agent, ""),
                "profile_key": profile_key,
                "comments": final_comments,
                "answer": "",
            }

            for update in agent_apps[agent].stream(initial_state, stream_mode="updates"):
                if not update:
                    continue
                node, payload = next(iter(update.items()))
                if not isinstance(payload, dict) or node != agent:
                    continue

                current_comments = payload.get("comments", final_comments)
                if isinstance(current_comments, list):
                    old_comments = current_comments[:comments_before_batch]
                    batch_comments = current_comments[comments_before_batch:]
                    verified_comments, batch_decisions = _verify_batch_comments(
                        comments=batch_comments,
                        agent=agent,
                        batch_indexes=batch.indexes,
                        chunks=prepared_document.chunks,
                        refs=prepared_document.refs,
                        reference_pipeline=prepared_document.reference_pipeline,
                        existing_comments=old_comments,
                        batch_index=batch_idx,
                    )
                    final_comments = [*old_comments, *verified_comments]
                    accepted_in_batch = verified_comments
                    verification_decisions.extend(batch_decisions)
                else:
                    batch_decisions = []

                batch_status = _format_batch_status(str(payload.get("batch_status", "") or ""), batch_decisions)
                total = len(final_comments)
                new_count = sum(1 for decision in batch_decisions if decision.accepted)
                if on_agent_done is not None:
                    on_agent_done(agent, new_count, total)
                if on_agent_batch_status is not None:
                    on_agent_batch_status(agent, batch_idx, len(batches), batch_status)
                if on_agent_progress is not None:
                    on_agent_progress(agent, batch_idx, len(batches), new_count, total)
                if "falha de conexao da llm" in _folded_text(batch_status):
                    failed_agents.append((agent, batch_status))
                    batch_failed = True
                    continue

            if batch_failed:
                continue

            should_refresh_summary = (
                batch_idx == len(batches)
                or batch_idx % max(1, DEFAULT_REVIEW_SUMMARY_UPDATE_INTERVAL) == 0
            )
            running_summaries[agent] = _update_running_summary(
                agent=agent,
                question=question,
                running_summary=running_summaries.get(agent, ""),
                batch=batch,
                accepted_comments=accepted_in_batch,
                use_llm=should_refresh_summary,
            )

    consolidated_comments = _consolidate_final_comments(final_comments, prepared_document.refs)

    if failed_agents:
        failed_summary = "; ".join(f"{agent}: {status}" for agent, status in failed_agents)
        base_answer = coordinate_answer(question=question, comments=consolidated_comments)
        final_answer = (
            (base_answer or "").rstrip()
            + "\n\n"
            + f"Avisos de execução: alguns agentes ficaram indisponíveis por falha de conexão da LLM: {failed_summary}."
        ).strip()
    else:
        _ = build_coordinator_excerpt(
            total_chunks=len(prepared_document.chunks),
            agent_order=agent_order,
            toc=prepared_document.toc,
        )
        final_answer = coordinate_answer(question=question, comments=consolidated_comments)

    return ConversationResult(
        answer=final_answer,
        comments=consolidated_comments,
        verification=_summarize_verification(verification_decisions),
    )


def run_conversation(
    paragraphs: list[str],
    refs: list[str],
    sections: list[Section],
    question: str,
    selected_agents: list[str] | None = None,
    user_comments: list[DocumentUserComment] | None = None,
    on_agent_done: Callable[[str, int, int], None] | None = None,
    on_agent_progress: Callable[[str, int, int, int, int], None] | None = None,
    on_agent_batch_status: Callable[[str, int, int, str], None] | None = None,
    profile_key: str = "GENERIC",
) -> ConversationResult:
    """Orquestra o fluxo completo: preparação, revisão e consolidação final."""
    if not paragraphs:
        return ConversationResult(answer="Documento vazio ou sem texto extraído.", comments=[])
    prepared_document = prepare_review_batches(
        paragraphs=paragraphs,
        refs=refs,
        sections=sections,
        selected_agents=selected_agents,
        user_comments=user_comments,
    )
    return run_prepared_review(
        prepared_document=prepared_document,
        question=question,
        selected_agents=selected_agents,
        on_agent_done=on_agent_done,
        on_agent_progress=on_agent_progress,
        on_agent_batch_status=on_agent_batch_status,
        profile_key=profile_key,
    )


__all__ = ["ChatState", "run_conversation", "run_prepared_review"]
