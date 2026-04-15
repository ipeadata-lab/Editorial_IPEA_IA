from __future__ import annotations

from dataclasses import dataclass, field


AGENT_SHORT_LABELS = {
    "metadados": "meta",
    "sinopse_abstract": "sin",
    "estrutura": "est",
    "gramatica_ortografia": "gram",
    "tabelas_figuras": "tab",
    "comentarios_usuario_referencias": "usrref",
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
class DocumentUserComment:
    comment_id: int
    author: str
    text: str
    paragraph_index: int | None = None
    anchor_excerpt: str = ""
    paragraph_text: str = ""


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
class AgentBatchTrace:
    agent: str
    batch_index: int
    total_batches: int
    status: str = ""
    llm_raw_comment_count: int = 0
    llm_post_review_comment_count: int = 0
    llm_validated_comment_count: int = 0
    llm_rejected_comment_count: int = 0
    heuristic_accepted_comment_count: int = 0
    visible_comment_count: int = 0


@dataclass(slots=True)
class AgentExecutionTrace:
    agent: str
    batches: list[AgentBatchTrace] = field(default_factory=list)
    llm_raw_comment_count: int = 0
    llm_post_review_comment_count: int = 0
    llm_validated_comment_count: int = 0
    llm_rejected_comment_count: int = 0
    heuristic_accepted_comment_count: int = 0
    failed: bool = False
    failure_status: str = ""


@dataclass(slots=True)
class ExecutionTrace:
    agents: list[AgentExecutionTrace] = field(default_factory=list)


@dataclass(slots=True)
class ConversationResult:
    answer: str
    comments: list[AgentComment]
    verification: VerificationSummary = field(default_factory=VerificationSummary)
    trace: ExecutionTrace = field(default_factory=ExecutionTrace)
