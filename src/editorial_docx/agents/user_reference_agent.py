from __future__ import annotations

from collections.abc import Callable

from ..llm import get_chat_model
from ..models import AgentComment, VerificationDecision
from ..pipeline.context import PreparedReviewDocument
from ..pipeline.runtime import _invoke_with_model_fallback, _parse_comments_with_status, _sanitize_for_llm
from ..prompts import build_agent_prompt
from ..review_patterns import _comment_key, _normalized_text, _ref_block_type
from ..user_comment_refs import (
    ReferenceSearchRequest,
    build_reference_search_requests,
    candidates_as_json,
    format_reference_candidate,
    reference_already_present,
    search_reference_candidates,
)

USER_REFERENCE_AGENT = "comentarios_usuario_referencias"


def _reference_entry_texts(chunks: list[str], refs: list[str]) -> list[str]:
    return [
        chunk
        for chunk, ref in zip(chunks, refs)
        if _ref_block_type(ref) == "reference_entry" and (chunk or "").strip()
    ]


def _reference_insertion_index(refs: list[str]) -> int | None:
    last_entry = next(
        (idx for idx in range(len(refs) - 1, -1, -1) if _ref_block_type(refs[idx]) == "reference_entry"),
        None,
    )
    if last_entry is not None:
        return last_entry
    return next((idx for idx, ref in enumerate(refs) if _ref_block_type(ref) == "reference_heading"), None)


def _build_user_reference_excerpt(
    request: ReferenceSearchRequest,
    candidates_json: str,
    refs: list[str],
    chunks: list[str],
) -> str:
    ref_lines = [
        f"[{idx}] {chunk}"
        for idx, (chunk, ref) in enumerate(zip(chunks, refs))
        if _ref_block_type(ref) == "reference_entry"
    ]
    summarized_refs = "\n".join(ref_lines[:25]) if ref_lines else "(lista final vazia ou não identificada)"
    anchor = request.anchor_excerpt.strip() or request.paragraph_text.strip()
    return (
        "SOLICITAÇÃO DO USUÁRIO/EDITOR:\n"
        f"- Comentário original: {request.comment_text.strip()}\n"
        f"- Índice global do trecho: [{request.paragraph_index}]\n"
        f"- Trecho âncora: {anchor}\n"
        f"- Parágrafo completo: {request.paragraph_text.strip()}\n"
        f"- Consulta de busca usada: {request.query_text.strip()}\n\n"
        "REFERÊNCIAS JÁ PRESENTES NA LISTA FINAL:\n"
        f"{summarized_refs}\n\n"
        "CANDIDATOS LOCALIZADOS NA INTERNET:\n"
        f"{candidates_json}"
    )


def _accept_user_reference_comment(
    base_comment: AgentComment,
    request: ReferenceSearchRequest,
    refs: list[str],
) -> AgentComment | None:
    insertion_index = _reference_insertion_index(refs)
    if insertion_index is None:
        return None

    suggested_fix = (base_comment.suggested_fix or "").strip()
    if not suggested_fix:
        return None

    anchor_excerpt = (request.anchor_excerpt or request.paragraph_text or "").strip()
    if not anchor_excerpt:
        return None

    message = (base_comment.message or "").strip()
    if "comentário do usuário" not in _normalized_text(message):
        message = (message + " " if message else "") + "Referência localizada e anexada por solicitação registrada em comentário do usuário."

    return AgentComment(
        agent=USER_REFERENCE_AGENT,
        category="user_reference_request",
        message=message.strip(),
        paragraph_index=request.paragraph_index,
        issue_excerpt=anchor_excerpt,
        suggested_fix=suggested_fix,
        auto_apply=True,
        format_spec=(
            "action=insert_reference;"
            f"insert_after={insertion_index};"
            f"source_comment_id={request.comment_id}"
        ),
    )


