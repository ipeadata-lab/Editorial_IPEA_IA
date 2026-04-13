from __future__ import annotations

from dataclasses import dataclass, field


AGENT_SHORT_LABELS = {
    "metadados": "meta",
    "sinopse_abstract": "sin",
    "estrutura": "est",
    "gramatica_ortografia": "gram",
    "tabelas_figuras": "tab",
    "referencias": "ref",
    "tipografia": "tip",
    "coordenador": "coord",
}


def agent_short_label(agent: str) -> str:
    return AGENT_SHORT_LABELS.get((agent or "").strip(), (agent or "").strip())


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
class VerificationDecision:
    comment: AgentComment
    accepted: bool
    reason: str
    source: str = "llm"
    batch_index: int | None = None


@dataclass(slots=True)
class VerificationSummary:
    decisions: list[VerificationDecision] = field(default_factory=list)
    accepted_count: int = 0
    rejected_count: int = 0


@dataclass(slots=True)
class ConversationResult:
    answer: str
    comments: list[AgentComment]
    verification: VerificationSummary = field(default_factory=VerificationSummary)
