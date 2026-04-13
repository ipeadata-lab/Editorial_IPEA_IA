import editorial_docx.llm as llm_module
from editorial_docx.llm import get_llm_candidate_configs, get_llm_config, get_llm_model_tag, get_llm_retry_config


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
    monkeypatch.setattr(llm_module, "_load_env", lambda: None)


def test_get_llm_config_defaults_to_openai(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    config = get_llm_config()

    assert config["provider"] == "openai"
    assert config["model"] == "gpt-5.2"
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


def test_get_llm_candidate_configs_prefers_openai_before_fallback_provider(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.2")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "modelo-interno")
    monkeypatch.setenv("LLM_BASE_URL", "http://interna/v1")

    configs = get_llm_candidate_configs()

    assert [cfg["provider"] for cfg in configs] == ["openai", "openai_compatible"]
    assert configs[0]["model"] == "gpt-5.2"
    assert configs[1]["model"] == "modelo-interno"


def test_get_llm_candidate_configs_uses_fallback_when_openai_is_unavailable(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "modelo-interno")
    monkeypatch.setenv("LLM_BASE_URL", "http://interna/v1")

    configs = get_llm_candidate_configs()

    assert len(configs) == 1
    assert configs[0]["provider"] == "openai_compatible"


def test_get_llm_model_tag_normalizes_openai_model_name():
    assert get_llm_model_tag({"model": "gpt-4o-mini"}) == "gpt4o_mini"


def test_get_llm_model_tag_normalizes_ollama_model_name():
    assert get_llm_model_tag({"model": "qwen3:14b"}) == "qwen3_14b"


def test_get_llm_retry_config_uses_defaults(monkeypatch):
    _clear_llm_env(monkeypatch)

    config = get_llm_retry_config()

    assert config["max_retries"] == 3
    assert config["backoff_seconds"] == 1.0


def test_get_llm_retry_config_reads_env(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_SECONDS", "2.5")

    config = get_llm_retry_config()

    assert config["max_retries"] == 5
    assert config["backoff_seconds"] == 2.5
