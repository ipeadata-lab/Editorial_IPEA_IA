from __future__ import annotations

import os
import json
import inspect
import hashlib
import html
from queue import Empty, Queue
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable

import streamlit as st

from src.editorial_docx.config import build_output_paths
from src.editorial_docx.docx_utils import apply_comments_to_docx
from src.editorial_docx.document_loader import load_document, load_normalized_document
from src.editorial_docx.graph_chat import run_conversation
from src.editorial_docx.llm import get_llm_config, get_llm_model_tag, get_runtime_settings
from src.editorial_docx.models import AgentComment, ExecutionTrace, VerificationSummary, agent_short_label
from src.editorial_docx.prompts import AGENT_ORDER, detect_prompt_profile

st.set_page_config(page_title="Editorial TD - Agentes", layout="wide")
st.title("Revisão Editorial TD com Agentes")

AGENT_LABELS = {
    "metadados": "Metadados",
    "sinopse_abstract": "Sinopse/Abstract",
    "estrutura": "Estrutura",
    "tabelas_figuras": "Tabelas/Figuras",
    "comentarios_usuario_referencias": "Comentários do Usuário/Referências",
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
        key_env = "LLM_API_KEY"
        key_source = "variável de ambiente" if llm_config.get("api_key") else "não configurada"
        st.caption(f"Chave atual: {key_source}")
        if llm_config["provider"] != "ollama":
            api_key_input = st.text_input(
                key_env,
                type="password",
                help="A chave fica somente nesta sessão e não é salva em disco. OPENAI_API_KEY continua aceito como alias legado.",
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
    st.caption("Modo determinístico sempre ativo: seed fixa, até 3 agentes em paralelo e sem fallback automático.")

    if st.button("Rodar todos os agentes", key="sidebar_run_all", use_container_width=True):
        st.session_state.pending_run = {
            "question": "Faça uma revisão completa com todos os agentes ativos e liste ajustes.",
            "agents": AGENT_ORDER.copy(),
            "source": "control:all",
        }

    st.markdown("#### Execução direta por agente")
    for agent in AGENT_ORDER:
        label = AGENT_LABELS.get(agent, agent)
        if st.button(label, key=f"sidebar_run_{agent}", use_container_width=True):
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
    "user_comments": [],
    "normalized_json_text": "",
    "normalized_json_path": None,
    "source_name": "",
    "report_json_path": None,
    "diagnostics_json_path": None,
    "commented_docx_path": None,
    "selected_comment_row": 0,
    "correction_state": {},
    "comments_signature": "",
    "pending_run": None,
    "agent_result_cache": {},
    "agent_nav_idx": 0,
    "review_answer": "",
    "review_logs": [],
    "review_question": "",
    "review_trace": None,
    "review_verification": None,
    "session_temp_dir": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _session_work_dir() -> Path:
    """Returns an ephemeral directory for uploaded files in the current session."""
    temp_dir = st.session_state.session_temp_dir
    if temp_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="editorial_web_")
        st.session_state.session_temp_dir = temp_dir
    return Path(temp_dir.name)


def _build_rows() -> list[dict]:
    """Handles build rows."""
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
    """Handles merge comments."""
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
    """Handles signature."""
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
    """Handles ensure correction state."""
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
    """Handles build correction report."""
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


def _sync_correction_widget_state(rows: list[dict]) -> None:
    """Syncs Streamlit widget values back into the correction state."""
    for row in rows:
        key = str(row["comment_idx"])
        if key not in st.session_state.correction_state:
            continue
        note_key = f"review_note_{key}"
        user_text = ""
        if note_key in st.session_state:
            user_text = st.session_state[note_key]
            st.session_state.correction_state[key]["observacao"] = user_text
        final_text_key = f"final_text_{key}"
        if final_text_key in st.session_state:
            st.session_state.correction_state[key]["final_text"] = st.session_state[final_text_key]
        if (user_text or "").strip() and st.session_state.correction_state[key].get("status") != "rejeitado":
            st.session_state.correction_state[key]["final_text"] = user_text
            st.session_state.correction_state[key]["status"] = "resolvido"


def _build_export_comments(report_rows: list[dict]) -> list[AgentComment]:
    """Handles build export comments."""
    overrides: dict[int, dict] = {int(row["comment_idx"]): row for row in report_rows}
    export_comments: list[AgentComment] = []

    for idx, comment in enumerate(st.session_state.comments):
        override = overrides.get(idx)
        if override is None:
            export_comments.append(comment)
            continue
        if override.get("status") == "rejeitado":
            continue

        export_comments.append(
            AgentComment(
                agent=override["agent_key"],
                category=override["categoria"],
                paragraph_index=override["indice_trecho"],
                message=override["comentario"],
                issue_excerpt=override["trecho_com_problema"],
                suggested_fix=override.get("texto_final_aprovado") or override["como_deve_ficar"],
                auto_apply=override.get("auto_aplicar", False),
                format_spec=override.get("format_spec", ""),
                review_status=override.get("status", ""),
                approved_text=override.get("texto_final_aprovado", ""),
                reviewer_note=override.get("observacao", ""),
            )
        )
    return export_comments


def _serialize_trace(trace: ExecutionTrace | None) -> dict[str, object]:
    """Handles serialize trace."""
    if trace is None:
        return {"agents": []}
    return {
        "agents": [
            {
                "agent": agent.agent,
                "agent_label": AGENT_LABELS.get(agent.agent, agent.agent),
                "failed": agent.failed,
                "failure_status": agent.failure_status,
                "llm_raw_comment_count": agent.llm_raw_comment_count,
                "llm_post_review_comment_count": agent.llm_post_review_comment_count,
                "llm_validated_comment_count": agent.llm_validated_comment_count,
                "llm_rejected_comment_count": agent.llm_rejected_comment_count,
                "heuristic_accepted_comment_count": agent.heuristic_accepted_comment_count,
                "batches": [
                    {
                        "batch_index": batch.batch_index,
                        "total_batches": batch.total_batches,
                        "status": batch.status,
                        "llm_raw_comment_count": batch.llm_raw_comment_count,
                        "llm_post_review_comment_count": batch.llm_post_review_comment_count,
                        "llm_validated_comment_count": batch.llm_validated_comment_count,
                        "llm_rejected_comment_count": batch.llm_rejected_comment_count,
                        "heuristic_accepted_comment_count": batch.heuristic_accepted_comment_count,
                        "visible_comment_count": batch.visible_comment_count,
                    }
                    for batch in agent.batches
                ],
            }
            for agent in trace.agents
        ]
    }


def _serialize_diagnostic_comment(comment: AgentComment) -> dict[str, object]:
    """Serializes one comment payload for diagnostics exports."""
    return {
        "agent": AGENT_LABELS.get(comment.agent, comment.agent),
        "agent_key": comment.agent,
        "category": comment.category,
        "message": comment.message,
        "paragraph_index": comment.paragraph_index,
        "issue_excerpt": comment.issue_excerpt,
        "suggested_fix": comment.suggested_fix,
        "auto_apply": comment.auto_apply,
        "format_spec": comment.format_spec,
    }


def _serialize_verification(summary: VerificationSummary | None) -> dict[str, object]:
    """Serializes verification counts and per-comment decisions for diagnostics."""
    if summary is None:
        return {"accepted_count": 0, "rejected_count": 0, "decisions": []}
    return {
        "accepted_count": summary.accepted_count,
        "rejected_count": summary.rejected_count,
        "decisions": [
            {
                "accepted": decision.accepted,
                "reason": decision.reason,
                "source": decision.source,
                "batch_index": decision.batch_index,
                "comment": _serialize_diagnostic_comment(decision.comment),
            }
            for decision in summary.decisions
        ],
    }


def _focus_area_from_comment(comment: AgentComment) -> str:
    """Handles focus area from comment."""
    category = (comment.category or "").strip().lower()
    if comment.agent == "tipografia" and category in {
        "tipografia_titulo",
        "tipografia_titulos",
        "heading",
        "alinhamento_titulo",
    }:
        return "tipografia de títulos"
    if comment.agent == "tipografia":
        return "tipografia"
    if comment.agent == "referencias":
        return "referências"
    if comment.agent == "gramatica_ortografia":
        return "gramática e ortografia"
    if comment.agent == "tabelas_figuras":
        return "tabelas e figuras"
    if comment.agent == "estrutura":
        return "estrutura"
    if comment.agent == "sinopse_abstract":
        return "sinopse e abstract"
    return AGENT_LABELS.get(comment.agent, comment.agent).lower()


def _join_focus_areas(items: list[str]) -> str:
    """Handles join focus areas."""
    cleaned = [item for item in items if item]
    if not cleaned:
        return "ajustes editoriais diversos"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} e {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f" e {cleaned[-1]}"


def _build_diagnostic_headline(comments: list[AgentComment]) -> str:
    """Handles build diagnostic headline."""
    if not comments:
        return "Nenhum problema editorial relevante foi encontrado na última execução."

    counts: dict[str, int] = {}
    for comment in comments:
        label = _focus_area_from_comment(comment)
        counts[label] = counts.get(label, 0) + 1

    top_areas = [item[0] for item in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]]
    return f"Os problemas mais importantes deste documento estão concentrados em {_join_focus_areas(top_areas)}."


