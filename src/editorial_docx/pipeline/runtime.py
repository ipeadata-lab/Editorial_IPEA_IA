from __future__ import annotations

import json
import re
import time
from json import JSONDecodeError

from langchain_core.prompts import ChatPromptTemplate

from ..config import GRAMMAR_CONTEXT_MODE, TEXTO_INTEIRO
from ..llm import get_chat_model, get_chat_models, get_llm_retry_config
from ..models import AgentComment, agent_short_label
from ..prompts import AgentCommentsPayload, CommentReviewsPayload, build_coordinator_prompt
from ..token_utils import truncate_text
from .context import PreparedReviewDocument, ReviewBatch

_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")


class LLMConnectionFailure(RuntimeError):
    def __init__(self, operation: str, attempts: int, original: Exception):
        self.operation = operation
        self.attempts = attempts
        self.original = original
        super().__init__(f"{operation} falhou por conexão após {attempts} tentativa(s): {original}")


def _sanitize_for_llm(text: str) -> str:
    cleaned = _CTRL_RE.sub(" ", text or "")
    cleaned = _SURROGATE_RE.sub(" ", cleaned)
    return cleaned.replace("\ufeff", " ").strip()


def _is_json_body_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "could not parse the json body of your request" in msg


def _iter_exception_chain(exc: Exception):
    current: Exception | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, Exception) else None


def _is_connection_error(exc: Exception) -> bool:
    connection_names = {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ReadTimeout",
        "WriteTimeout",
        "ConnectTimeout",
        "TimeoutException",
    }
    connection_tokens = {
        "connection error",
        "getaddrinfo failed",
        "name or service not known",
        "temporary failure in name resolution",
        "failed to resolve",
        "dns",
        "timed out",
        "timeout",
        "connection reset",
        "network is unreachable",
    }
    for item in _iter_exception_chain(exc):
        if item.__class__.__name__ in connection_names:
            return True
        msg = str(item).lower()
        if any(token in msg for token in connection_tokens):
            return True
    return False


def _connection_error_summary(exc: Exception) -> str:
    messages: list[str] = []
    for item in _iter_exception_chain(exc):
        msg = str(item).strip()
        if msg:
            messages.append(msg)
    for msg in messages:
        if "getaddrinfo failed" in msg.lower():
            return "falha de DNS/conectividade (`getaddrinfo failed`)"
    if messages:
        return messages[-1]
    return "falha de conexão com a LLM"


def _is_quota_or_rate_limit_error(exc: Exception) -> bool:
    quota_tokens = {
        "insufficient_quota",
        "rate limit",
        "ratelimit",
        "quota",
        "too many requests",
        "error code: 429",
    }
    for item in _iter_exception_chain(exc):
        msg = str(item).lower()
        if any(token in msg for token in quota_tokens):
            return True
    return False


def _quota_or_rate_limit_summary(exc: Exception) -> str:
    messages: list[str] = []
    for item in _iter_exception_chain(exc):
        msg = str(item).strip()
        if msg:
            messages.append(msg)
    for msg in messages:
        lowered = msg.lower()
        if "insufficient_quota" in lowered:
            return "cota esgotada (`insufficient_quota`)"
        if "rate limit" in lowered or "too many requests" in lowered or "error code: 429" in lowered:
            return msg
    if messages:
        return messages[-1]
    return "limite da API atingido"


def _classify_llm_failure(exc: Exception) -> tuple[str, str]:
    if _is_connection_error(exc):
        return "connection", _connection_error_summary(exc)
    if _is_quota_or_rate_limit_error(exc):
        return "quota/rate limit", _quota_or_rate_limit_summary(exc)
    if _is_json_body_error(exc):
        return "json/payload", "falha ao montar ou interpretar o payload/json da LLM"

    messages: list[str] = []
    for item in _iter_exception_chain(exc):
        msg = str(item).strip()
        if msg:
            messages.append(msg)
    if messages:
        return "unknown", messages[-1]
    return "unknown", exc.__class__.__name__


def _invoke_with_retry(runnable, payload: dict[str, str], operation: str):
    retry_config = get_llm_retry_config()
    max_retries = int(retry_config["max_retries"])
    backoff_seconds = float(retry_config["backoff_seconds"])
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return runnable.invoke(payload)
        except Exception as exc:
            if _is_json_body_error(exc) or not _is_connection_error(exc):
                raise
            last_exc = exc
            if attempt >= max_retries:
                break
            if backoff_seconds > 0:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))

    if last_exc is None:
        raise RuntimeError(f"{operation} falhou sem exceção capturada.")
    raise LLMConnectionFailure(operation=operation, attempts=max_retries, original=last_exc) from last_exc


