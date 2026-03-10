from __future__ import annotations

import json
import inspect
from pathlib import Path
from typing import Callable

import streamlit as st

from src.editorial_docx.docx_utils import apply_comments_to_docx
from src.editorial_docx.document_loader import load_document
from src.editorial_docx.graph_chat import run_conversation
from src.editorial_docx.models import AgentComment
from src.editorial_docx.prompts import AGENT_ORDER

st.set_page_config(page_title="Editorial TD - Agentes", layout="wide")
st.title("Revisão Editorial TD com Agentes")

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

AGENT_LABELS = {
    "metadados": "Metadados",
    "sinopse_abstract": "Sinopse/Abstract",
    "estrutura": "Estrutura",
    "tabelas_figuras": "Tabelas/Figuras",
    "referencias": "Referências",
    "conformidade_estilos": "Conformidade de Estilos",
    "gramatica_ortografia": "Gramática/Ortografia",
}

for key, default in {
    "messages": [],
    "comments": [],
    "doc_path": None,
    "doc_bytes": b"",
    "paragraphs": [],
    "refs": [],
    "doc_kind": None,
    "sections": [],
    "toc": [],
    "selected_comment_row": 0,
    "correction_state": {},
    "comments_signature": "",
    "pending_run": None,
    "agent_result_cache": {},
    "agent_nav_idx": 0,
    "control_collapsed": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _build_rows() -> list[dict]:
    rows = []
    for c in st.session_state.comments:
        ref = "sem referência"
        if isinstance(c.paragraph_index, int) and 0 <= c.paragraph_index < len(st.session_state.refs):
            ref = st.session_state.refs[c.paragraph_index]

        rows.append(
            {
                "agente": c.agent,
                "categoria": c.category,
                "referencia": ref,
                "indice_trecho": c.paragraph_index,
                "comentario": c.message,
                "trecho_com_problema": c.issue_excerpt,
                "como_deve_ficar": c.suggested_fix,
            }
        )
    return rows


def _merge_comments(existing: list[AgentComment], incoming: list[AgentComment]) -> list[AgentComment]:
    merged: list[AgentComment] = []
    seen: set[tuple[str, str, int | None, str, str, str]] = set()

    for c in [*existing, *incoming]:
        key = (
            c.agent,
            c.category,
            c.paragraph_index,
            (c.message or "").strip(),
            (c.issue_excerpt or "").strip(),
            (c.suggested_fix or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(c)
    return merged


def _signature(rows: list[dict]) -> str:
    base = [
        (
            r["agente"],
            r["categoria"],
            str(r["indice_trecho"]),
            r["comentario"],
            r["trecho_com_problema"],
            r["como_deve_ficar"],
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
            st.session_state.correction_state[key] = {
                "status": "pendente",
                "final_text": initial,
                "observacao": "",
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


def _run_review(
    question: str,
    agents: list[str],
    on_progress: Callable[[str, int, int, int, int], None] | None = None,
) -> tuple[str, list[AgentComment], list[str]]:
    logs: list[str] = []

    def on_agent_done(agent: str, new_count: int, total: int) -> None:
        _ = (agent, new_count, total)

    def on_agent_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int) -> None:
        label = AGENT_LABELS.get(agent, agent)
        line = (
            (
                f"- `{label}` lote {batch_idx}/{batch_total}: "
                f"+{new_count} comentário(s), total {total}"
            )
        )
        logs.append(line)
        if on_progress is not None:
            on_progress(agent, batch_idx, batch_total, new_count, total)

    kwargs = {
        "selected_agents": agents,
        "on_agent_done": on_agent_done,
    }
    params = inspect.signature(run_conversation).parameters
    if "on_agent_progress" in params:
        kwargs["on_agent_progress"] = on_agent_progress

    result = run_conversation(
        st.session_state.paragraphs,
        st.session_state.refs,
        st.session_state.sections,
        question,
        **kwargs,
    )
    return result.answer, result.comments, logs


uploaded = st.file_uploader("Ingestão do documento (.docx ou .pdf)", type=["docx", "pdf"])
if uploaded is not None:
    current_name = st.session_state.doc_path.name if st.session_state.doc_path else None
    if current_name != uploaded.name:
        st.session_state.messages = []
        st.session_state.comments = []
        st.session_state.agent_result_cache = {}
        st.session_state.selected_comment_row = 0
        st.session_state.correction_state = {}
        st.session_state.comments_signature = ""

    tmp_dir = Path(".tmp")
    tmp_dir.mkdir(exist_ok=True)
    doc_path = tmp_dir / uploaded.name
    file_bytes = uploaded.getvalue()
    doc_path.write_bytes(file_bytes)

    loaded = load_document(doc_path)
    st.session_state.doc_path = doc_path
    st.session_state.doc_bytes = file_bytes
    st.session_state.paragraphs = loaded.chunks
    st.session_state.refs = loaded.refs
    st.session_state.sections = loaded.sections
    st.session_state.toc = loaded.toc
    st.session_state.doc_kind = loaded.kind

control_w = 0.32 if st.session_state.control_collapsed else 0.85
col_control, col_chat, col_fix = st.columns([control_w, 1.35, 1.1], gap="large")

with col_control:
    t1, t2 = st.columns([1, 1])
    with t1:
        st.subheader("Execução")
    with t2:
        icon = "»" if st.session_state.control_collapsed else "«"
        if st.button(icon, key="toggle_control", help="Minimizar/expandir painel", width="stretch"):
            st.session_state.control_collapsed = not st.session_state.control_collapsed
            st.rerun()

    if st.session_state.control_collapsed:
        if st.button("🧠", key="run_all_icon", help="Rodar todos os agentes", width="stretch"):
            st.session_state.pending_run = {
                "question": "Faça uma revisão completa com todos os agentes e liste ajustes prioritários.",
                "agents": AGENT_ORDER.copy(),
                "source": "control:all",
            }
        icon_map = {
            "metadados": "🏷️",
            "sinopse_abstract": "📝",
            "estrutura": "🧱",
            "tabelas_figuras": "📊",
            "referencias": "📚",
            "conformidade_estilos": "🎨",
            "gramatica_ortografia": "✍️",
        }
        for agent in AGENT_ORDER:
            label = AGENT_LABELS.get(agent, agent)
            if st.button(icon_map.get(agent, "⚙️"), key=f"icon_{agent}", help=f"Rodar: {label}", width="stretch"):
                st.session_state.pending_run = {
                    "question": f"Execute revisão focada em {label} e liste problemas com trecho e sugestão de correção.",
                    "agents": [agent],
                    "source": f"agent:{agent}",
                }
    else:
        if st.button("Rodar todos os agentes", width="stretch"):
            st.session_state.pending_run = {
                "question": "Faça uma revisão completa com todos os agentes e liste ajustes prioritários.",
                "agents": AGENT_ORDER.copy(),
                "source": "control:all",
            }

        st.markdown("### Execução direta por agente")
        for agent in AGENT_ORDER:
            label = AGENT_LABELS.get(agent, agent)
            if st.button(f"Rodar: {label}", key=f"run_{agent}", width="stretch"):
                st.session_state.pending_run = {
                    "question": f"Execute revisão focada em {label} e liste problemas com trecho e sugestão de correção.",
                    "agents": [agent],
                    "source": f"agent:{agent}",
                }

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

        def _push_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int) -> None:
            label = AGENT_LABELS.get(agent, agent)
            pct = int((batch_idx / max(batch_total, 1)) * 100)
            progress_header.info(
                f"Processando: {label} | lote {batch_idx}/{batch_total} | +{new_count} comentário(s), total {total}"
            )
            progress_bar.progress(pct, text=f"Progresso do documento: lote {batch_idx}/{batch_total}")
            line = f"- `{label}` lote {batch_idx}/{batch_total}: +{new_count} comentário(s), total {total}"
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

        def _push_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int) -> None:
            label = AGENT_LABELS.get(agent, agent)
            pct = int((batch_idx / max(batch_total, 1)) * 100)
            progress_header.info(
                f"Processando: {label} | lote {batch_idx}/{batch_total} | +{new_count} comentário(s), total {total}"
            )
            progress_bar.progress(pct, text=f"Progresso do documento: lote {batch_idx}/{batch_total}")
            line = f"- `{label}` lote {batch_idx}/{batch_total}: +{new_count} comentário(s), total {total}"
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
        st.subheader("Comentários dos agentes")
        st.dataframe(rows, width="stretch")

        if st.session_state.doc_path and st.session_state.doc_kind == "docx":
            output_bytes = apply_comments_to_docx(
                st.session_state.doc_path,
                [
                    AgentComment(
                        agent=r["agente"],
                        category=r["categoria"],
                        paragraph_index=r["indice_trecho"],
                        message=r["comentario"],
                        issue_excerpt=r["trecho_com_problema"],
                        suggested_fix=r["como_deve_ficar"],
                    )
                    for r in rows
                ],
            )
            st.download_button(
                label="Baixar DOCX comentado",
                data=output_bytes,
                file_name=f"{Path(st.session_state.doc_path).stem}_output.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        report = _build_correction_report(rows)
        st.download_button(
            label="Baixar relatório de correções (JSON)",
            data=json.dumps(report, ensure_ascii=False, indent=2),
            file_name=f"{Path(st.session_state.doc_path).stem if st.session_state.doc_path else 'correcoes'}.relatorio.json",
            mime="application/json",
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

        options = []
        for i, row in enumerate(rows):
            snippet = row["comentario"][:65].replace("\n", " ")
            options.append(f"{i+1}. {row['referencia']} | {row['categoria']} | {snippet}")

        if st.session_state.selected_comment_row >= len(options):
            st.session_state.selected_comment_row = 0

        nav_prev, nav_next, nav_info = st.columns([1, 1, 2])
        with nav_prev:
            if st.button("Anterior", key="fix_prev", disabled=st.session_state.selected_comment_row <= 0):
                st.session_state.selected_comment_row -= 1
        with nav_next:
            if st.button("Próximo", key="fix_next", disabled=st.session_state.selected_comment_row >= len(options) - 1):
                st.session_state.selected_comment_row += 1
        with nav_info:
            st.caption(f"Item {st.session_state.selected_comment_row + 1} de {len(options)}")

        selected_option = st.selectbox(
            "Selecionar item:",
            options,
            index=st.session_state.selected_comment_row,
            key="fix_select",
        )
        st.session_state.selected_comment_row = options.index(selected_option)

        idx = st.session_state.selected_comment_row
        row = rows[idx]
        state_key = str(idx)
        state = st.session_state.correction_state[state_key]

        resolved = sum(1 for v in st.session_state.correction_state.values() if v.get("status") == "resolvido")
        st.progress(resolved / len(rows), text=f"{resolved}/{len(rows)} itens resolvidos")

        st.markdown(f"**Agente:** `{AGENT_LABELS.get(row['agente'], row['agente'])}` | **Categoria:** `{row['categoria']}`")
        st.markdown(f"**Referência:** {row['referencia']}")
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

        st.text_area("Versão final (editável)", key=final_key, height=180)
        st.selectbox("Status", ["pendente", "em_revisao", "resolvido"], key=status_key)
        st.text_area("Observação do revisor", key=note_key, height=90)

        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("Usar sugestão", key="fix_use"):
                st.session_state[final_key] = row["como_deve_ficar"] or st.session_state[final_key]
        with b2:
            if st.button("Marcar resolvido", key="fix_done"):
                st.session_state[status_key] = "resolvido"
        with b3:
            if st.button("Reabrir", key="fix_reopen"):
                st.session_state[status_key] = "em_revisao"

        state["final_text"] = st.session_state.get(final_key, "")
        state["status"] = st.session_state.get(status_key, "pendente")
        state["observacao"] = st.session_state.get(note_key, "")

        pidx = row["indice_trecho"]
        st.markdown("### Contexto do documento")
        if isinstance(pidx, int) and 0 <= pidx < len(st.session_state.paragraphs):
            st.caption("Trecho-alvo")
            st.code(st.session_state.paragraphs[pidx], language="text")
            if pidx - 1 >= 0:
                st.caption("Trecho anterior")
                st.text(st.session_state.paragraphs[pidx - 1][:900])
            if pidx + 1 < len(st.session_state.paragraphs):
                st.caption("Trecho seguinte")
                st.text(st.session_state.paragraphs[pidx + 1][:900])
        else:
            st.caption("Sem índice de trecho para contexto.")
