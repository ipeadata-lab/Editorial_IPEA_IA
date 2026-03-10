from __future__ import annotations

import argparse
import json
from pathlib import Path

from .docx_utils import apply_comments_to_docx
from .document_loader import load_document
from .graph_chat import run_conversation
from .prompts import AGENT_ORDER


def main() -> int:
    parser = argparse.ArgumentParser(description="Executa revisão editorial em arquivo DOCX/PDF.")
    parser.add_argument("input", type=Path, help="Caminho do arquivo de entrada (.docx ou .pdf)")
    parser.add_argument(
        "--question",
        default="Faça uma revisão completa com todos os agentes e liste ajustes prioritários.",
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

    base = args.input.with_suffix("")
    output_json = args.output_json or base.parent / f"{base.name}_output.relatorio.json"
    output_json.write_text(
        json.dumps(
            [
                {
                    "agent": c.agent,
                    "category": c.category,
                    "message": c.message,
                    "paragraph_index": c.paragraph_index,
                    "issue_excerpt": c.issue_excerpt,
                    "suggested_fix": c.suggested_fix,
                }
                for c in result.comments
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if loaded.kind == "docx":
        output_docx = args.output_docx or base.parent / f"{base.name}_output.docx"
        output_docx.write_bytes(apply_comments_to_docx(args.input, result.comments))
        print(f"DOCX comentado: {output_docx}")

    print(f"Relatório JSON: {output_json}")
    print(f"Comentários gerados: {len(result.comments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())