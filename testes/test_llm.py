from editorial_docx.llm import get_llm_config


def _clear_llm_env(monkeypatch):
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OLLAMA_MODEL",
        "OLLAMA_BASE_URL",
        "OLLAMA_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_get_llm_config_defaults_to_openai(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    config = get_llm_config()

    assert config["provider"] == "openai"
    assert config["model"] == "gpt-4o-mini"
    assert config["api_key"] == "sk-test"


def test_get_llm_config_uses_ollama_settings(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")

    config = get_llm_config()

    assert config["provider"] == "ollama"
    assert config["base_url"] == "http://localhost:11434/v1"
    assert config["model"] == "llama3.1:8b"
    assert config["api_key"] == "ollama"


def test_get_llm_config_uses_openai_compatible_settings(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://interna/v1")
    monkeypatch.setenv("LLM_MODEL", "modelo-interno")

    config = get_llm_config()

    assert config["provider"] == "openai_compatible"
    assert config["base_url"] == "http://interna/v1"
    assert config["model"] == "modelo-interno"
    assert config["api_key"] == ""
