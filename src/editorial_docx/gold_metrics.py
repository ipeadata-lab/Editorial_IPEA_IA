from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


VALID_GOLD_SUFFIXES = ("gold_", "seed_")


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0


def _empty_counter() -> dict[str, float | int | None]:
    return {
        "VP": 0,
        "VP_parcial": 0,
        "FP": 0,
        "FN": 0,
        "VN": None,
    }


def _accumulate_annotation_metrics(counter: dict[str, float | int | None], annotation: dict[str, object]) -> None:
    label = str(annotation.get("label") or "").strip().lower()
    if label == "correto":
        counter["VP"] = int(counter["VP"]) + 1
    elif label == "parcial":
        counter["VP_parcial"] = int(counter["VP_parcial"]) + 1
    elif label == "incorreto":
        counter["FP"] = int(counter["FP"]) + 1


def _accumulate_missed_issue_metrics(counter: dict[str, float | int | None], missed_issue: dict[str, object]) -> None:
    label = str(missed_issue.get("label") or "").strip().lower()
    if label == "faltou":
        counter["FN"] = int(counter["FN"]) + 1


def _finalize_metrics(counter: dict[str, float | int | None], partial_weight: float) -> dict[str, float | int | None]:
    vp = int(counter["VP"])
    vp_parcial = int(counter["VP_parcial"])
    fp = int(counter["FP"])
    fn = int(counter["FN"])
    weighted_vp = vp + (vp_parcial * partial_weight)

    precision = _safe_div(vp + vp_parcial, vp + vp_parcial + fp)
    recall = _safe_div(vp + vp_parcial, vp + vp_parcial + fn)
    weighted_precision = _safe_div(weighted_vp, weighted_vp + fp)
    weighted_recall = _safe_div(weighted_vp, weighted_vp + fn)

    return {
        "VP": vp,
        "VP_parcial": vp_parcial,
        "FP": fp,
        "FN": fn,
        "VN": counter["VN"],
        "precisao": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "precisao_ponderada": weighted_precision,
        "recall_ponderado": weighted_recall,
        "f1_ponderado": _f1(weighted_precision, weighted_recall),
        "qualidade_respostas": weighted_precision,
    }


def _load_gold_files(paths: list[Path]) -> list[dict[str, object]]:
    datasets: list[dict[str, object]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Arquivo inválido para dataset ouro: {path}")
        datasets.append(data)
    return datasets


def _discover_gold_files(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        if item.is_file():
            files.append(item)
            continue
        if item.is_dir():
            for candidate in sorted(item.glob("*.json")):
                if candidate.name.startswith(VALID_GOLD_SUFFIXES):
                    files.append(candidate)
    unique = []
    seen: set[Path] = set()
    for path in files:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def compute_gold_metrics(datasets: list[dict[str, object]], partial_weight: float = 0.5) -> dict[str, object]:
    overall = _empty_counter()
    by_model: dict[str, dict[str, float | int | None]] = defaultdict(_empty_counter)
    by_agent: dict[str, dict[str, float | int | None]] = defaultdict(_empty_counter)
    by_model_agent: dict[str, dict[str, dict[str, float | int | None]]] = defaultdict(lambda: defaultdict(_empty_counter))

    for dataset in datasets:
        document = dataset.get("document") or {}
        model_name = str((document or {}).get("model_name") or "desconhecido")

        for annotation in dataset.get("annotations", []):
            if not isinstance(annotation, dict):
                continue
            agent = str(annotation.get("agent") or "desconhecido")
            _accumulate_annotation_metrics(overall, annotation)
            _accumulate_annotation_metrics(by_model[model_name], annotation)
            _accumulate_annotation_metrics(by_agent[agent], annotation)
            _accumulate_annotation_metrics(by_model_agent[model_name][agent], annotation)

        for missed_issue in dataset.get("missed_issues", []):
            if not isinstance(missed_issue, dict):
                continue
            if str(missed_issue.get("label") or "").strip().lower() != "faltou":
                continue
            agent = str(missed_issue.get("agent") or "desconhecido")
            _accumulate_missed_issue_metrics(overall, missed_issue)
            _accumulate_missed_issue_metrics(by_model[model_name], missed_issue)
            _accumulate_missed_issue_metrics(by_agent[agent], missed_issue)
            _accumulate_missed_issue_metrics(by_model_agent[model_name][agent], missed_issue)

    return {
        "metadata": {
            "datasets": len(datasets),
            "partial_weight": partial_weight,
            "note": "VN não é observável neste esquema atual de dataset ouro e permanece nulo até existir anotação explícita de negativos verdadeiros.",
        },
        "overall": _finalize_metrics(overall, partial_weight),
        "by_model": {model: _finalize_metrics(counter, partial_weight) for model, counter in sorted(by_model.items())},
        "by_agent": {agent: _finalize_metrics(counter, partial_weight) for agent, counter in sorted(by_agent.items())},
        "by_model_agent": {
            model: {agent: _finalize_metrics(counter, partial_weight) for agent, counter in sorted(agent_map.items())}
            for model, agent_map in sorted(by_model_agent.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolida métricas reais a partir de arquivos do dataset ouro.")
    parser.add_argument("inputs", nargs="+", type=Path, help="Arquivos JSON do dataset ouro ou diretórios contendo `gold_*.json`/`seed_*.json`.")
    parser.add_argument("--output", type=Path, required=True, help="Caminho do JSON consolidado de métricas.")
    parser.add_argument("--partial-weight", type=float, default=0.5, help="Peso usado para comentários rotulados como `parcial`.")
    args = parser.parse_args()

    files = _discover_gold_files(args.inputs)
    if not files:
        raise SystemExit("Nenhum arquivo de dataset ouro encontrado.")

    datasets = _load_gold_files(files)
    metrics = compute_gold_metrics(datasets, partial_weight=args.partial_weight)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