def _build_diagnostic_summary_text(answer: str, comments: list[AgentComment]) -> str:
    """Handles build diagnostic summary text."""
    headline = _build_diagnostic_headline(comments)
    clean_answer = (answer or "").strip()
    if not clean_answer:
        return headline
    return headline + "\n\n" + clean_answer


def _build_diagnostics_payload(export_comments: list[AgentComment]) -> dict[str, object]:
    """Handles build diagnostics payload."""
    runtime = get_runtime_settings()
    return {
        "source_name": st.session_state.source_name,
        "question": st.session_state.review_question,
        "answer": st.session_state.review_answer,
        "summary": _build_diagnostic_summary_text(st.session_state.review_answer, export_comments),
        "comment_count": len(export_comments),
        "provider": runtime["provider"],
        "model": runtime["model"],
        "runtime": runtime,
        "verification": _serialize_verification(st.session_state.review_verification),
        "trace": _serialize_trace(st.session_state.review_trace),
        "progress_logs": st.session_state.review_logs,
    }


def _persist_review_outputs(report_rows: list[dict], export_comments: list[AgentComment]) -> tuple[Path, str, Path | None, bytes | None]:
    """Handles persist review outputs."""
    source_for_outputs = (
        st.session_state.doc_path
        or st.session_state.normalized_json_path
        or (_session_work_dir() / st.session_state.source_name)
    )
    output_paths = build_output_paths(Path(source_for_outputs), llm_model_tag)

    report_json_path = output_paths["report_json"]
    report_json_text = json.dumps(report_rows, ensure_ascii=False, indent=2)

    docx_path: Path | None = None
    docx_bytes: bytes | None = None
    if st.session_state.doc_path and st.session_state.doc_kind == "docx":
        docx_bytes = apply_comments_to_docx(
            st.session_state.doc_path,
            export_comments,
        )
        docx_path = output_paths["docx"]

    st.session_state.report_json_path = report_json_path
    st.session_state.diagnostics_json_path = None
    st.session_state.commented_docx_path = docx_path
    return report_json_path, report_json_text, docx_path, docx_bytes


