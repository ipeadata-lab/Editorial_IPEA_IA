from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from editorial_docx.gold_metrics import compute_gold_metrics


def test_compute_gold_metrics_aggregates_models_agents_and_partials():
    datasets = [
        {
            "document": {"model_name": "gpt-4o"},
            "annotations": [
                {"agent": "gram", "label": "correto"},
                {"agent": "gram", "label": "incorreto"},
                {"agent": "sin", "label": "parcial"},
            ],
            "missed_issues": [
                {"agent": "gram", "label": "faltou"},
                {"agent": "ref", "label": "faltou"},
            ],
        },
        {
            "document": {"model_name": "gpt-5.2"},
            "annotations": [
                {"agent": "gram", "label": "correto"},
                {"agent": "ref", "label": "correto"},
            ],
            "missed_issues": [
                {"agent": "sin", "label": "faltou"},
            ],
        },
    ]

    metrics = compute_gold_metrics(datasets, partial_weight=0.5)

    assert metrics["overall"]["VP"] == 3
    assert metrics["overall"]["VP_parcial"] == 1
    assert metrics["overall"]["FP"] == 1
    assert metrics["overall"]["FN"] == 3
    assert metrics["overall"]["VN"] is None
    assert round(metrics["overall"]["precisao"], 4) == 0.8
    assert round(metrics["overall"]["recall"], 4) == 0.5714
    assert round(metrics["overall"]["precisao_ponderada"], 4) == 0.7778
    assert round(metrics["overall"]["recall_ponderado"], 4) == 0.5385
    assert metrics["by_model"]["gpt-4o"]["FP"] == 1
    assert metrics["by_model"]["gpt-5.2"]["VP"] == 2
    assert metrics["by_agent"]["gram"]["VP"] == 2
    assert metrics["by_agent"]["gram"]["FP"] == 1
    assert metrics["by_agent"]["gram"]["FN"] == 1
