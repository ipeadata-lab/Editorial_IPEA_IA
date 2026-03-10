from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .context_selector import build_excerpt
from .document_loader import Section
from .llm import get_chat_model
from .models import AgentComment, ConversationResult
from .prompts import AGENT_ORDER, build_agent_prompt, build_coordinator_prompt


class ChatState(TypedDict):
    question: str
    document_excerpt: str
    comments: list[AgentComment]
    answer: str


_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")


def _sanitize_for_llm(text: str) -> str:
    # Remove characters that frequently break JSON payload parsing upstream.
    cleaned = _CTRL_RE.sub(" ", text or "")
    cleaned = _SURROGATE_RE.sub(" ", cleaned)
    return cleaned.replace("\ufeff", " ").strip()


def _is_json_body_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "could not parse the json body of your request" in msg


def _serialize_comments(comments: list[AgentComment]) -> str:
    return json.dumps(
        [
            {
                "agent": c.agent,
                "category": c.category,
                "message": c.message,
                "paragraph_index": c.paragraph_index,
                "issue_excerpt": c.issue_excerpt,
                "suggested_fix": c.suggested_fix,
            }
            for c in comments
        ],
        ensure_ascii=False,
        indent=2,
    )


def _parse_comments(raw: str, agent: str) -> list[AgentComment]:
    try:
        parsed = json.loads(raw)
    except Exception:
        return []

    if not isinstance(parsed, list):
        return []

    out: list[AgentComment] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message", "")).strip()
        if not message:
            continue
        category = str(item.get("category", agent)).strip() or agent
        paragraph_index = item.get("paragraph_index")
        if isinstance(paragraph_index, str) and paragraph_index.isdigit():
            paragraph_index = int(paragraph_index)
        if not isinstance(paragraph_index, int):
            paragraph_index = None
        issue_excerpt = str(item.get("issue_excerpt", "")).strip()
        suggested_fix = str(item.get("suggested_fix", "")).strip()
        out.append(
            AgentComment(
                agent=agent,
                category=category,
                message=message,
                paragraph_index=paragraph_index,
                issue_excerpt=issue_excerpt,
                suggested_fix=suggested_fix,
            )
        )
    return out


def _agent_node(agent: str):
    def run(state: ChatState) -> ChatState:
        model = get_chat_model()
        if model is None:
            return {"comments": state.get("comments", [])}

        prompt = build_agent_prompt(agent)
        payload = {
            "question": _sanitize_for_llm(state["question"]),
            "document_excerpt": _sanitize_for_llm(state["document_excerpt"]),
        }
        try:
            response = (prompt | model).invoke(payload)
        except Exception as exc:
            if _is_json_body_error(exc):
                # Keep pipeline alive for the next batches/agents.
                return {"comments": state.get("comments", [])}
            raise
        raw = response.content if isinstance(response.content, str) else str(response.content)
        items = _parse_comments(raw, agent=agent)
        merged = [*state.get("comments", []), *items]
        return {"comments": merged}

    return run


def _coordinator_node(state: ChatState) -> ChatState:
    model = get_chat_model()
    comments = state.get("comments", [])
    if model is None:
        if comments:
            points = "\n".join(f"- [{c.agent}] {c.message}" for c in comments[:8])
            answer = "Resumo dos agentes:\n" + points
        else:
            answer = "Não foi possível consultar a LLM. Configure OPENAI_API_KEY no .env."
        return {"answer": answer}

    prompt = build_coordinator_prompt()
    payload = {
        "question": _sanitize_for_llm(state["question"]),
        "document_excerpt": _sanitize_for_llm(state["document_excerpt"]),
        "comments_json": _sanitize_for_llm(_serialize_comments(comments)),
    }
    try:
        response = (prompt | model).invoke(payload)
    except Exception as exc:
        if _is_json_body_error(exc):
            if comments:
                points = "\n".join(f"- [{c.agent}] {c.message}" for c in comments[:12])
                return {"answer": "Resumo parcial (falha de payload da LLM no coordenador):\n" + points}
            return {"answer": "Falha ao montar payload para a LLM nesta execução."}
        raise
    answer = response.content if isinstance(response.content, str) else str(response.content)
    return {"answer": answer}


def _build_graph(agent_order: list[str], include_coordinator: bool = True):
    graph = StateGraph(ChatState)

    for agent in agent_order:
        graph.add_node(agent, _agent_node(agent))

    if include_coordinator:
        graph.add_node("coordenador", _coordinator_node)

    if not agent_order and include_coordinator:
        graph.add_edge(START, "coordenador")
    else:
        if agent_order:
            graph.add_edge(START, agent_order[0])
            for idx in range(len(agent_order) - 1):
                graph.add_edge(agent_order[idx], agent_order[idx + 1])
            if include_coordinator:
                graph.add_edge(agent_order[-1], "coordenador")
        elif include_coordinator:
            graph.add_edge(START, "coordenador")

    if include_coordinator:
        graph.add_edge("coordenador", END)
    elif agent_order:
        graph.add_edge(agent_order[-1], END)
    else:
        graph.add_edge(START, END)
    return graph.compile()