def _select_comment_row(index: int, visible_indexes: list[int], option_labels: list[str]) -> None:
    """Handles select comment row."""
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
    """Handles find next pending index."""
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


def _normalize_text_with_mapping(text: str) -> tuple[str, list[int]]:
    """Handles normalize text with mapping."""
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
    """Handles find excerpt span."""
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
    """Handles render target excerpt."""
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
    *,
    paragraphs: list[str],
    refs: list[str],
    sections: list[dict],
    user_comments: list[dict],
    profile_key: str,
    on_progress: Callable[[str, int, int, int, int, str], None] | None = None,
    event_queue=None,
) -> tuple[object, list[str]]:
    """Handles run review."""
    logs: list[str] = []
    batch_statuses: dict[tuple[str, int], str] = {}

    def on_agent_done(agent: str, new_count: int, total: int) -> None:
        """Handles the `on_agent_done` callback."""
        _ = (agent, new_count, total)

    def on_agent_batch_status(agent: str, batch_idx: int, batch_total: int, status: str) -> None:
        """Handles the `on_agent_batch_status` callback."""
        _ = batch_total
        batch_statuses[(agent, batch_idx)] = status

    def on_agent_progress(agent: str, batch_idx: int, batch_total: int, new_count: int, total: int) -> None:
        """Handles the `on_agent_progress` callback."""
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
        if event_queue is not None:
            event_queue.put(
                {
                    "type": "progress",
                    "agent": agent,
                    "batch_idx": batch_idx,
                    "batch_total": batch_total,
                    "new_count": new_count,
                    "total": total,
                    "status": status,
                    "line": line,
                }
            )
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
        paragraphs,
        refs,
        sections,
        question,
        user_comments=user_comments,
        profile_key=profile_key,
        **kwargs,
    )
    return result, logs


