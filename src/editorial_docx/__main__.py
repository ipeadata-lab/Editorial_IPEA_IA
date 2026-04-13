from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

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
    args = parser.parse_args()

    loaded = load_document(args.input)
    result = run_conversation(
        paragraphs=loaded.chunks,
        refs=loaded.refs,
        sections=loaded.sections,
        question=args.question,
        selected_agents=AGENT_ORDER.copy(),
    )
    visible_comments = result.comments[:]

    base = args.input.with_suffix("")
    model_tag = get_llm_model_tag()
    output_json = args.output_json or base.parent / f"{base.name}_output_{model_tag}.relatorio.json"
    json_text = json.dumps(
        [_serialize_comment(c) for c in visible_comments],
        ensure_ascii=False,
        indent=2,
    )
    output_json.write_text(json_text, encoding="utf-8")
    history_json = _write_history_snapshot(output_json, json_text)

    if loaded.kind == "docx":
        output_docx = args.output_docx or base.parent / f"{base.name}_output_{model_tag}.docx"
        docx_bytes = apply_comments_to_docx(args.input, result.comments)
        output_docx.write_bytes(docx_bytes)
        history_docx = _write_history_snapshot(output_docx, docx_bytes)
        print(f"DOCX comentado: {output_docx}")
        print(f"Histórico DOCX: {history_docx}")

    print(f"Relatório JSON: {output_json}")
    print(f"Histórico JSON: {history_json}")
    print(f"Comentários visíveis: {len(visible_comments)}")
    print(
        "Camada verificadora: "
        f"{result.verification.accepted_count} aceitos, {result.verification.rejected_count} rejeitados"
    )
    if (result.answer or "").startswith("Resumo parcial"):
        print(result.answer.splitlines()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
