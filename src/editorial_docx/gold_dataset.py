from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


VALID_COMMENT_LABELS = ["correto", "parcial", "incorreto"]
VALID_SEVERITY_LABELS = ["alta", "media", "baixa"]
VALID_MISSED_LABELS = ["faltou"]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    return slug.strip("_") or "item"


def _annotation_id(agent: str, ordinal: int) -> str:
    return f"{_slugify(agent)}_{ordinal:04d}"


def build_gold_annotation_template(
    report_items: list[dict[str, object]],
    *,
    source_document: str = "",
    report_path: str = "",
    model_name: str = "",
    run_label: str = "",
) -> dict[str, object]:
    by_agent = Counter(str(item.get("agent") or "") for item in report_items)
    annotations: list[dict[str, object]] = []

    for ordinal, item in enumerate(report_items, start=1):
        annotations.append(
            {
                "id": _annotation_id(str(item.get("agent") or "comentario"), ordinal),
                "agent": item.get("agent") or "",
                "category": item.get("category") or "",
                "paragraph_index": item.get("paragraph_index"),
                "issue_excerpt": item.get("issue_excerpt") or "",
                "suggested_fix": item.get("suggested_fix") or "",
                "model_comment": item.get("message") or "",
                "label": "",
                "severity": "",
                "reviewer_note": "",
                "source": {
                    "document": source_document,
                    "report_path": report_path,
                    "model_name": model_name,
                    "run_label": run_label,
                },
            }
        )

    return {
        "dataset_version": "1.0",
        "label_taxonomy": {
            "comment_labels": VALID_COMMENT_LABELS,
            "severity_labels": VALID_SEVERITY_LABELS,
            "missed_issue_labels": VALID_MISSED_LABELS,
        },
        "document": {
            "source_document": source_document,
            "report_path": report_path,
            "model_name": model_name,
            "run_label": run_label,
        },
        "summary": {
            "total_model_comments": len(report_items),
            "by_agent": dict(by_agent),
        },
        "annotations": annotations,
        "missed_issues": [
            {
                "id": "faltou_0001",
                "agent": "",
                "paragraph_index": None,
                "issue_excerpt": "",
                "expected_fix": "",
                "label": "faltou",
                "severity": "",
                "reviewer_note": "",
            }
        ],
    }


def build_gold_annotation_template_from_report(
    report_path: Path,
    *,
    source_document: str = "",
    model_name: str = "",
    run_label: str = "",
) -> dict[str, object]:
    items = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("O relatório deve ser uma lista JSON de comentários.")
    return build_gold_annotation_template(
        items,
        source_document=source_document,
        report_path=str(report_path),
        model_name=model_name,
        run_label=run_label,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera um scaffold de anotação para dataset ouro a partir de um relatório JSON.")
    parser.add_argument("report_json", type=Path, help="Caminho do relatório JSON de comentários aceitos.")
    parser.add_argument("--output", type=Path, required=True, help="Caminho do JSON de saída do scaffold ouro.")
    parser.add_argument("--source-document", default="", help="Caminho ou identificador do documento original.")
    parser.add_argument("--model-name", default="", help="Nome do modelo usado na rodada.")
    parser.add_argument("--run-label", default="", help="Rótulo livre da rodada de avaliação.")
    args = parser.parse_args()

    dataset = build_gold_annotation_template_from_report(
        args.report_json,
        source_document=args.source_document,
        model_name=args.model_name,
        run_label=args.run_label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