def _build_batches(
    chunks: list[str],
    refs: list[str],
    indexes: list[int],
    max_chars: int = 12000,
    max_chunks: int = 28,
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


def _expand_section_ranges(sections: list[Section], keywords: tuple[str, ...]) -> list[int]:
    selected: list[int] = []
    for sec in sections:
        title = sec.title.lower()
        if any(k in title for k in keywords):
            selected.extend(range(sec.start_idx, sec.end_idx + 1))
    return sorted(dict.fromkeys(selected))


def _find_content_indexes(chunks: list[str], pattern: str) -> list[int]:
    rx = re.compile(pattern, re.IGNORECASE)
    out: list[int] = []
    for idx, chunk in enumerate(chunks):
        if rx.search(chunk):
            out.append(idx)
    return out


def _agent_scope_indexes(agent: str, chunks: list[str], sections: list[Section]) -> list[int]:
    total = len(chunks)
    if total == 0:
        return []

    all_indexes = list(range(total))
    head_20 = list(range(max(1, int(total * 0.20))))
    tail_30_start = max(0, int(total * 0.70))
    tail_30 = list(range(tail_30_start, total))

    if agent == "metadados":
        sec = _expand_section_ranges(
            sections,
            ("metadad", "ficha catalogr", "capa", "titulo", "autoria"),
        )
        return sec or head_20

    if agent == "sinopse_abstract":
        sec = _expand_section_ranges(
            sections,
            ("sinopse", "abstract", "resumo", "summary"),
        )
        content = _find_content_indexes(chunks, r"\b(sinopse|abstract|resumo|summary)\b")
        picked = sorted(dict.fromkeys([*sec, *content]))
        return picked or head_20

    if agent == "tabelas_figuras":
        sec = _expand_section_ranges(
            sections,
            ("tabela", "figura", "quadro", "grafico", "gráfico", "anexo"),
        )
        content = _find_content_indexes(chunks, r"\b(tabela|figura|quadro|gr[aá]fico|imagem)\b")
        picked = sorted(dict.fromkeys([*sec, *content]))
        return picked or all_indexes

    if agent == "referencias":
        sec = _expand_section_ranges(
            sections,
            ("refer", "bibliograf", "references", "bibliography"),
        )
        if sec:
            return sec
        content = _find_content_indexes(chunks, r"\b(doi|http://|https://|et al\.|v\.\s*\d+|n\.\s*\d+)\b")
        picked = sorted(dict.fromkeys(content))
        if not picked:
            return tail_30
        return picked

    if agent == "gramatica_ortografia":
        return all_indexes

    # estrutura e conformidade de estilos: visão completa do documento
    return all_indexes


def run_conversation(
    paragraphs: list[str],
    refs: list[str],
    sections: list[Section],
    question: str,
    selected_agents: list[str] | None = None,
    on_agent_done: Callable[[str, int, int], None] | None = None,
    on_agent_progress: Callable[[str, int, int, int, int], None] | None = None,
) -> ConversationResult:
    agent_order = [a for a in (selected_agents or AGENT_ORDER) if a in AGENT_ORDER]
    if not paragraphs:
        return ConversationResult(answer="Documento vazio ou sem texto extraído.", comments=[])
    agent_apps = {agent: _build_graph([agent], include_coordinator=False) for agent in agent_order}

    final_comments: list[AgentComment] = []
    previous_count = 0

    for agent in agent_order:
        scoped_indexes = _agent_scope_indexes(agent, paragraphs, sections)
        batches = _build_batches(paragraphs, refs, scoped_indexes)
        if not batches:
            continue

        for batch_idx, batch_indexes in enumerate(batches, start=1):
            excerpt = build_excerpt(indexes=batch_indexes, chunks=paragraphs, refs=refs, max_chars=1_000_000)
            initial_state: ChatState = {
                "question": question,
                "document_excerpt": excerpt,
                "comments": final_comments,
                "answer": "",
            }

            for update in agent_apps[agent].stream(initial_state, stream_mode="updates"):
                if not update:
                    continue
                node, payload = next(iter(update.items()))
                if not isinstance(payload, dict):
                    continue
                if node != agent:
                    continue

                current_comments = payload.get("comments", final_comments)
                if isinstance(current_comments, list):
                    final_comments = current_comments
                total = len(final_comments)
                new_count = max(total - previous_count, 0)
                previous_count = total
                if on_agent_done is not None:
                    on_agent_done(agent, new_count, total)
                if on_agent_progress is not None:
                    on_agent_progress(agent, batch_idx, len(batches), new_count, total)

    coordinator_state: ChatState = {
        "question": question,
        "document_excerpt": (
            "Revisão por escopo de agente concluída. "
            f"Total de trechos no documento: {len(paragraphs)}. "
            f"Agentes executados: {', '.join(agent_order)}."
        ),
        "comments": final_comments,
        "answer": "",
    }
    final_answer = _coordinator_node(coordinator_state).get("answer", "")

    return ConversationResult(answer=final_answer, comments=final_comments)
