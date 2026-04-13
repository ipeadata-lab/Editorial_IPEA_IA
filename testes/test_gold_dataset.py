from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from editorial_docx.gold_dataset import build_gold_annotation_template


def test_build_gold_annotation_template_creates_expected_structure():
    dataset = build_gold_annotation_template(
        [
            {
                "agent": "gram",
                "category": "Concordância",
                "message": "A concordância verbal está incorreta.",
                "paragraph_index": 8,
                "issue_excerpt": "e sugerem",
                "suggested_fix": "e sugere",
            }
        ],
        source_document="doc.docx",
        report_path="saida.json",
        model_name="gpt-5.2",
        run_label="seed_inicial",
    )

    assert dataset["dataset_version"] == "1.0"
    assert dataset["summary"]["total_model_comments"] == 1
    assert dataset["summary"]["by_agent"] == {"gram": 1}
    assert dataset["label_taxonomy"]["comment_labels"] == ["correto", "parcial", "incorreto"]
    assert dataset["label_taxonomy"]["missed_issue_labels"] == ["faltou"]
    assert dataset["annotations"][0]["id"] == "gram_0001"
    assert dataset["annotations"][0]["label"] == ""
    assert dataset["annotations"][0]["model_comment"] == "A concordância verbal está incorreta."
    assert dataset["missed_issues"][0]["label"] == "faltou"
