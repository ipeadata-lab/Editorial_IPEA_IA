from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def get_chat_model():
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"

    # Load from project root first; fallback to default dotenv discovery.
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    try:
        from langchain_openai import ChatOpenAI
    except Exception:
        return None

    return ChatOpenAI(model=model_name, temperature=0)