def _partial_answer_from_comments(comments: list[AgentComment], prefix: str) -> str:
    if comments:
        points = "\n".join(f"- [{agent_short_label(c.agent)}] {c.message}" for c in comments[:12])
        return prefix + "\n" + points
    return prefix


def _build_coordinator_document_excerpt(comments: list[AgentComment], limit: int = 12) -> str:
    if not comments:
        return "(sem comentários aceitos para contextualizar a síntese final)"

    lines: list[str] = []
    for item in comments[:limit]:
        paragraph_label = (
            f"[{item.paragraph_index}] "
            if isinstance(item.paragraph_index, int)
            else ""
        )
        excerpt = (item.issue_excerpt or "").strip()
        if excerpt:
            lines.append(f"- {paragraph_label}{excerpt}")
        else:
            lines.append(f"- {paragraph_label}{item.message}")
    return "\n".join(lines)


def _truncate_progressive_summary(summary: str, max_chars: int = 6000) -> str:
    text = re.sub(r"\s+\n", "\n", (summary or "").strip())
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars].rstrip()
    cut = trimmed.rfind("\n- ")
    if cut > int(max_chars * 0.55):
        trimmed = trimmed[:cut].rstrip()
    return trimmed


def _comment_memory_lines(comments: list[AgentComment], limit: int = 6) -> str:
    if not comments:
        return "(sem comentários aceitos nesta passagem)"
    lines = [
        f"- [{agent_short_label(item.agent)}] {item.message}"
        + (f" | correção: {item.suggested_fix}" if (item.suggested_fix or "").strip() else "")
        for item in comments[:limit]
    ]
    return "\n".join(lines)


def _deterministic_progressive_summary(
    agent: str,
    running_summary: str,
    batch: ReviewBatch,
    accepted_comments: list[AgentComment],
) -> str:
    parts: list[str] = []
    if running_summary.strip():
        parts.append(running_summary.strip())

    heading_text = " > ".join(batch.headings) if batch.headings else "sem seção explícita"
    focus_lines = [line.strip() for line in (batch.focus_excerpt or "").splitlines() if line.strip()]
    compact_focus = " ".join(focus_lines[:3])[:900] if focus_lines else ""
    compact_focus = re.sub(r"\s+", " ", compact_focus)
    entry = (
        f"- Lote {batch.start_idx}-{batch.end_idx} [{agent} | {heading_text}]"
        + (f": {compact_focus}" if compact_focus else "")
    )
    parts.append(entry)
    if accepted_comments:
        parts.append("  Comentários aceitos:\n" + _comment_memory_lines(accepted_comments))

    return _truncate_progressive_summary("\n".join(part for part in parts if part))


def _update_running_summary(
    agent: str,
    question: str,
    running_summary: str,
    batch: ReviewBatch,
    accepted_comments: list[AgentComment],
    use_llm: bool = True,
) -> str:
    fallback = _deterministic_progressive_summary(agent, running_summary, batch, accepted_comments)
    if not use_llm or agent == "gramatica_ortografia" or get_chat_model() is None:
        return fallback

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "Você mantém a memória progressiva do agente de revisão editorial. "
                    "Condense apenas fatos relevantes já aceitos, mantendo observações objetivas e curtas."
                ),
            ),
            (
                "human",
                (
                    "Pergunta do usuário: {question}\n"
                    "Agente: {agent}\n\n"
                    "Memória anterior:\n{running_summary}\n\n"
                    "Seções do lote atual: {headings}\n\n"
                    "Trecho-alvo atual:\n{focus_excerpt}\n\n"
                    "Comentários aceitos neste lote:\n{accepted_comments}\n\n"
                    "Retorne a memória atualizada em tópicos curtos."
                ),
            ),
        ]
    )

    payload = {
        "agent": agent,
        "question": _sanitize_for_llm(question),
        "running_summary": _sanitize_for_llm(running_summary or "(memória vazia)"),
        "headings": _sanitize_for_llm(" > ".join(batch.headings) if batch.headings else "(sem seção explícita)"),
        "focus_excerpt": _sanitize_for_llm(batch.focus_excerpt),
        "accepted_comments": _sanitize_for_llm(_comment_memory_lines(accepted_comments)),
    }
    try:
        response = _invoke_with_model_fallback(prompt, payload, operation=f"memória progressiva {agent}")
        if response is None:
            return fallback
    except Exception:
        return fallback

    content = response.content if isinstance(response.content, str) else str(response.content)
    normalized = _truncate_progressive_summary(content)
    return normalized or fallback


