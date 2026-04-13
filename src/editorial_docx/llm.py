from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_RETRY_BACKOFF_SECONDS = 1.0


def _load_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"

    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv()

def _build_provider_config(provider: str) -> dict[str, str]:
    provider = (provider or "").strip().lower()

    if provider == "ollama":
        return {
            "provider": "ollama",
            "model": (os.getenv("OLLAMA_MODEL") or os.getenv("LLM_MODEL") or "llama3.1:8b").strip(),
            "base_url": (os.getenv("OLLAMA_BASE_URL") or os.getenv("LLM_BASE_URL") or "http://localhost:11434/v1").strip(),
            "api_key": (os.getenv("OLLAMA_API_KEY") or os.getenv("LLM_API_KEY") or "ollama").strip(),
        }

    if provider == "openai_compatible":
        return {
            "provider": "openai_compatible",
            "model": (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL).strip(),
            "base_url": (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip(),
            "api_key": os.getenv("LLM_API_KEY", "").strip(),
        }

    return {
        "provider": "openai",
        "model": (os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL).strip(),
        "base_url": (os.getenv("OPENAI_BASE_URL") or "").strip(),
        "api_key": (os.getenv("OPENAI_API_KEY") or "").strip(),
    }


def _is_config_usable(config: dict[str, str]) -> bool:
    provider = config.get("provider", "").strip().lower()
    if provider == "openai":
        return bool(config.get("api_key"))
    if provider == "openai_compatible":
        return bool(config.get("base_url"))
    if provider == "ollama":
        return bool(config.get("base_url"))
    return False


def get_llm_candidate_configs() -> list[dict[str, str]]:
    _load_env()

    explicit_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    inferred_provider = "ollama" if os.getenv("OLLAMA_MODEL") or os.getenv("OLLAMA_BASE_URL") else "openai"
    fallback_provider = explicit_provider or inferred_provider

    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for provider in ("openai", fallback_provider):
        config = _build_provider_config(provider)
        key = (config["provider"], config["model"], config["base_url"])
        if key in seen or not _is_config_usable(config):
            continue
        seen.add(key)
        candidates.append(config)

    return candidates


def get_llm_config() -> dict[str, str]:
    candidates = get_llm_candidate_configs()
    if candidates:
        return candidates[0]
    return _build_provider_config("openai")


def get_llm_model_tag(config: dict[str, str] | None = None) -> str:
    current = config or get_llm_config()
    model_name = (current.get("model") or "modelo").strip().lower()
    model_name = model_name.replace("\\", "/").split("/")[-1]
    tag = re.sub(r"[^a-z0-9]+", "_", model_name).strip("_")
    tag = re.sub(r"(?<=[a-z])_(?=\d)", "", tag)
    tag = re.sub(r"(?<=\d)_(?=[a-z])", "", tag)
    return tag or "modelo"


def get_llm_retry_config() -> dict[str, int | float]:
    _load_env()

    raw_attempts = (os.getenv("LLM_MAX_RETRIES") or "").strip()
    raw_backoff = (os.getenv("LLM_RETRY_BACKOFF_SECONDS") or "").strip()

    try:
        max_retries = max(1, int(raw_attempts)) if raw_attempts else DEFAULT_LLM_MAX_RETRIES
    except ValueError:
        max_retries = DEFAULT_LLM_MAX_RETRIES

    try:
        backoff_seconds = max(0.0, float(raw_backoff)) if raw_backoff else DEFAULT_LLM_RETRY_BACKOFF_SECONDS
    except ValueError:
        backoff_seconds = DEFAULT_LLM_RETRY_BACKOFF_SECONDS

    return {
        "max_retries": max_retries,
        "backoff_seconds": backoff_seconds,
    }


def get_chat_model():
    models = get_chat_models()
    return models[0][1] if models else None


def get_chat_models():
    try:
        from langchain_openai import ChatOpenAI
    except Exception:
        return []

    out = []
    for config in get_llm_candidate_configs():
        kwargs: dict[str, str | int | float] = {
            "model": config["model"],
            "temperature": 0,
            "api_key": config["api_key"] or "local",
        }
        if config["base_url"]:
            kwargs["base_url"] = config["base_url"]
        out.append((config, ChatOpenAI(**kwargs)))
    return out
