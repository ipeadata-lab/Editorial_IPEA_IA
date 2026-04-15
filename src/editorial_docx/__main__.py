from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .config import build_output_paths, ensure_runtime_directories, resolve_input_path
from .docx_utils import apply_comments_to_docx
from .document_loader import load_document
from .graph_chat import run_conversation
from .llm import get_llm_model_tag
from .models import agent_short_label
from .prompts import AGENT_ORDER


def _serialize_comment(comment) -> dict[str, object]:
    return {
        "agent": agent_short_label(comment.agent),
        "category": comment.category,
        "message": comment.message,
        "paragraph_index": comment.paragraph_index,
        "issue_excerpt": comment.issue_excerpt,
        "suggested_fix": comment.suggested_fix,
    }


def _serialize_trace(trace) -> dict[str, object]:
    return {
        "agents": [
            {
                "agent": agent.agent,
                "agent_label": agent_short_label(agent.agent),
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


def _history_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_history_snapshot(main_path: Path, content: str | bytes) -> Path:
    history_dir = main_path.parent / "historico"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"{main_path.stem}__{_history_stamp()}{main_path.suffix}"
    if isinstance(content, bytes):
        history_path.write_bytes(content)
    else:
        history_path.write_text(content, encoding="utf-8")
    return history_path


def main() -> int:
    ensure_runtime_directories()

    parser = argparse.ArgumentParser(description="Executa revisão editorial em arquivo DOCX/PDF.")
    parser.add_argument("input", type=Path, help="Caminho do arquivo de entrada (.docx ou .pdf)")
    parser.add_argument(
        "--question",
        default="Faça uma revisão completa com todos os agentes ativos e liste ajustes prioritários.",
        help="Pergunta/instrução para os agentes.",
    )
    parser.add_argument(
        "--output-docx",
        type=Path,
        default=None,
        help="Caminho do DOCX de saída comentado (padrão: <entrada>_output.docx).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Caminho do relatório JSON (padrão: <entrada>_output.relatorio.json).",
    )
    parser.add_argument(
        "--output-normalized-json",
        type=Path,
        default=None,
        help="Caminho do artefato normalized_document.json (padrão: <entrada>_normalized_document.json).",
    )
    args = parser.parse_args()

    input_path = resolve_input_path(args.input)
    loaded = load_document(input_path)
    model_tag = get_llm_model_tag()
    output_paths = build_output_paths(input_path, model_tag)

    output_normalized_json = args.output_normalized_json or output_paths["normalized_json"]
    normalized_text = loaded.normalized_document.to_json()
    output_normalized_json.write_text(normalized_text, encoding="utf-8")
    history_normalized = _write_history_snapshot(output_normalized_json, normalized_text)
    result = run_conversation(
        paragraphs=loaded.chunks,
        refs=loaded.refs,
        sections=loaded.sections,
        user_comments=loaded.user_comments,
        question=args.question,
        selected_agents=AGENT_ORDER.copy(),
    )
    visible_comments = result.comments[:]

    output_json = args.output_json or output_paths["report_json"]
    output_diagnostics_json = output_paths["diagnostics_json"] if args.output_json is None else output_json.with_name(
        f"{output_json.stem}.diagnostics.json"
    )
    json_text = json.dumps(
        [_serialize_comment(c) for c in visible_comments],
        ensure_ascii=False,
        indent=2,
    )
    output_json.write_text(json_text, encoding="utf-8")
    history_json = _write_history_snapshot(output_json, json_text)
    diagnostics_text = json.dumps(_serialize_trace(result.trace), ensure_ascii=False, indent=2)
    output_diagnostics_json.write_text(diagnostics_text, encoding="utf-8")
    history_diagnostics = _write_history_snapshot(output_diagnostics_json, diagnostics_text)

    if loaded.kind == "docx":
        output_docx = args.output_docx or output_paths["docx"]
        docx_bytes = apply_comments_to_docx(input_path, result.comments)
        output_docx.write_bytes(docx_bytes)
        history_docx = _write_history_snapshot(output_docx, docx_bytes)
        print(f"DOCX comentado: {output_docx}")
        print(f"Histórico DOCX: {history_docx}")

    print(f"Relatório JSON: {output_json}")
    print(f"Histórico JSON: {history_json}")
    print(f"Diagnóstico JSON: {output_diagnostics_json}")
    print(f"Histórico diagnóstico JSON: {history_diagnostics}")
    print(f"Normalized JSON: {output_normalized_json}")
    print(f"Histórico normalized JSON: {history_normalized}")
    print(f"Comentários visíveis: {len(visible_comments)}")
    print(
        "Camada verificadora: "
        f"{result.verification.accepted_count} aceitos, {result.verification.rejected_count} rejeitados"
    )
    if (result.answer or "").startswith("Resumo parcial") or "Avisos de execução:" in (result.answer or ""):
        print(result.answer.splitlines()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
