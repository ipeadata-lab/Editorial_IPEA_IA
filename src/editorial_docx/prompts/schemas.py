from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, RootModel


class AgentCommentPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    category: str
    message: str
    paragraph_index: int | None = None
    issue_excerpt: str = ""
    suggested_fix: str = ""
    format_spec: str = ""


class AgentCommentsPayload(RootModel[list[AgentCommentPayload]]):
    pass


class CommentReviewPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    paragraph_index: int | None = None
    issue_excerpt: str = ""
    suggested_fix: str = ""
    decision: Literal["approve", "reject"]
    reason: str = ""


class CommentReviewsPayload(RootModel[list[CommentReviewPayload]]):
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
        "Em cada item, use `message` para explicar de forma natural e objetiva o que está errado ou faltando no trecho. "
        "Use `suggested_fix` para trazer a correção exata do fragmento ou uma instrução curta e concreta de ajuste. "
        "Se `suggested_fix` já trouxer a correção, mantenha `message` em no máximo uma frase curta e nunca mencione hipóteses descartadas ou diga que 'não há ajuste necessário'. "
        "Evite mensagens genéricas como `ajustar trecho` ou `corrigir problema`. "
        "Siga este JSON Schema: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


def review_output_contract_text() -> str:
    schema = CommentReviewsPayload.model_json_schema()
    return (
        "Você DEVE responder APENAS com um JSON válido, sem markdown nem texto extra. "
        "Avalie cada comentário proposto e retorne uma lista com `decision` igual a `approve` ou `reject`. "
        "Use `reason` com justificativa curta e objetiva. "
        "Não crie novos comentários nem novas correções. "
        "Siga este JSON Schema: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
