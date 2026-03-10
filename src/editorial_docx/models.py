from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AgentComment:
    agent: str
    category: str
    message: str
    paragraph_index: int | None = None
    issue_excerpt: str = ""
    suggested_fix: str = ""


@dataclass(slots=True)
class ConversationResult:
    answer: str
    comments: list[AgentComment]