def _store_loaded_document(loaded, *, file_fingerprint: str | None, file_bytes: bytes = b"", doc_path: Path | None = None) -> None:
    """Handles store loaded document."""
    st.session_state.messages = []
    st.session_state.comments = []
    st.session_state.agent_result_cache = {}
    st.session_state.selected_comment_row = 0
    st.session_state.correction_state = {}
    st.session_state.comments_signature = ""
    st.session_state.doc_path = doc_path
    st.session_state.doc_bytes = file_bytes
    st.session_state.doc_fingerprint = file_fingerprint
    st.session_state.paragraphs = loaded.chunks
    st.session_state.refs = loaded.refs
    st.session_state.sections = loaded.sections
    st.session_state.toc = loaded.toc
    st.session_state.user_comments = loaded.user_comments
    st.session_state.doc_kind = loaded.kind
    st.session_state.normalized_json_text = loaded.normalized_document.to_json()
    st.session_state.normalized_json_path = (
        loaded.source_path if loaded.source_path.suffix.lower() == ".json" else None
    )
    st.session_state.source_name = (doc_path or loaded.source_path).stem
    st.session_state.report_json_path = None
    st.session_state.diagnostics_json_path = None
    st.session_state.commented_docx_path = None
    st.session_state.review_answer = ""
    st.session_state.review_logs = []
    st.session_state.review_question = ""
    st.session_state.review_trace = None
    st.session_state.review_verification = None


uploaded = st.file_uploader("Carregar documento (.docx ou .pdf)", type=["docx", "pdf"])
uploaded_normalized = st.file_uploader("Carregar normalized_document.json", type=["json"])

if uploaded is not None:
    file_bytes = uploaded.getvalue()
    file_fingerprint = hashlib.sha256(file_bytes).hexdigest()
    profile = detect_prompt_profile(uploaded.name)
    st.session_state.doc_profile = profile.key

    if st.session_state.doc_fingerprint != file_fingerprint:
        doc_path = _session_work_dir() / uploaded.name
        doc_path.write_bytes(file_bytes)

        loaded = load_document(doc_path)
        _store_loaded_document(loaded, file_fingerprint=file_fingerprint, file_bytes=file_bytes, doc_path=doc_path)

elif uploaded_normalized is not None:
    file_bytes = uploaded_normalized.getvalue()
    file_fingerprint = hashlib.sha256(file_bytes).hexdigest()
    if st.session_state.doc_fingerprint != file_fingerprint:
        normalized_path = _session_work_dir() / uploaded_normalized.name
        normalized_path.write_bytes(file_bytes)
        loaded = load_normalized_document(normalized_path)
        st.session_state.doc_profile = "GENERIC"
        _store_loaded_document(loaded, file_fingerprint=file_fingerprint, file_bytes=b"", doc_path=None)
        st.session_state.normalized_json_path = normalized_path

col_diag, col_comments = st.columns([1.05, 1.35], gap="large")

