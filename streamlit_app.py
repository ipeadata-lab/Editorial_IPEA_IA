from __future__ import annotations

import os
import json
import inspect
import hashlib
import html
import re
import unicodedata
from pathlib import Path
from typing import Callable

import streamlit as st

from src.editorial_docx.docx_utils import apply_comments_to_docx
from src.editorial_docx.document_loader import load_document
from src.editorial_docx.graph_chat import run_conversation
from src.editorial_docx.llm import get_llm_config, get_llm_model_tag
from src.editorial_docx.models import AgentComment, agent_short_label
from src.editorial_docx.prompts import AGENT_ORDER, detect_prompt_profile

st.set_page_config(page_title="Editorial TD - Agentes", layout="wide")
st.title("Revisão Editorial TD com Agentes")

AGENT_LABELS = {
    "metadados": "Metadados",
    "sinopse_abstract": "Sinopse/Abstract",
    "estrutura": "Estrutura",
    "tabelas_figuras": "Tabelas/Figuras",
    "referencias": "Referências",
    "gramatica_ortografia": "Gramática/Ortografia",
    "tipografia": "Tipografia",
}

project_root = Path(__file__).resolve().parent
env_path = project_root / ".env"

with st.sidebar:
    llm_config = get_llm_config()
    llm_model_tag = get_llm_model_tag(llm_config)
    st.markdown("### LLM")
    st.caption(
        f"Provider: `{llm_config['provider']}` | Modelo: `{llm_config['model']}`"
    )
    if llm_config.get("base_url"):
        st.caption(f"Base URL: `{llm_config['base_url']}`")
    if env_path.exists():
        st.caption("Arquivo .env detectado no repositório. Usando configuração local.")
    else:
        key_env = "OPENAI_API_KEY" if llm_config["provider"] == "openai" else "LLM_API_KEY"
        key_source = "variável de ambiente" if llm_config.get("api_key") else "não configurada"
        st.caption(f"Chave atual: {key_source}")
        if llm_config["provider"] != "ollama":
            api_key_input = st.text_input(
                key_env,
                type="password",
                help="A chave fica somente nesta sessão e não é salva em disco.",
            )
            if st.button("Usar chave nesta sessão", use_container_width=True):
                if api_key_input.strip():
                    os.environ[key_env] = api_key_input.strip()
                    st.success("Chave carregada para esta sessão.")
                else:
                    st.warning("Informe uma chave antes de confirmar.")
        else:
            st.caption("Provider Ollama configurado: chave não é obrigatória por padrão.")

    st.divider()
    st.markdown("### Execução")
    if st.button("Rodar todos os agentes", key="sidebar_run_all", use_container_width=True):
        st.session_state.pending_run = {
            "question": "Faça uma revisão completa com todos os agentes ativos e liste ajustes prioritários.",
            "agents": AGENT_ORDER.copy(),
            "source": "control:all",
        }

    st.markdown("#### Execução direta por agente")
    for agent in AGENT_ORDER:
        label = AGENT_LABELS.get(agent, agent)
        if st.button(f"Rodar: {label}", key=f"sidebar_run_{agent}", use_container_width=True):
            st.session_state.pending_run = {
                "question": f"Execute revisão focada em {label} e liste problemas com trecho e sugestão de correção.",
                "agents": [agent],
                "source": f"agent:{agent}",
            }

CHAT_HEIGHT_VH = 72

st.markdown(
    f"""
<style>
.st-key-chat_history_box {{
  height: {CHAT_HEIGHT_VH}vh;
  overflow-y: auto;
}}
.small-nav {{
  font-size: 0.82rem;
  line-height: 1.2;
}}
</style>
""",
    unsafe_allow_html=True,
)