def _build_batch_review_excerpt(
    prepared: PreparedReviewDocument,
    batch: ReviewBatch,
    running_summary: str,
    agent: str | None = None,
) -> str:
    if agent == "gramatica_ortografia":
        if GRAMMAR_CONTEXT_MODE == TEXTO_INTEIRO:
            return batch.focus_excerpt
        headings = " > ".join(batch.headings) if batch.headings else "sem seção explícita"
        compact_window = truncate_text(batch.window_excerpt, max_tokens=450)
        return (
            "JANELA MÍNIMA DE CONTEXTO:\n"
            f"- Faixa atual: {batch.start_idx}-{batch.end_idx}\n"
            f"- Seções relacionadas: {headings}\n"
            f"{compact_window}\n\n"
            "TRECHO-ALVO DESTA PASSAGEM:\n"
            f"{batch.focus_excerpt}"
        )

    toc_text = "\n".join(f"- {line}" for line in prepared.toc[:20]) if prepared.toc else "- (sem seções explícitas)"
    memory_text = running_summary.strip() or "(sem histórico consolidado para este agente até agora)"
    headings = " > ".join(batch.headings) if batch.headings else "sem seção explícita"
    return (
        "MAPA DO DOCUMENTO:\n"
        f"- Total de trechos: {len(prepared.chunks)}\n"
        f"- Faixa atual: {batch.start_idx}-{batch.end_idx}\n"
        f"- Seções relacionadas: {headings}\n"
        f"{toc_text}\n\n"
        "MEMÓRIA PROGRESSIVA DO AGENTE:\n"
        f"{memory_text}\n\n"
        "JANELA DE CONTEXTO:\n"
        f"{batch.window_excerpt}\n\n"
        "TRECHO-ALVO DESTA PASSAGEM:\n"
        f"{batch.focus_excerpt}"
    )


def _invoke_with_model_fallback(prompt, payload: dict[str, str], operation: str):
    last_exc: Exception | None = None
    for candidate in get_chat_models():
        if candidate is None:
            continue
        model = candidate[1] if isinstance(candidate, tuple) and len(candidate) >= 2 else candidate
        runnable = prompt | model
        try:
            return _invoke_with_retry(runnable, payload, operation=operation)
        except LLMConnectionFailure:
            raise
        except Exception as exc:
            if _is_json_body_error(exc):
                raise
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    return None


def _invoke_coordinator_with_retry(prompt, payload: dict[str, str]):
    retry_config = get_llm_retry_config()
    max_retries = int(retry_config["max_retries"])
    backoff_seconds = float(retry_config["backoff_seconds"])
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = _invoke_with_model_fallback(prompt, payload, operation="coordenador")
            if response is not None:
                return response
            last_exc = RuntimeError("coordenador não retornou resposta")
        except Exception as exc:
            last_exc = exc

        if attempt >= max_retries:
            break
        if backoff_seconds > 0:
            time.sleep(backoff_seconds * (2 ** (attempt - 1)))

    if last_exc is not None:
        raise last_exc
    return None