if st.session_state.pending_run and st.session_state.paragraphs:
    run = st.session_state.pending_run
    st.session_state.pending_run = None
    review_context = {
        "paragraphs": list(st.session_state.paragraphs),
        "refs": list(st.session_state.refs),
        "sections": list(st.session_state.sections),
        "user_comments": list(st.session_state.user_comments),
        "profile_key": st.session_state.doc_profile,
    }
    progress_header = st.empty()
    progress_bar = st.progress(0, text="Preparando execução dos agentes...")
    agent_progress_host = st.empty()
    progress_box = st.empty()
    progress_lines: list[str] = []
    with agent_progress_host.container():
        agent_slots = {agent: st.empty() for agent in run["agents"]}
    agent_state = {
        agent: {
            "batch_idx": 0,
            "batch_total": 0,
            "new_count": 0,
            "comments_total": 0,
            "status": "aguardando",
            "done": False,
        }
        for agent in run["agents"]
    }

    def _render_parallel_progress() -> None:
        """Handles render parallel progress."""
        total_agents = max(len(run["agents"]), 1)
        accumulated_ratio = 0.0
        completed_agents = 0

        for agent in run["agents"]:
            state = agent_state[agent]
            if state["done"]:
                completed_agents += 1
            if state["batch_total"] > 0:
                accumulated_ratio += min(state["batch_idx"] / max(state["batch_total"], 1), 1.0)
            elif state["done"]:
                accumulated_ratio += 1.0

            label = AGENT_LABELS.get(agent, agent)
            if state["batch_total"] > 0:
                pct = int(min(state["batch_idx"] / max(state["batch_total"], 1), 1.0) * 100)
                subtitle = (
                    f"Lote {state['batch_idx']}/{state['batch_total']} | "
                    f"+{state['new_count']} comentário(s) | total local {state['comments_total']}"
                )
            else:
                pct = 100 if state["done"] else 0
                subtitle = f"Status: {state['status']}"

            status = state["status"]
            suffix = f" | {status}" if status else ""
            with agent_slots[agent].container():
                st.markdown(f"**{label}**")
                st.progress(pct, text=f"{subtitle}{suffix}")

        overall_pct = int((accumulated_ratio / total_agents) * 100)
        progress_header.info(f"Executando revisão: {completed_agents}/{total_agents} agente(s) concluído(s).")
        progress_bar.progress(overall_pct, text=f"Progresso geral: {overall_pct}%")
        if progress_lines:
            progress_box.markdown("**Progresso da revisão:**\n" + "\n".join(progress_lines[-14:]))

    event_queue = Queue()
    outcome: dict[str, object] = {"done": False}

    def _review_worker() -> None:
        """Handles review worker."""
        try:
            result, logs = _run_review(
                run["question"],
                run["agents"],
                paragraphs=review_context["paragraphs"],
                refs=review_context["refs"],
                sections=review_context["sections"],
                user_comments=review_context["user_comments"],
                profile_key=review_context["profile_key"],
                event_queue=event_queue,
            )
            outcome["result"] = result
            outcome["logs"] = logs
        except Exception as exc:  # pragma: no cover - surface in UI path
            outcome["error"] = exc
        finally:
            outcome["done"] = True

    worker = threading.Thread(target=_review_worker, name="streamlit-review", daemon=True)
    worker.start()

    try:
        with st.spinner("Executando agentes..."):
            while worker.is_alive() or not event_queue.empty():
                drained = False
                while True:
                    try:
                        event = event_queue.get_nowait()
                    except Empty:
                        break

                    drained = True
                    if event.get("type") != "progress":
                        continue
                    agent = str(event["agent"])
                    state = agent_state.setdefault(
                        agent,
                        {
                            "batch_idx": 0,
                            "batch_total": 0,
                            "new_count": 0,
                            "comments_total": 0,
                            "status": "aguardando",
                            "done": False,
                        },
                    )
                    state["batch_idx"] = int(event["batch_idx"])
                    state["batch_total"] = int(event["batch_total"])
                    state["new_count"] = int(event["new_count"])
                    state["comments_total"] += int(event["new_count"])
                    state["status"] = str(event.get("status") or "em execução")
                    state["done"] = state["batch_total"] > 0 and state["batch_idx"] >= state["batch_total"]
                    progress_lines.append(str(event["line"]))
                if drained:
                    _render_parallel_progress()
                time.sleep(0.05)
            worker.join()
            _render_parallel_progress()
            if "error" in outcome:
                raise outcome["error"]
            result = outcome["result"]
            logs = outcome.get("logs", [])
    except Exception as exc:
        progress_header.error("A revisão foi interrompida por uma falha inesperada.")
        progress_bar.empty()
        agent_progress_host.empty()
        progress_box.empty()
        st.error(f"Falha ao executar a revisão: {exc}")
        st.caption("Verifique a configuração da LLM, especialmente provider, base URL e modelo.")
    else:
        if getattr(result, "trace", None) is not None:
            for agent_trace in result.trace.agents:
                state = agent_state.setdefault(
                    agent_trace.agent,
                    {
                        "batch_idx": 0,
                        "batch_total": 0,
                        "new_count": 0,
                        "comments_total": 0,
                        "status": "aguardando",
                        "done": False,
                    },
                )
                total_batches = len(agent_trace.batches)
                state["batch_total"] = max(state["batch_total"], total_batches)
                state["batch_idx"] = total_batches if total_batches else state["batch_idx"]
                state["status"] = agent_trace.failure_status or "concluído"
                state["done"] = True
        _render_parallel_progress()
        progress_header.success("Revisão concluída.")
        progress_bar.progress(100, text="Processamento completo")

        st.session_state.review_question = run["question"]
        st.session_state.review_answer = result.answer
        st.session_state.review_logs = logs
        st.session_state.review_trace = result.trace
        st.session_state.review_verification = result.verification
        st.session_state.comments = _merge_comments(st.session_state.comments, result.comments)

        if len(run["agents"]) == 1:
            agent = run["agents"][0]
            st.session_state.agent_result_cache[agent] = {
                "answer": result.answer,
                "comments": result.comments,
                "question": run["question"],
                "trace": result.trace,
                "verification": result.verification,
            }
        st.rerun()
