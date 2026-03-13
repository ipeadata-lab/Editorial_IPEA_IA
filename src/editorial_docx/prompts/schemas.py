from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, RootModel


class AgentCommentPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    category: str
    message: str
    paragraph_index: int | None = None
    issue_excerpt: str = ""
    suggested_fix: str = ""
    auto_apply: bool = False
    format_spec: str = ""


class AgentCommentsPayload(RootModel[list[AgentCommentPayload]]):
    pass


class PromptProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str
    description: str
    instruction: str


def agent_output_contract_text() -> str:
    """Human-readable contract injected into prompts for consistent JSON output."""
    schema = AgentCommentsPayload.model_json_schema()
    return (
        "Você DEVE responder APENAS com um JSON válido, sem markdown, sem explicação extra e sem cercas de código. "
        "Se não houver achados no trecho analisado, responda com uma lista vazia: []. "
        "Siga este JSON Schema: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
