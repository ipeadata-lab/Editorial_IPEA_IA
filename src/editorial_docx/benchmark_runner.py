from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .document_loader import load_document
from .gold_metrics import compute_gold_metrics
from .graph_chat import run_conversation
from .llm import get_llm_model_tag
from .prompts import AGENT_ORDER


@dataclass(slots=True)
class BenchmarkRunResult:
    document: str
    report_json: str
    normalized_json: str
    visible_comments: int
    accepted_comments: int
    rejected_comments: int


def discover_rais_documents(search_root: Path) -> list[Path]:
    """Descobre documentos RAIS no repositório para benchmark fixo."""
    candidates = sorted(
        path
        for path in search_root.rglob("*RAIS*.docx")
        if "_output_" not in path.name and "historico" not in path.parts
    )
    return candidates


def run_benchmark_document(input_path: Path, output_dir: Path) -> BenchmarkRunResult:
    """Executa uma rodada reproduzível de review e guarda seus artefatos."""
    loaded = load_document(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = input_path.with_suffix("").name
    model_tag = get_llm_model_tag()

    normalized_path = output_dir / f"{base_name}_normalized_document.json"
    normalized_path.write_text(loaded.normalized_document.to_json(), encoding="utf-8")

    result = run_conversation(
        paragraphs=loaded.chunks,
        refs=loaded.refs,
        sections=loaded.sections,
        question="Faça uma revisão completa com todos os agentes ativos e liste ajustes prioritários.",
        selected_agents=AGENT_ORDER.copy(),
        user_comments=loaded.user_comments,
    )
    report_path = output_dir / f"{base_name}_output_{model_tag}.relatorio.json"
    report_path.write_text(
        json.dumps(
            [
                {
                    "agent": item.agent,
                    "category": item.category,
                    "message": item.message,
                    "paragraph_index": item.paragraph_index,
                    "issue_excerpt": item.issue_excerpt,
                    "suggested_fix": item.suggested_fix,
                }
                for item in result.comments
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return BenchmarkRunResult(
        document=str(input_path),
        report_json=str(report_path),
        normalized_json=str(normalized_path),
        visible_comments=len(result.comments),
        accepted_comments=result.verification.accepted_count,
        rejected_comments=result.verification.rejected_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Executa benchmark reproduzível do pipeline editorial.")
    parser.add_argument("inputs", nargs="*", type=Path, help="Documentos DOCX/PDF a serem avaliados.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Diretório onde os artefatos serão salvos.")
    parser.add_argument(
        "--gold",
        nargs="*",
        type=Path,
        default=[],
        help="Arquivos do dataset ouro para consolidar métricas após a rodada.",
    )
    parser.add_argument(
        "--preset",
        choices=["rais"],
        default=None,
        help="Executa um conjunto fixo de benchmark já conhecido pelo projeto.",
    )
    args = parser.parse_args()

    if args.preset == "rais":
        project_root = Path(__file__).resolve().parents[2]
        inputs = discover_rais_documents(project_root / "testes")
        output_dir = args.output_dir or (project_root / "testes" / "benchmarks" / "rais")
    else:
        inputs = args.inputs
        output_dir = args.output_dir

    if not inputs:
        raise SystemExit("Nenhum documento informado para benchmark.")
    if output_dir is None:
        raise SystemExit("Informe --output-dir ou use um preset com diretório padrão.")

    results = [run_benchmark_document(input_path, output_dir) for input_path in inputs]
    manifest = {
        "preset": args.preset or "",
        "runs": [asdict(item) for item in results],
    }

    if args.gold:
        datasets = [json.loads(path.read_text(encoding="utf-8")) for path in args.gold]
        manifest["gold_metrics"] = compute_gold_metrics(datasets)

    manifest_path = output_dir / "benchmark_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