def run_user_reference_agent(
    prepared_document: PreparedReviewDocument,
    question: str,
    profile_key: str,
    existing_comments: list[AgentComment],
    on_agent_done: Callable[[str, int, int], None] | None = None,
    on_agent_progress: Callable[[str, int, int, int, int], None] | None = None,
    on_agent_batch_status: Callable[[str, int, int, str], None] | None = None,
) -> tuple[list[AgentComment], list[VerificationDecision]]:
    """Resolve pedidos explícitos do usuário para buscar e anexar referências."""
    requests = build_reference_search_requests(prepared_document.user_comments)
    if not requests:
        if on_agent_batch_status is not None:
            on_agent_batch_status(USER_REFERENCE_AGENT, 1, 1, "sem solicitações explícitas em comentários do usuário")
        if on_agent_progress is not None:
            on_agent_progress(USER_REFERENCE_AGENT, 1, 1, 0, len(existing_comments))
        if on_agent_done is not None:
            on_agent_done(USER_REFERENCE_AGENT, 0, len(existing_comments))
        return [], []

    prompt = build_agent_prompt(USER_REFERENCE_AGENT, profile_key=profile_key)
    accepted: list[AgentComment] = []
    decisions: list[VerificationDecision] = []
    reference_entries = _reference_entry_texts(prepared_document.chunks, prepared_document.refs)

    for batch_idx, request in enumerate(requests, start=1):
        candidates = search_reference_candidates(request)
        candidates = [
            candidate
            for candidate in candidates
            if not reference_already_present(format_reference_candidate(candidate), reference_entries)
        ]

        if not candidates:
            if on_agent_batch_status is not None:
                on_agent_batch_status(USER_REFERENCE_AGENT, batch_idx, len(requests), "nenhum candidato novo localizado")
            if on_agent_progress is not None:
                on_agent_progress(USER_REFERENCE_AGENT, batch_idx, len(requests), 0, len(existing_comments) + len(accepted))
            if on_agent_done is not None:
                on_agent_done(USER_REFERENCE_AGENT, 0, len(existing_comments) + len(accepted))
            continue

        if get_chat_model() is None:
            if on_agent_batch_status is not None:
                on_agent_batch_status(USER_REFERENCE_AGENT, batch_idx, len(requests), "modelo indisponível")
            if on_agent_progress is not None:
                on_agent_progress(USER_REFERENCE_AGENT, batch_idx, len(requests), 0, len(existing_comments) + len(accepted))
            if on_agent_done is not None:
                on_agent_done(USER_REFERENCE_AGENT, 0, len(existing_comments) + len(accepted))
            continue

        excerpt = _build_user_reference_excerpt(
            request=request,
            candidates_json=candidates_as_json(candidates),
            refs=prepared_document.refs,
            chunks=prepared_document.chunks,
        )
        payload = {
            "question": _sanitize_for_llm(question),
            "document_excerpt": _sanitize_for_llm(excerpt),
        }

        try:
            response = _invoke_with_model_fallback(prompt, payload, operation=f"agente {USER_REFERENCE_AGENT}")
            if response is None:
                raise RuntimeError("modelo indisponível")
            raw = response.content if isinstance(response.content, str) else str(response.content)
            parsed_items, status = _parse_comments_with_status(raw, agent=USER_REFERENCE_AGENT)
        except Exception as exc:
            status = f"falha no agente: {exc}"
            parsed_items = []

        added_count = 0
        existing_keys = {_comment_key(existing) for existing in [*existing_comments, *accepted]}
        for item in parsed_items:
            candidate_comment = _accept_user_reference_comment(item, request=request, refs=prepared_document.refs)
            if candidate_comment is None:
                continue
            if reference_already_present(candidate_comment.suggested_fix, reference_entries):
                continue
            key = _comment_key(candidate_comment)
            if key in existing_keys:
                continue
            accepted.append(candidate_comment)
            existing_keys.add(key)
            decisions.append(
                VerificationDecision(
                    comment=candidate_comment,
                    accepted=True,
                    reason="referência localizada a partir de comentário do usuário",
                    source="user_comment_agent",
                    batch_index=batch_idx,
                )
            )
            reference_entries.append(candidate_comment.suggested_fix)
            added_count += 1

        if on_agent_batch_status is not None:
            on_agent_batch_status(USER_REFERENCE_AGENT, batch_idx, len(requests), status)
        if on_agent_progress is not None:
            on_agent_progress(USER_REFERENCE_AGENT, batch_idx, len(requests), added_count, len(existing_comments) + len(accepted))
        if on_agent_done is not None:
            on_agent_done(USER_REFERENCE_AGENT, added_count, len(existing_comments) + len(accepted))

    return accepted, decisions


__all__ = ["USER_REFERENCE_AGENT", "run_user_reference_agent"]
