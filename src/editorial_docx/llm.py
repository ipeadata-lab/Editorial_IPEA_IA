from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"

    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv()

def get_llm_config() -> dict[str, str]:
    _load_env()

    explicit_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    inferred_provider = "ollama" if os.getenv("OLLAMA_MODEL") or os.getenv("OLLAMA_BASE_URL") else "openai"
    provider = explicit_provider or inferred_provider

    if provider == "ollama":
        model_name = (os.getenv("OLLAMA_MODEL") or os.getenv("LLM_MODEL") or "llama3.1:8b").strip()
        base_url = (os.getenv("OLLAMA_BASE_URL") or os.getenv("LLM_BASE_URL") or "http://localhost:11434/v1").strip()
        api_key = (os.getenv("OLLAMA_API_KEY") or os.getenv("LLM_API_KEY") or "ollama").strip()
        return {
            "provider": "ollama",
            "model": model_name,
            "base_url": base_url,
            "api_key": api_key,
        }

    if provider == "openai_compatible":
        model_name = (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
        base_url = (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip()
        api_key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "local").strip()
        return {
            "provider": "openai_compatible",
            "model": model_name,
            "base_url": base_url,
            "api_key": api_key,
        }

    model_name = (os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-4o-mini").strip()
    base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL") or "").strip()
    api_key = (os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY") or "").strip()
    return {
        "provider": "openai",
        "model": model_name,
        "base_url": base_url,
        "api_key": api_key,
    }


def get_chat_model():
    config = get_llm_config()

    if config["provider"] == "openai" and not config["api_key"]:
        return None
    if config["provider"] == "openai_compatible" and not config["base_url"]:
        return None

    try:
        from langchain_openai import ChatOpenAI
    except Exception:
        return None

    kwargs: dict[str, str | int | float] = {
        "model": config["model"],
        "temperature": 0,
        "api_key": config["api_key"],
    }
    if config["base_url"]:
        kwargs["base_url"] = config["base_url"]

    return ChatOpenAI(**kwargs)