elif st.session_state.pending_run and not st.session_state.paragraphs:
    st.warning("Carregue um documento antes de executar os agentes.")
    st.session_state.pending_run = None

rows = _build_rows()
report_json_path = None
report_json_text = None
diagnostics_json_name = None
diagnostics_json_text = None
docx_path = None
docx_bytes = None

if rows:
    _ensure_correction_state(rows)
    _sync_correction_widget_state(rows)
    report = _build_correction_report(rows)
    export_comments = _build_export_comments(report)
    report_json_path, report_json_text, docx_path, docx_bytes = _persist_review_outputs(report, export_comments)
    diagnostics_json_name = f"{report_json_path.stem}.diagnostics.json"
    diagnostics_json_text = json.dumps(_build_diagnostics_payload(export_comments), ensure_ascii=False, indent=2)
elif st.session_state.comments:
    report = _build_correction_report(_build_rows())
    export_comments = st.session_state.comments
    report_json_path, report_json_text, docx_path, docx_bytes = _persist_review_outputs(report, export_comments)
    diagnostics_json_name = f"{report_json_path.stem}.diagnostics.json"
    diagnostics_json_text = json.dumps(_build_diagnostics_payload(export_comments), ensure_ascii=False, indent=2)

with col_diag:
    st.subheader("Diagnóstico")

    if not st.session_state.paragraphs:
        st.info("Carregue um documento e use os botões da barra lateral para rodar os agentes.")
    elif not st.session_state.comments:
        st.info("Documento carregado. Rode os agentes na barra lateral para gerar o diagnóstico editorial.")
    else:
        st.metric("Comentários gerados", len(st.session_state.comments))

        if docx_path and docx_bytes is not None:
            st.download_button(
                label="Baixar DOCX comentado",
                data=docx_bytes,
                file_name=docx_path.name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )

        if report_json_path:
            st.download_button(
                label="Baixar relatório JSON",
                data=report_json_text or Path(report_json_path).read_text(encoding="utf-8"),
                file_name=Path(report_json_path).name,
                mime="application/json",
                use_container_width=True,
            )