def _serialize_comments(comments: list[AgentComment]) -> str:
    payload: list[dict[str, object]] = []
    for item in comments:
        payload.append(
            {
                "agent": item.agent,
                "category": item.category,
                "message": item.message,
                "paragraph_index": item.paragraph_index,
                "issue_excerpt": item.issue_excerpt,
                "suggested_fix": item.suggested_fix,
                "auto_apply": item.auto_apply,
                "format_spec": item.format_spec,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_comments_with_status(raw: str, agent: str) -> tuple[list[AgentComment], str]:
    content = (raw or "").strip()
    if not content:
        return [], "sem conteúdo"

    candidates = [content]
    fenced_match = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.append(fenced_match.group(1).strip())

    key_match = re.search(r'"comments"\s*:\s*(\[[\s\S]*\])', content, flags=re.IGNORECASE)
    if key_match:
        candidates.append(key_match.group(1).strip())

    array_start = content.find("[")
    array_end = content.rfind("]")
    if array_start != -1 and array_end != -1 and array_end > array_start:
        candidates.append(content[array_start : array_end + 1].strip())

    statuses: list[str] = []
    for idx, candidate in enumerate(candidates):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except JSONDecodeError:
            statuses.append("falha json")
            continue

        if isinstance(data, dict):
            maybe_comments = data.get("comments")
            if isinstance(maybe_comments, list):
                data = maybe_comments
            else:
                statuses.append("json sem comments")
                continue

        if not isinstance(data, list):
            statuses.append("json não é lista")
            continue

        comments: list[AgentComment] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category") or "").strip() or agent
            message = str(entry.get("message") or "").strip()
            paragraph_index = entry.get("paragraph_index")
            if isinstance(paragraph_index, bool):
                paragraph_index = int(paragraph_index)
            elif isinstance(paragraph_index, (int, float)):
                paragraph_index = int(paragraph_index)
            else:
                paragraph_index = None
            auto_apply = bool(entry.get("auto_apply")) if agent in {"estrutura", "tabelas_figuras", "referencias"} else False
            format_spec = str(entry.get("format_spec") or "").strip() if auto_apply or agent == "tipografia" else str(entry.get("format_spec") or "").strip()
            comments.append(
                AgentComment(
                    agent=agent,
                    category=category,
                    message=message,
                    paragraph_index=paragraph_index,
                    issue_excerpt=str(entry.get("issue_excerpt") or "").strip(),
                    suggested_fix=str(entry.get("suggested_fix") or "").strip(),
                    auto_apply=auto_apply,
                    format_spec=format_spec,
                )
            )
        if comments:
            status = "json direto" if idx == 0 else "json recuperado"
            return comments, status

    return [], "sem comentários válidos"


def _parse_comments(raw: str, agent: str) -> list[AgentComment]:
    comments, _ = _parse_comments_with_status(raw, agent=agent)
    return comments


def _parse_comment_reviews(raw: str) -> tuple[list[dict[str, object]], str]:
    content = (raw or "").strip()
    if not content:
        return [], "sem conteúdo"

    candidates = [content]
    fenced_match = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.append(fenced_match.group(1).strip())

    key_match = re.search(r'"reviews"\s*:\s*(\[[\s\S]*\])', content, flags=re.IGNORECASE)
    if key_match:
        candidates.append(key_match.group(1).strip())

    array_start = content.find("[")
    array_end = content.rfind("]")
    if array_start != -1 and array_end != -1 and array_end > array_start:
        candidates.append(content[array_start : array_end + 1].strip())

    for idx, candidate in enumerate(candidates):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except JSONDecodeError:
            continue

        if isinstance(data, dict):
            maybe_reviews = data.get("reviews")
            if isinstance(maybe_reviews, list):
                data = maybe_reviews
            else:
                continue

        if not isinstance(data, list):
            continue

        reviews: list[dict[str, object]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            decision = str(entry.get("decision") or "").strip().lower()
            if decision not in {"approve", "reject"}:
                continue
            paragraph_index = entry.get("paragraph_index")
            if isinstance(paragraph_index, bool):
                paragraph_index = int(paragraph_index)
            elif isinstance(paragraph_index, (int, float)):
                paragraph_index = int(paragraph_index)
            else:
                paragraph_index = None
            reviews.append(
                {
                    "paragraph_index": paragraph_index,
                    "issue_excerpt": str(entry.get("issue_excerpt") or "").strip(),
                    "suggested_fix": str(entry.get("suggested_fix") or "").strip(),
                    "decision": decision,
                    "reason": str(entry.get("reason") or "").strip(),
                }
            )
        if reviews:
            status = "json direto" if idx == 0 else "json recuperado"
            return reviews, status

    return [], "sem revisões válidas"


def build_coordinator_answer(question: str, comments: list[AgentComment]) -> str:
    if get_chat_model() is None:
        if comments:
            points = "\n".join(f"- [{agent_short_label(c.agent)}] {c.message}" for c in comments[:8])
            return "Resumo dos agentes:\n" + points
        return "Nenhum comentário relevante foi identificado pelos agentes."

    prompt = build_coordinator_prompt()
    payload = {
        "question": _sanitize_for_llm(question),
        "document_excerpt": _sanitize_for_llm(_build_coordinator_document_excerpt(comments)),
        "comments_json": _sanitize_for_llm(_serialize_comments(comments)),
    }
    try:
        response = _invoke_coordinator_with_retry(prompt, payload)
        if response is None:
            return _partial_answer_from_comments(comments, "Resumo dos agentes.")
    except Exception as exc:
        category, detail = _classify_llm_failure(exc)
        return _partial_answer_from_comments(
            comments,
            f"Resumo dos agentes (coordenador indisponível por {category}: {detail}).",
        )

    answer = response.content if isinstance(response.content, str) else str(response.content)
    return answer.strip() or _partial_answer_from_comments(comments, "Resumo dos agentes.")


__all__ = [
    "LLMConnectionFailure",
    "_build_batch_review_excerpt",
    "_comment_memory_lines",
    "_classify_llm_failure",
    "_build_coordinator_document_excerpt",
    "_connection_error_summary",
    "_deterministic_progressive_summary",
    "_invoke_coordinator_with_retry",
    "_invoke_with_model_fallback",
    "_invoke_with_retry",
    "_is_connection_error",
    "_is_json_body_error",
    "_is_quota_or_rate_limit_error",
    "_parse_comment_reviews",
    "_parse_comments",
    "_parse_comments_with_status",
    "_partial_answer_from_comments",
    "_sanitize_for_llm",
    "_serialize_comments",
    "_truncate_progressive_summary",
    "_update_running_summary",
    "build_coordinator_answer",
]