for key, default in {
    "messages": [],
    "comments": [],
    "doc_path": None,
    "doc_bytes": b"",
    "paragraphs": [],
    "refs": [],
    "doc_kind": None,
    "doc_fingerprint": None,
    "doc_profile": "GENERIC",
    "sections": [],
    "toc": [],
    "selected_comment_row": 0,
    "correction_state": {},
    "comments_signature": "",
    "pending_run": None,
    "agent_result_cache": {},
    "agent_nav_idx": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _build_rows() -> list[dict]:
    rows = []
    for comment_idx, c in enumerate(st.session_state.comments):
        ref = "sem referência"
        if isinstance(c.paragraph_index, int) and 0 <= c.paragraph_index < len(st.session_state.refs):
            ref = st.session_state.refs[c.paragraph_index]

        rows.append(
            {
                "comment_idx": comment_idx,
                "agente": agent_short_label(c.agent),
                "agent_key": c.agent,
                "categoria": c.category,
                "referencia": ref,
                "indice_trecho": c.paragraph_index,
                "comentario": c.message,
                "trecho_com_problema": c.issue_excerpt,
                "como_deve_ficar": c.suggested_fix,
                "auto_aplicar": c.auto_apply,
                "format_spec": c.format_spec,
            }
        )
    return rows


def _merge_comments(existing: list[AgentComment], incoming: list[AgentComment]) -> list[AgentComment]:
    merged: list[AgentComment] = []
    seen: set[tuple[str, str, int | None, str, str, str, bool, str]] = set()

    for c in [*existing, *incoming]:
        key = (
            c.agent,
            c.category,
            c.paragraph_index,
            (c.message or "").strip(),
            (c.issue_excerpt or "").strip(),
            (c.suggested_fix or "").strip(),
            c.auto_apply,
            (c.format_spec or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    return merged


def _signature(rows: list[dict]) -> str:
    base = [
        (
            str(r["comment_idx"]),
            r["agent_key"],
            r["categoria"],
            str(r["indice_trecho"]),
            r["comentario"],
            r["trecho_com_problema"],
            r["como_deve_ficar"],
            r.get("auto_aplicar", False),
            r.get("format_spec", ""),
        )
        for r in rows
    ]
    return json.dumps(base, ensure_ascii=False)


def _ensure_correction_state(rows: list[dict]) -> None:
    sig = _signature(rows)
    if st.session_state.comments_signature != sig:
        st.session_state.comments_signature = sig
        st.session_state.correction_state = {}
        st.session_state.selected_comment_row = 0

    for idx, row in enumerate(rows):
        key = str(idx)
        if key not in st.session_state.correction_state:
            initial = row["como_deve_ficar"] or row["trecho_com_problema"] or ""
            initial_status = "resolvido" if row.get("auto_aplicar") else "pendente"
            initial_note = "Aplicado automaticamente pelo revisor de tipografia." if row.get("auto_aplicar") else ""
            st.session_state.correction_state[key] = {
                "status": initial_status,
                "final_text": initial,
                "observacao": initial_note,
            }


def _build_correction_report(rows: list[dict]) -> list[dict]:
    report = []
    for idx, row in enumerate(rows):
        state = st.session_state.correction_state.get(str(idx), {})
        report.append(
            {
                **row,
                "status": state.get("status", "pendente"),
                "texto_final_aprovado": state.get("final_text", ""),
                "observacao": state.get("observacao", ""),
            }
        )
    return report


def _build_export_comments(report_rows: list[dict]) -> list[AgentComment]:
    overrides: dict[int, dict] = {int(row["comment_idx"]): row for row in report_rows}
    export_comments: list[AgentComment] = []

    for idx, comment in enumerate(st.session_state.comments):
        override = overrides.get(idx)
        if override is None:
            export_comments.append(comment)
            continue

        export_comments.append(
            AgentComment(
                agent=override["agent_key"],
                category=override["categoria"],
                paragraph_index=override["indice_trecho"],
                message=override["comentario"],
                issue_excerpt=override["trecho_com_problema"],
                suggested_fix=override["como_deve_ficar"],
                auto_apply=override.get("auto_aplicar", False),
                format_spec=override.get("format_spec", ""),
                review_status=override.get("status", ""),
                approved_text=override.get("texto_final_aprovado", ""),
                reviewer_note=override.get("observacao", ""),
            )
        )
    return export_comments


def _set_status_value(key: str, value: str) -> None:
    st.session_state[key] = value


def _select_comment_row(index: int, visible_indexes: list[int], option_labels: list[str]) -> None:
    if not visible_indexes:
        st.session_state.selected_comment_row = 0
        return

    safe_index = max(0, min(index, len(visible_indexes) - 1))
    st.session_state.selected_comment_row = visible_indexes[safe_index]
    st.session_state.fix_select = option_labels[safe_index]


def _find_next_pending_index(
    rows: list[dict],
    current_index: int,
    visible_indexes: list[int],
) -> int:
    pending_indexes = [
        idx
        for idx in visible_indexes
        if st.session_state.correction_state.get(str(idx), {}).get("status") != "resolvido"
    ]
    for idx in pending_indexes:
        if idx > current_index:
            return idx
    if pending_indexes:
        return pending_indexes[0]

    for idx in visible_indexes:
        if idx > current_index:
            return idx
    return visible_indexes[-1] if visible_indexes else current_index


def _apply_suggestion_and_advance(
    rows: list[dict],
    final_key: str,
    final_value: str,
    status_key: str,
    current_index: int,
    visible_indexes: list[int],
    option_labels: list[str],
) -> None:
    st.session_state[final_key] = final_value
    st.session_state[status_key] = "resolvido"
    next_row_index = _find_next_pending_index(rows, current_index, visible_indexes)
    next_visible_pos = visible_indexes.index(next_row_index) if next_row_index in visible_indexes else 0
    _select_comment_row(next_visible_pos, visible_indexes, option_labels)


def _normalize_text_with_mapping(text: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    mapping: list[int] = []

    for idx, char in enumerate(text or ""):
        decomposed = unicodedata.normalize("NFD", char)
        stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
        if not stripped:
            continue

        normalized = stripped.lower()
        if normalized.isspace():
            normalized_chars.append(" ")
            mapping.append(idx)
            continue

        for part in normalized:
            normalized_chars.append(part)
            mapping.append(idx)

    collapsed_chars: list[str] = []
    collapsed_mapping: list[int] = []
    prev_space = False
    for char, idx in zip(normalized_chars, mapping):
        is_space = char.isspace()
        if is_space and prev_space:
            continue
        collapsed_chars.append(" " if is_space else char)
        collapsed_mapping.append(idx)
        prev_space = is_space

    return "".join(collapsed_chars), collapsed_mapping


def _find_excerpt_span(text: str, target: str) -> tuple[int, int] | None:
    if not text or not target:
        return None

    direct = re.search(re.escape(target), text, flags=re.IGNORECASE)
    if direct:
        return direct.span()

    normalized_text, text_mapping = _normalize_text_with_mapping(text)
    normalized_target, _ = _normalize_text_with_mapping(target)
    normalized_target = normalized_target.strip()
    if not normalized_text or not normalized_target:
        return None

    start = normalized_text.find(normalized_target)
    if start != -1:
        end = start + len(normalized_target) - 1
        return text_mapping[start], text_mapping[end] + 1

    target_tokens = [token for token in normalized_target.split() if len(token) >= 3]
    if not target_tokens:
        return None

    best_start = -1
    best_score = 0
    for token in target_tokens:
        token_pos = normalized_text.find(token)
        if token_pos == -1:
            continue
        score = sum(1 for part in target_tokens if part in normalized_text[token_pos : token_pos + len(normalized_target) + 80])
        if score > best_score:
            best_score = score
            best_start = token_pos

    if best_start == -1 or best_score == 0:
        return None

    end = min(best_start + len(normalized_target) + 40, len(text_mapping) - 1)
    return text_mapping[best_start], text_mapping[end] + 1


def _render_target_excerpt(paragraph: str, issue_excerpt: str) -> None:
    text = paragraph or ""
    target = (issue_excerpt or "").strip()

    if not target:
        st.text_area("Trecho-alvo", value=text, height=180, disabled=True)
        return

    span = _find_excerpt_span(text, target)
    if not span:
        st.text_area("Trecho-alvo", value=text, height=180, disabled=True)
        return

    start, end = span
    before = html.escape(text[:start])
    highlighted = html.escape(text[start:end])
    after = html.escape(text[end:])

    st.markdown("Trecho-alvo")
    st.markdown(
        (
            "<div style='border: 1px solid rgba(49, 51, 63, 0.2); border-radius: 0.5rem; "
            "padding: 0.75rem 1rem; background: rgb(249, 250, 251); "
            "font-family: \"Source Code Pro\", monospace; white-space: pre-wrap; line-height: 1.6;'>"
            f"{before}<mark style='background-color: #fff3a3; padding: 0.05rem 0.15rem;'>{highlighted}</mark>{after}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _run_review(
    question: str,
    agents: list[str],
    on_progress: Callable[[str, int, int, int, int, str], None] | None = None,
) -> tuple[str, list[AgentComment], list[str]]:
    logs: list[str] = []
    batch_statuses: dict[tuple[str, int], str] = {}

    def on_agent_done(agent: str, new_count: int, total: int) -> None:
        _ = (agent, new_count, total)

    def on_agent_batch_status(agent: str, batch_idx: int, batch_total: int, status: str) -> None:
        _ = batch_total
        batch_statuses[(agent, batch_idx)] = status

    def on_agent_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int) -> None:
        label = AGENT_LABELS.get(agent, agent)
        status = batch_statuses.get((agent, batch_idx), "")
        suffix = f" | {status}" if status and status not in {"json direto", "lista em `comments`"} else ""
        line = (
            (
                f"- `{label}` lote {batch_idx}/{batch_total}: "
                f"+{new_count} comentário(s), total {total}{suffix}"
            )
        )
        logs.append(line)
        if on_progress is not None:
            on_progress(agent, batch_idx, batch_total, new_count, total, status)

    kwargs = {
        "selected_agents": agents,
        "on_agent_done": on_agent_done,
    }
    params = inspect.signature(run_conversation).parameters
    if "on_agent_progress" in params:
        kwargs["on_agent_progress"] = on_agent_progress
    if "on_agent_batch_status" in params:
        kwargs["on_agent_batch_status"] = on_agent_batch_status

    result = run_conversation(
        st.session_state.paragraphs,
        st.session_state.refs,
        st.session_state.sections,
        question,
        profile_key=st.session_state.doc_profile,
        **kwargs,
    )
    return result.answer, result.comments, logs


uploaded = st.file_uploader("Ingestão do documento (.docx ou .pdf)", type=["docx", "pdf"])
if uploaded is not None:
    file_bytes = uploaded.getvalue()
    file_fingerprint = hashlib.sha256(file_bytes).hexdigest()
    profile = detect_prompt_profile(uploaded.name)
    st.session_state.doc_profile = profile.key

    if st.session_state.doc_fingerprint != file_fingerprint:
        st.session_state.messages = []
        st.session_state.comments = []
        st.session_state.agent_result_cache = {}
        st.session_state.selected_comment_row = 0
        st.session_state.correction_state = {}
        st.session_state.comments_signature = ""

        tmp_dir = Path(".tmp")
        tmp_dir.mkdir(exist_ok=True)
        doc_path = tmp_dir / uploaded.name
        doc_path.write_bytes(file_bytes)

        loaded = load_document(doc_path)
        st.session_state.doc_path = doc_path
        st.session_state.doc_bytes = file_bytes
        st.session_state.doc_fingerprint = file_fingerprint
        st.session_state.paragraphs = loaded.chunks
        st.session_state.refs = loaded.refs
        st.session_state.sections = loaded.sections
        st.session_state.toc = loaded.toc
        st.session_state.doc_kind = loaded.kind

col_chat, col_fix = st.columns([1.7, 1.1], gap="large")

with col_chat:
    st.subheader("Chat")

    if st.session_state.pending_run and st.session_state.paragraphs:
        run = st.session_state.pending_run
        st.session_state.pending_run = None
        st.session_state.messages.append({"role": "user", "content": f"[Ação rápida] {run['question']}"})
        progress_header = st.empty()
        progress_bar = st.progress(0, text="Iniciando revisão completa...")
        progress_box = st.empty()
        progress_lines: list[str] = []

        def _push_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int, status: str) -> None:
            label = AGENT_LABELS.get(agent, agent)
            pct = int((batch_idx / max(batch_total, 1)) * 100)
            suffix = f" | {status}" if status and status not in {"json direto", "lista em `comments`"} else ""
            progress_header.info(
                f"Processando: {label} | lote {batch_idx}/{batch_total} | +{new_count} comentário(s), total {total}{suffix}"
            )
            progress_bar.progress(pct, text=f"Progresso do documento: lote {batch_idx}/{batch_total}")
            line = f"- `{label}` lote {batch_idx}/{batch_total}: +{new_count} comentário(s), total {total}{suffix}"
            progress_lines.append(line)
            tail = progress_lines[-14:]
            progress_box.markdown("**Progresso da revisão:**\n" + "\n".join(tail))

        with st.spinner("Executando agentes..."):
            answer, comments, logs = _run_review(
                run["question"],
                run["agents"],
                on_progress=_push_progress,
            )
        progress_header.success("Revisão concluída.")
        progress_bar.progress(100, text="Processamento completo")
        progress_box.empty()
        merged = answer + ("\n\n" + "\n".join(logs) if logs else "")
        st.session_state.messages.append({"role": "assistant", "content": merged})
        st.session_state.comments = _merge_comments(st.session_state.comments, comments)
        if len(run["agents"]) == 1:
            agent = run["agents"][0]
            st.session_state.agent_result_cache[agent] = {
                "answer": answer,
                "comments": comments,
                "question": run["question"],
            }
        st.rerun()

    question = st.chat_input("Pergunte algo sobre o documento")
    if question and st.session_state.paragraphs:
        st.session_state.messages.append({"role": "user", "content": question})
        progress_header = st.empty()
        progress_bar = st.progress(0, text="Iniciando revisão completa...")
        progress_box = st.empty()
        progress_lines: list[str] = []

        def _push_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int, status: str) -> None:
            label = AGENT_LABELS.get(agent, agent)
            pct = int((batch_idx / max(batch_total, 1)) * 100)
            suffix = f" | {status}" if status and status not in {"json direto", "lista em `comments`"} else ""
            progress_header.info(
                f"Processando: {label} | lote {batch_idx}/{batch_total} | +{new_count} comentário(s), total {total}{suffix}"
            )
            progress_bar.progress(pct, text=f"Progresso do documento: lote {batch_idx}/{batch_total}")
            line = f"- `{label}` lote {batch_idx}/{batch_total}: +{new_count} comentário(s), total {total}{suffix}"
            progress_lines.append(line)
            tail = progress_lines[-14:]
            progress_box.markdown("**Progresso da revisão:**\n" + "\n".join(tail))

        with st.spinner("Executando agentes..."):
            answer, comments, logs = _run_review(
                question,
                AGENT_ORDER.copy(),
                on_progress=_push_progress,
            )
        progress_header.success("Revisão concluída.")
        progress_bar.progress(100, text="Processamento completo")
        progress_box.empty()
        merged = answer + ("\n\n" + "\n".join(logs) if logs else "")
        st.session_state.messages.append({"role": "assistant", "content": merged})
        st.session_state.comments = _merge_comments(st.session_state.comments, comments)
        st.rerun()
    elif question and not st.session_state.paragraphs:
        st.warning("Carregue um documento antes de conversar.")

    chat_box = st.container(height=560, border=True, key="chat_history_box")
    with chat_box:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    rows = _build_rows()
    if rows:
        _ensure_correction_state(rows)
        report = _build_correction_report(rows)
        st.subheader("Comentários dos agentes")
        st.dataframe(rows, width="stretch")

        if st.session_state.doc_path and st.session_state.doc_kind == "docx":
            output_bytes = apply_comments_to_docx(
                st.session_state.doc_path,
                _build_export_comments(report),
            )
            st.download_button(
                label="Baixar DOCX comentado",
                data=output_bytes,
                file_name=f"{Path(st.session_state.doc_path).stem}_output_{llm_model_tag}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        st.download_button(
            label="Baixar relatório de correções (JSON)",
            data=json.dumps(report, ensure_ascii=False, indent=2),
            file_name=f"{Path(st.session_state.doc_path).stem if st.session_state.doc_path else 'correcoes'}_output_{llm_model_tag}.relatorio.json",
            mime="application/json",
        )
    elif st.session_state.comments:
        if st.session_state.doc_path and st.session_state.doc_kind == "docx":
            output_bytes = apply_comments_to_docx(
                st.session_state.doc_path,
                st.session_state.comments,
            )
            st.download_button(
                label="Baixar DOCX com ajustes automáticos",
                data=output_bytes,
                file_name=f"{Path(st.session_state.doc_path).stem}_output_{llm_model_tag}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

with col_fix:
    st.subheader("Painel de Correção Assistida")

    st.markdown("<div class='small-nav'><b>Navegação por agente (sem rerun)</b></div>", unsafe_allow_html=True)
    nav_a, nav_b, nav_c = st.columns([1, 1, 2])
    with nav_a:
        if st.button("◀", key="agent_nav_prev"):
            st.session_state.agent_nav_idx = (st.session_state.agent_nav_idx - 1) % len(AGENT_ORDER)
    with nav_b:
        if st.button("▶", key="agent_nav_next"):
            st.session_state.agent_nav_idx = (st.session_state.agent_nav_idx + 1) % len(AGENT_ORDER)
    agent_cursor = AGENT_ORDER[st.session_state.agent_nav_idx]
    with nav_c:
        st.markdown(
            f"<div class='small-nav'>Agente atual: {AGENT_LABELS.get(agent_cursor, agent_cursor)}</div>",
            unsafe_allow_html=True,
        )

    if st.button("Abrir último resultado deste agente", key="agent_nav_open", width="stretch"):
        cached = st.session_state.agent_result_cache.get(agent_cursor)
        if cached:
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": (
                        f"[Cache de {AGENT_LABELS.get(agent_cursor, agent_cursor)}]\n\n"
                        f"{cached['answer']}"
                    ),
                }
            )
            st.session_state.comments = _merge_comments(st.session_state.comments, cached["comments"])
            st.rerun()
        else:
            st.info("Esse agente ainda não foi executado nesta sessão.")

    rows = _build_rows()

    if not rows:
        st.info("Execute uma revisão no chat ou no painel de controle para carregar itens.")
    else:
        _ensure_correction_state(rows)
        status_counts = {
            "pendente": sum(1 for v in st.session_state.correction_state.values() if v.get("status") == "pendente"),
            "em_revisao": sum(1 for v in st.session_state.correction_state.values() if v.get("status") == "em_revisao"),
            "resolvido": sum(1 for v in st.session_state.correction_state.values() if v.get("status") == "resolvido"),
        }
        st.caption(
            "Status: "
            f"{status_counts['pendente']} pendente(s) | "
            f"{status_counts['em_revisao']} em revisão | "
            f"{status_counts['resolvido']} resolvido(s)"
        )

        filter_a, filter_b, filter_c = st.columns([1.2, 1.2, 1])
        with filter_a:
            agent_filter = st.multiselect(
                "Filtrar agentes",
                options=sorted({row["agente"] for row in rows}),
                default=[],
                key="fix_agent_filter",
            )
        with filter_b:
            category_filter = st.multiselect(
                "Filtrar categorias",
                options=sorted({row["categoria"] for row in rows}),
                default=[],
                key="fix_category_filter",
            )
        with filter_c:
            pending_only = st.checkbox("Só pendentes", key="fix_pending_only")

        visible_indexes: list[int] = []
        option_labels: list[str] = []
        for i, row in enumerate(rows):
            agent_label = row["agente"]
            state = st.session_state.correction_state.get(str(i), {})
            if agent_filter and agent_label not in agent_filter:
                continue
            if category_filter and row["categoria"] not in category_filter:
                continue
            if pending_only and state.get("status") == "resolvido":
                continue

            snippet = row["comentario"][:65].replace("\n", " ")
            visible_indexes.append(i)
            option_labels.append(f"{i+1}. {row['referencia']} | {row['categoria']} | {snippet}")

        if not visible_indexes:
            st.info("Nenhum item corresponde aos filtros atuais.")
            st.stop()

        if st.session_state.selected_comment_row not in visible_indexes:
            st.session_state.selected_comment_row = visible_indexes[0]
            st.session_state.fix_select = option_labels[0]

        current_visible_pos = visible_indexes.index(st.session_state.selected_comment_row)

        nav_prev, nav_next, nav_info = st.columns([1, 1, 2])
        with nav_prev:
            st.button(
                "Anterior",
                key="fix_prev",
                disabled=current_visible_pos <= 0,
                on_click=_select_comment_row,
                args=(current_visible_pos - 1, visible_indexes, option_labels),
            )
        with nav_next:
            st.button(
                "Próximo",
                key="fix_next",
                disabled=current_visible_pos >= len(visible_indexes) - 1,
                on_click=_select_comment_row,
                args=(current_visible_pos + 1, visible_indexes, option_labels),
            )
        with nav_info:
            st.caption(f"Item {current_visible_pos + 1} de {len(visible_indexes)}")

        selected_option = st.selectbox(
            "Selecionar item:",
            option_labels,
            index=current_visible_pos,
            key="fix_select",
        )
        st.session_state.selected_comment_row = visible_indexes[option_labels.index(selected_option)]

        idx = st.session_state.selected_comment_row
        row = rows[idx]
        state_key = str(idx)
        state = st.session_state.correction_state[state_key]
        is_auto_apply = bool(row.get("auto_aplicar"))

        resolved = sum(1 for v in st.session_state.correction_state.values() if v.get("status") == "resolvido")
        st.progress(resolved / len(rows), text=f"{resolved}/{len(rows)} itens resolvidos")

        st.markdown(f"**Agente:** `{row['agente']}` | **Categoria:** `{row['categoria']}`")
        st.markdown(f"**Referência:** {row['referencia']}")
        if is_auto_apply:
            st.caption("Este item será aplicado automaticamente no DOCX exportado.")
        st.info(row["comentario"])

        st.markdown("**Trecho com problema**")
        st.code(row["trecho_com_problema"] or "(não informado)", language="text")

        st.markdown("**Sugestão do agente**")
        st.code(row["como_deve_ficar"] or "(não informado)", language="text")

        final_key = f"final_text_{idx}"
        status_key = f"status_{idx}"
        note_key = f"note_{idx}"

        if final_key not in st.session_state:
            st.session_state[final_key] = state.get("final_text", "")
        if status_key not in st.session_state:
            st.session_state[status_key] = state.get("status", "pendente")
        if note_key not in st.session_state:
            st.session_state[note_key] = state.get("observacao", "")

        st.text_area("Versão final (editável)", key=final_key, height=180, disabled=is_auto_apply)
        st.selectbox("Status", ["pendente", "em_revisao", "resolvido"], key=status_key, disabled=is_auto_apply)
        st.text_area("Observação do revisor", key=note_key, height=90, disabled=is_auto_apply)

        b1, b2, b3 = st.columns(3)
        with b1:
            st.button(
                "Usar sugestão",
                key="fix_use",
                disabled=is_auto_apply,
                on_click=_apply_suggestion_and_advance,
                args=(
                    rows,
                    final_key,
                    row["como_deve_ficar"] or st.session_state.get(final_key, ""),
                    status_key,
                    idx,
                    visible_indexes,
                    option_labels,
                ),
            )
        with b2:
            st.button(
                "Marcar resolvido",
                key="fix_done",
                disabled=is_auto_apply,
                on_click=_set_status_value,
                args=(status_key, "resolvido"),
            )
        with b3:
            st.button(
                "Reabrir",
                key="fix_reopen",
                disabled=is_auto_apply,
                on_click=_set_status_value,
                args=(status_key, "em_revisao"),
            )

        state["final_text"] = st.session_state.get(final_key, "")
        state["status"] = st.session_state.get(status_key, "pendente")
        state["observacao"] = st.session_state.get(note_key, "")

        pidx = row["indice_trecho"]
        st.markdown("### Contexto do documento")
        if isinstance(pidx, int) and 0 <= pidx < len(st.session_state.paragraphs):
            _render_target_excerpt(st.session_state.paragraphs[pidx], row["trecho_com_problema"])
            if pidx - 1 >= 0:
                st.text_area(
                    "Trecho anterior",
                    value=st.session_state.paragraphs[pidx - 1],
                    height=180,
                    disabled=True,
                )
            if pidx + 1 < len(st.session_state.paragraphs):
                st.text_area(
                    "Trecho seguinte",
                    value=st.session_state.paragraphs[pidx + 1],
                    height=180,
                    disabled=True,
                )
        else:
            st.caption("Sem índice de trecho para contexto.")
