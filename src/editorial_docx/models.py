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
    auto_apply: bool = False
    format_spec: str = ""
    review_status: str = ""
    approved_text: str = ""
    reviewer_note: str = ""


@dataclass(slots=True)
class ConversationResult:
    answer: str
    comments: list[AgentComment]