with col_comments:
    st.subheader("Erros Encontrados")

    if not rows:
        st.info("Os comentários dos agentes vão aparecer aqui depois da execução.")
    else:
        agent_options = sorted({row["agente"] for row in rows})
        category_options = sorted({row["categoria"] for row in rows})
        filter_a, filter_b = st.columns(2)
        with filter_a:
            selected_agents = st.multiselect(
                "Filtrar por agente",
                options=agent_options,
                default=[],
                key="diagnostic_agent_filter",
            )
        with filter_b:
            selected_categories = st.multiselect(
                "Filtrar por categoria",
                options=category_options,
                default=[],
                key="diagnostic_category_filter",
            )

        visible_rows = [
            row
            for row in rows
            if (not selected_agents or row["agente"] in selected_agents)
            and (not selected_categories or row["categoria"] in selected_categories)
        ]

        summary_a, summary_b, summary_c = st.columns(3)
        summary_a.metric("Itens filtrados", len(visible_rows))
        summary_b.metric("Agentes no filtro", len({row["agente"] for row in visible_rows}) if visible_rows else 0)
        summary_c.metric("Categorias no filtro", len({row["categoria"] for row in visible_rows}) if visible_rows else 0)

        if not visible_rows:
            st.info("Nenhum comentário corresponde aos filtros atuais.")
        else:
            for row in visible_rows:
                title = (
                    f"[{row['agente']}] {row['categoria']}"
                    + (f" | trecho {row['indice_trecho']}" if isinstance(row["indice_trecho"], int) else "")
                )
                with st.expander(title, expanded=False):
                    state_key = str(row["comment_idx"])
                    state = st.session_state.correction_state.setdefault(
                        state_key,
                        {
                            "status": "pendente",
                            "final_text": row["como_deve_ficar"] or row["trecho_com_problema"] or "",
                            "observacao": "",
                        },
                    )
                    status_label = {
                        "pendente": "Pendente",
                        "resolvido": "Aceito",
                        "rejeitado": "Rejeitado",
                    }.get(state.get("status", "pendente"), state.get("status", "pendente"))
                    st.caption(f"Status: {status_label}")

                    st.markdown(f"**Referência:** {row['referencia']}")
                    st.markdown(f"**Comentário:** {row['comentario']}")
                    st.markdown("**Trecho com problema**")
                    st.code(row["trecho_com_problema"] or "(não informado)", language="text")
                    st.markdown("**Sugestão de correção**")
                    st.code(row["como_deve_ficar"] or "(não informado)", language="text")

                    final_text_key = f"final_text_{state_key}"
                    if final_text_key not in st.session_state:
                        st.session_state[final_text_key] = state.get("final_text", "")
                    final_text = st.text_area(
                        "Correção que irá para o documento",
                        key=final_text_key,
                        placeholder="Edite aqui o texto final a aplicar no documento.",
                    )
                    state["final_text"] = final_text

                    note_key = f"review_note_{state_key}"
                    if note_key not in st.session_state:
                        st.session_state[note_key] = state.get("observacao", "")
                    note = st.text_area(
                        "Comentário/correção do usuário",
                        key=note_key,
                        placeholder="Escreva aqui a correção que deve ser aplicada no documento.",
                    )
                    state["observacao"] = note
                    if note.strip() and state.get("status") != "rejeitado":
                        state["final_text"] = note
                        state["status"] = "resolvido"

                    action_accept, action_reject = st.columns(2)
                    with action_accept:
                        if st.button("Aceitar comentário", key=f"accept_comment_{state_key}", use_container_width=True):
                            state["status"] = "resolvido"
                            state["final_text"] = note.strip() or final_text or row["como_deve_ficar"] or row["trecho_com_problema"] or ""
                            state["observacao"] = note
                            st.rerun()
                    with action_reject:
                        if st.button("Rejeitar comentário", key=f"reject_comment_{state_key}", use_container_width=True):
                            state["status"] = "rejeitado"
                            state["final_text"] = ""
                            state["observacao"] = note
                            st.rerun()

                    pidx = row["indice_trecho"]
                    if isinstance(pidx, int) and 0 <= pidx < len(st.session_state.paragraphs):
                        _render_target_excerpt(st.session_state.paragraphs[pidx], row["trecho_com_problema"])
