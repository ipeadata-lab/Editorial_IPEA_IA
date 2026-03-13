from __future__ import annotations

import re

from .schemas import PromptProfile

_DEFAULT_PROFILE = PromptProfile(
    key="GENERIC",
    description="Documento genérico",
    instruction=(
        "Considere as regras gerais de revisão editorial institucional. "
        "Se faltarem regras específicas do tipo documental, mantenha neutralidade."
    ),
)

_TD_PROFILE = PromptProfile(
    key="TD",
    description="Texto para Discussão (TD)",
    instruction=(
        "Este documento é um Texto para Discussão (TD). "
        "Aplique critérios editoriais de TD: clareza argumentativa, coerência entre seções, "
        "consistência terminológica e aderência ao padrão formal de publicação técnica."
    ),
)

_PROFILE_BY_KEY = {
    _DEFAULT_PROFILE.key: _DEFAULT_PROFILE,
    _TD_PROFILE.key: _TD_PROFILE,
}


def detect_prompt_profile(filename: str) -> PromptProfile:
    if not filename:
        return _DEFAULT_PROFILE

    # Ex.: 123456_TD_2345.docx
    match = re.search(r"_(?P<kind>[A-Za-z]{2,})_", filename)
    if not match:
        return _DEFAULT_PROFILE

    kind = match.group("kind").upper()
    return _PROFILE_BY_KEY.get(kind, _DEFAULT_PROFILE)


def get_prompt_profile(profile_key: str | None) -> PromptProfile:
    if not profile_key:
        return _DEFAULT_PROFILE
    return _PROFILE_BY_KEY.get(profile_key.upper(), _DEFAULT_PROFILE)
