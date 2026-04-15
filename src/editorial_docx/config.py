from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_DATA_DIR = PROJECT_ROOT / "input_data"
OUTPUT_DATA_DIR = PROJECT_ROOT / "output_data"
TMP_DATA_DIR = PROJECT_ROOT / ".tmp"

DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_API_KEY = "ollama"

DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_LLM_TIMEOUT_SECONDS = 120.0
DEFAULT_GRAMMAR_AGENT_MAX_WORKERS = 3

GRAMMAR_BATCH_SIZE = 4
GRAMMAR_BATCH_OVERLAP = 1
DEFAULT_REVIEW_MAX_BATCH_CHARS = 12000
DEFAULT_REVIEW_MAX_BATCH_CHUNKS = 28
DEFAULT_REVIEW_WINDOW_RADIUS = 2


def ensure_runtime_directories() -> None:
    for directory in (INPUT_DATA_DIR, OUTPUT_DATA_DIR, TMP_DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def resolve_input_path(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.exists():
        return candidate.resolve()

    input_candidate = INPUT_DATA_DIR / candidate.name
    if input_candidate.exists():
        return input_candidate.resolve()

    return candidate


def build_output_paths(source_path: Path, model_tag: str) -> dict[str, Path]:
    ensure_runtime_directories()

    stem = source_path.stem
    if stem.endswith("_normalized_document"):
        stem = stem[: -len("_normalized_document")]
    report_json = OUTPUT_DATA_DIR / f"{stem}_output_{model_tag}.relatorio.json"
    return {
        "normalized_json": OUTPUT_DATA_DIR / f"{stem}_normalized_document.json",
        "report_json": report_json,
        "diagnostics_json": report_json.with_name(f"{report_json.stem}.diagnostics.json"),
        "docx": OUTPUT_DATA_DIR / f"{stem}_output_{model_tag}.docx",
    }
