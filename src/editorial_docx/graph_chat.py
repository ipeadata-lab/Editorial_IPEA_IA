from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from json import JSONDecodeError
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .context_selector import build_excerpt
from .document_loader import Section
from .llm import get_chat_model, get_chat_models, get_llm_retry_config
from .models import AgentComment, ConversationResult, VerificationDecision, VerificationSummary, agent_short_label
from .prompts import (
    AGENT_ORDER,
    AgentCommentsPayload,
    CommentReviewsPayload,
    build_agent_prompt,
    build_comment_review_prompt,
    build_coordinator_prompt,
)


class ChatState(TypedDict, total=False):
    question: str
    document_excerpt: str
    profile_key: str
    comments: list[AgentComment]
    answer: str
    batch_status: str


class LLMConnectionFailure(RuntimeError):
    def __init__(self, operation: str, attempts: int, original: Exception):
        self.operation = operation
        self.attempts = attempts
        self.original = original
        super().__init__(f"{operation} falhou por conexão após {attempts} tentativa(s): {original}")


_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")
_REF_TYPE_RE = re.compile(r"\btipo=([a-z_]+)\b", re.IGNORECASE)
_ALLOWED_TYPOGRAPHY_KEYS = {
    "font",
    "size_pt",
    "bold",
    "italic",
    "case",
    "align",
    "space_before_pt",
    "space_after_pt",
    "line_spacing",
    "left_indent_pt",
}
_ILLUSTRATION_LABEL_RE = re.compile(
    r"^\s*(?:tabela|figura|quadro|imagem)\s+\d+\b|^\s*gr\S*fico\s+\d+\b",
    re.IGNORECASE,
)
_QUOTED_EXCERPT_RE = re.compile(r'^\s*["“”\'‘’«»].+["“”\'‘’«»]\s*$')
_QUOTE_CHAR_RE = re.compile(r'["â€œâ€\'â€˜â€™Â«Â»]')
_STYLE_BY_BLOCK_TYPE = {
    "heading": {"TITULO_1", "TITULO_2", "TITULO_3", "TÍTULO_1", "TÍTULO_2", "TÍTULO_3"},
    "paragraph": {"TEXTO"},
    "table_cell": {"TEXTO_TABELA"},
    "reference_entry": {"TEXTO_REFERENCIA"},
    "reference_heading": {"TITULO_1", "TÍTULO_1"},
    "caption": {"TEXTO", "FONTE_TABELA_GRAFICO", "TEXTO_TABELA"},
}


_SAFE_QUOTED_EXCERPT_RE = re.compile(r'^\s*["\u201c\u201d\'\u2018\u2019\u00ab\u00bb].+["\u201c\u201d\'\u2018\u2019\u00ab\u00bb]\s*$')
_SAFE_QUOTE_CHAR_RE = re.compile(r'["\u201c\u201d\'\u2018\u2019\u00ab\u00bb]')
_HEADING_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:(?:\d+(?:\.\d+)*)\.?|[ivxlcdm]+\.?)\s+", re.IGNORECASE)
_REF_NUMBERING_RE = re.compile(r"\bnumerado=sim\b", re.IGNORECASE)


def _sanitize_for_llm(text: str) -> str:
    # Remove characters that frequently break JSON payload parsing upstream.
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


def _invoke_with_model_fallback(prompt, payload: dict[str, str], operation: str):
    candidates = get_chat_models()
    if not candidates:
        return None

    last_connection_failure: LLMConnectionFailure | None = None
    last_non_connection_error: Exception | None = None

    for config, model in candidates:
        try:
            return _invoke_with_retry(prompt | model, payload, operation=f"{operation} [{config['provider']}:{config['model']}]")
        except LLMConnectionFailure as exc:
            last_connection_failure = exc
            continue
        except Exception as exc:
            last_non_connection_error = exc
            if len(candidates) > 1 and config.get("provider") == "openai":
                continue
            raise

    if last_connection_failure is not None:
        raise last_connection_failure
    if last_non_connection_error is not None:
        raise last_non_connection_error
    return None


def _serialize_comments(comments: list[AgentComment]) -> str:
    return json.dumps(
        [
            {
                "agent": c.agent,
                "category": c.category,
                "message": c.message,
                "paragraph_index": c.paragraph_index,
                "issue_excerpt": c.issue_excerpt,
                "suggested_fix": c.suggested_fix,
                "format_spec": c.format_spec,
            }
            for c in comments
        ],
        ensure_ascii=False,
        indent=2,
    )


def _parse_comments_with_status(raw: str, agent: str) -> tuple[list[AgentComment], str]:
    cleaned = (raw or "").strip()
    if not cleaned:
        return [], "resposta vazia"

    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    parsed_input: object | str = cleaned
    status = "json direto"
    try:
        parsed_input = json.loads(cleaned)
    except JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", cleaned)
        if match:
            try:
                parsed_input = json.loads(match.group(1))
                status = "json extraído"
            except JSONDecodeError:
                parsed_input = cleaned

    if isinstance(parsed_input, dict):
        for key in ("comments", "itens", "items", "results", "root", "data"):
            value = parsed_input.get(key)
            if isinstance(value, list):
                parsed_input = value
                status = f"lista em `{key}`"
                break

    try:
        parsed = AgentCommentsPayload.model_validate(parsed_input)
    except Exception:
        return [], "resposta fora do schema"

    out: list[AgentComment] = []
    for item in parsed.root:
        if not item.message:
            continue
        category = item.category or agent
        out.append(
            AgentComment(
                agent=agent,
                category=category,
                message=item.message,
                paragraph_index=item.paragraph_index,
                issue_excerpt=item.issue_excerpt,
                suggested_fix=item.suggested_fix,
                auto_apply=False,
                format_spec=item.format_spec,
            )
        )

    if not out:
        return [], "json válido sem comentários"
    return out, status


def _parse_comments(raw: str, agent: str) -> list[AgentComment]:
    items, _ = _parse_comments_with_status(raw, agent)
    return items


def _parse_comment_reviews(raw: str) -> tuple[list[dict[str, object]], str]:
    cleaned = (raw or "").strip()
    if not cleaned:
        return [], "revisor vazio"

    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    parsed_input: object | str = cleaned
    status = "json direto"
    try:
        parsed_input = json.loads(cleaned)
    except JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", cleaned)
        if match:
            try:
                parsed_input = json.loads(match.group(1))
                status = "json extraído"
            except JSONDecodeError:
                parsed_input = cleaned

    if isinstance(parsed_input, dict):
        for key in ("reviews", "itens", "items", "results", "root", "data"):
            value = parsed_input.get(key)
            if isinstance(value, list):
                parsed_input = value
                status = f"lista em `{key}`"
                break

    try:
        parsed = CommentReviewsPayload.model_validate(parsed_input)
    except Exception:
        return [], "revisor fora do schema"

    out = [
        {
            "paragraph_index": item.paragraph_index,
            "issue_excerpt": item.issue_excerpt,
            "suggested_fix": item.suggested_fix,
            "decision": item.decision,
            "reason": item.reason,
        }
        for item in parsed.root
    ]
    if not out:
        return [], "revisor sem itens"
    return out, status


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _folded_text(value: str) -> str:
    return _ascii_fold(_normalized_text(value))


def _parse_format_spec(raw: str) -> dict[str, str]:
    spec: dict[str, str] = {}
    for part in (raw or "").split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            spec[key] = value
    return spec


def _ref_block_type(ref: str) -> str:
    match = _REF_TYPE_RE.search(ref or "")
    return match.group(1).lower() if match else ""


def _ref_style_name(ref: str) -> str:
    match = re.search(r"\bestilo=([^|]+)", ref or "", re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _indexes_by_ref_type(refs: list[str], allowed_types: set[str]) -> list[int]:
    return [idx for idx, ref in enumerate(refs) if _ref_block_type(ref) in allowed_types]


def _style_name_looks_explicit(style_name: str) -> bool:
    normalized = (style_name or "").strip().casefold()
    if not normalized:
        return False
    generic = {"normal", "paragraph", "parágrafo", "paragrafo", "texto", "body text", "corpo de texto"}
    return normalized not in generic


def _is_relevant_typography_spec(spec: dict[str, str]) -> bool:
    strong_keys = {"size_pt", "bold", "italic", "case", "align", "left_indent_pt"}
    if any(key in spec for key in strong_keys):
        return True
    spacing_keys = [key for key in ("space_before_pt", "space_after_pt", "line_spacing") if key in spec]
    return len(spacing_keys) == 3


def _is_illustration_caption(text: str) -> bool:
    return bool(_ILLUSTRATION_LABEL_RE.match(_normalized_text(text)))


def _looks_like_all_caps_title(text: str) -> bool:
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    return upper_ratio >= 0.8


def _looks_like_quoted_excerpt(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped and _SAFE_QUOTED_EXCERPT_RE.match(stripped))


def _contains_quote_marks(text: str) -> bool:
    return bool(_SAFE_QUOTE_CHAR_RE.search(text or ""))


def _ascii_fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _diacritic_count(text: str) -> int:
    return sum(1 for ch in unicodedata.normalize("NFKD", text or "") if unicodedata.combining(ch))


def _strip_heading_prefix(text: str) -> str:
    return _HEADING_NUMBER_PREFIX_RE.sub("", (text or "").strip())


def _heading_word_count(text: str) -> int:
    stripped = _strip_heading_prefix(text)
    return len(re.findall(r"[A-Za-zÀ-ÿ0-9]+", stripped))


def _ref_has_numbering(ref: str) -> bool:
    return bool(_REF_NUMBERING_RE.search(ref or ""))


def _ref_has_flag(ref: str, flag: str) -> bool:
    return f"{flag}=sim" in _normalized_text(ref or "")


def _ref_align(ref: str) -> str:
    match = re.search(r"\balign=([a-z]+)\b", _normalized_text(ref or ""))
    return match.group(1) if match else ""


def _is_implicit_heading_candidate(index: int, chunks: list[str], refs: list[str]) -> bool:
    if not (0 <= index < len(chunks)):
        return False
    ref_type = _ref_block_type(refs[index]) if index < len(refs) else ""
    if ref_type not in {"", "paragraph", "heading"}:
        return False

    text = (chunks[index] or "").strip()
    if not text or _heading_word_count(text) == 0 or _heading_word_count(text) > 4:
        return False

    stripped = _strip_heading_prefix(text)
    if not stripped:
        return False
    if stripped[-1] in ".!?;:":
        return False
    if _contains_quote_marks(stripped):
        return False

    folded = _ascii_fold(stripped).casefold()
    banned_prefixes = {
        "fonte",
        "elaboracao",
        "elaboração",
        "nota",
        "nota:",
        "figura",
        "grafico",
        "gráfico",
        "tabela",
        "quadro",
        "imagem",
        "palavras-chave",
        "keywords",
        "jel",
        "abstract",
        "sinopse",
    }
    if any(folded.startswith(prefix) for prefix in banned_prefixes):
        return False

    if stripped[0].islower():
        return False

    next_idx = index + 1
    if next_idx >= len(chunks):
        return False
    next_text = (chunks[next_idx] or "").strip()
    next_ref_type = _ref_block_type(refs[next_idx]) if next_idx < len(refs) else ""
    if next_ref_type not in {"", "paragraph"}:
        return False
    if len(re.findall(r"[A-Za-zÀ-ÿ0-9]+", next_text)) < 6:
        return False

    return True


def _is_intro_heading(text: str) -> bool:
    stripped = _strip_heading_prefix(text)
    if not stripped:
        return False
    folded = _ascii_fold(stripped).casefold()
    return any(
        folded.startswith(prefix)
        for prefix in (
            "introducao",
            "introduction",
            "consideracoes iniciais",
            "consideracoes introdutorias",
            "nota introdutoria",
            "notas introdutorias",
        )
    )


def _removes_terminal_period_only(issue_excerpt: str, suggested_fix: str) -> bool:
    issue = (issue_excerpt or "").strip()
    suggestion = (suggested_fix or "").strip()
    if not issue or not suggestion or not issue.endswith("."):
        return False
    return suggestion == issue[:-1].rstrip()


def _years_in_text(text: str) -> list[str]:
    return re.findall(r"\b(?:19|20)\d{2}\b", text or "")


def _quoted_terms(text: str) -> list[str]:
    return re.findall(r"[\"']([^\"']{2,80})[\"']", text or "")


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", text or "", flags=re.UNICODE)


def _count_words(text: str) -> int:
    return len(_word_tokens(text))


def _extract_word_limit(text: str) -> int | None:
    match = re.search(r"\b(\d{2,4})\s+palavras\b", text or "", re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _split_keyword_entries(text: str) -> list[str]:
    cleaned = re.sub(r"^(palavras-chave|keywords)\s*:\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    parts = [part.strip(" .;:,") for part in re.split(r"[;\n]", cleaned) if part.strip(" .;:,")]
    return [part for part in parts if part]


def _has_repeated_keyword_entries(text: str) -> bool:
    keywords = [_normalized_text(part) for part in _split_keyword_entries(text)]
    keywords = [part for part in keywords if part]
    return len(keywords) != len(set(keywords))


def _punctuation_only_change(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_tokens = _word_tokens(issue_excerpt.casefold())
    suggestion_tokens = _word_tokens(suggested_fix.casefold())
    return bool(issue_tokens) and issue_tokens == suggestion_tokens


def _adds_coordination_comma(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_norm = _normalized_text(issue_excerpt)
    suggestion_norm = _normalized_text(suggested_fix)
    if not issue_norm or not suggestion_norm or not _punctuation_only_change(issue_excerpt, suggested_fix):
        return False
    return any(
        marker in suggestion_norm and marker not in issue_norm
        for marker in {", e ", ", ou ", ", nem "}
    )


def _is_demonstrative_swap(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_tokens = _word_tokens(issue_excerpt.casefold())
    suggestion_tokens = _word_tokens(suggested_fix.casefold())
    if issue_tokens == suggestion_tokens or len(issue_tokens) != len(suggestion_tokens):
        return False
    allowed_swaps = {
        ("esse", "este"),
        ("este", "esse"),
        ("essa", "esta"),
        ("esta", "essa"),
        ("esses", "estes"),
        ("estes", "esses"),
        ("essas", "estas"),
        ("estas", "essas"),
        ("isso", "isto"),
        ("isto", "isso"),
    }
    diffs = [(left, right) for left, right in zip(issue_tokens, suggestion_tokens) if left != right]
    return len(diffs) == 1 and diffs[0] in allowed_swaps


def _drops_article_before_possessive(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_tokens = _word_tokens(issue_excerpt.casefold())
    suggestion_tokens = _word_tokens(suggested_fix.casefold())
    if len(issue_tokens) != len(suggestion_tokens) + 1:
        return False
    possessives = {"seu", "sua", "seus", "suas"}
    articles = {"o", "a", "os", "as"}
    for idx in range(len(issue_tokens) - 1):
        if issue_tokens[idx] in articles and issue_tokens[idx + 1] in possessives:
            candidate = issue_tokens[:idx] + issue_tokens[idx + 1 :]
            if candidate == suggestion_tokens:
                return True
    return False


def _removes_diacritic_only_word(issue_excerpt: str, suggested_fix: str) -> bool:
    issue = (issue_excerpt or "").strip()
    suggestion = (suggested_fix or "").strip()
    if not issue or not suggestion or " " in issue or " " in suggestion:
        return False
    if len(issue) < 5 or len(suggestion) < 5:
        return False
    if _ascii_fold(issue).casefold() != _ascii_fold(suggestion).casefold():
        return False
    return _diacritic_count(issue) > _diacritic_count(suggestion) and issue.casefold() != suggestion.casefold()


def _introduces_plural_copula_for_singular_head(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_norm = f" {_normalized_text(issue_excerpt)} "
    suggestion_norm = f" {_normalized_text(suggested_fix)} "
    if not issue_norm.strip() or not suggestion_norm.strip():
        return False
    return issue_norm.lstrip().startswith(("o ", "a ")) and " é " in issue_norm and " são " in suggestion_norm


def _looks_like_full_reference_rewrite(source_text: str, suggested_fix: str) -> bool:
    source_tokens = _word_tokens(_normalized_text(source_text))
    suggestion_tokens = _word_tokens(_normalized_text(suggested_fix))
    if len(source_tokens) < 12 or len(suggestion_tokens) < 12:
        return False
    shared = len(set(source_tokens) & set(suggestion_tokens))
    overlap = shared / max(len(set(source_tokens)), len(set(suggestion_tokens)))
    return overlap >= 0.75 and abs(len(source_tokens) - len(suggestion_tokens)) <= 6


def _is_reference_missing_data_speculation(message: str, suggested_fix: str) -> bool:
    blob = _folded_text(" ".join([message or "", suggested_fix or ""]))
    if not blob:
        return False

    uncertainty_tokens = {
        "incomplet",
        "ambigu",
        "hibrid",
        "nao identifica claramente",
        "nao apresenta claramente",
        "sem o local",
        "sem local",
        "sem a editora",
        "sem local/editora",
        "sem o local/editora",
    }
    metadata_tokens = {
        "local",
        "editora",
        "instituicao",
        "tipo",
        "serie",
        "doi",
        "volume",
        "numero",
        "fasciculo",
        "paginacao",
        "paginacao",
        "periodico",
        "cidade",
        "data",
    }
    action_tokens = {
        "completar",
        "incluir",
        "inserir",
        "reestruturar",
        "conferir no documento-fonte",
        "conferir na fonte",
        "se disponivel",
        "se essa informacao constar",
        "usando os dados",
    }

    has_uncertainty = any(token in blob for token in uncertainty_tokens)
    has_metadata = any(token in blob for token in metadata_tokens)
    has_action = any(token in blob for token in action_tokens)
    return has_metadata and (has_uncertainty or has_action)


def _is_grammar_rewrite_or_regency_comment(message: str, suggested_fix: str) -> bool:
    blob = _folded_text(" ".join([message or "", suggested_fix or ""]))
    if not blob:
        return False
    if any(token in blob for token in {"repeticao", "redundanc", "duplicacao local", "melhor formulacao"}):
        return True
    return any(
        token in blob
        for token in {
            "transitivo direto",
            "regencia verbal",
            "regencia nominal",
            "colocacao pronominal",
            "nao exige a preposicao",
            "exige a preposicao em",
            "exige a preposicao de",
        }
    )


def _comment_key(item: AgentComment) -> tuple[str, str, int | None, str, str, str, bool, str]:
    return (
        item.agent,
        item.category,
        item.paragraph_index,
        (item.message or "").strip(),
        (item.issue_excerpt or "").strip(),
        (item.suggested_fix or "").strip(),
        item.auto_apply,
        (item.format_spec or "").strip(),
    )


def _comment_review_key(paragraph_index: int | None, issue_excerpt: str, suggested_fix: str) -> tuple[int | None, str, str]:
    return (
        paragraph_index,
        (issue_excerpt or "").strip(),
        (suggested_fix or "").strip(),
    )


def _dedupe_comments(items: list[AgentComment]) -> list[AgentComment]:
    out: list[AgentComment] = []
    seen: set[tuple[str, str, int | None, str, str, str, bool, str]] = set()
    for item in items:
        key = _comment_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _heuristic_grammar_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    patterns: list[tuple[re.Pattern[str], str, str]] = [
        (
            re.compile(r"\bbenef[ií]cios monet[aá]rio\b", re.IGNORECASE),
            "Concordância",
            "benefícios monetários",
        ),
        (
            re.compile(r"\bque assenta o acesso\b", re.IGNORECASE),
            "Concordância",
            "que assentam o acesso",
        ),
        (
            re.compile(r"\bgrupos éticos\b", re.IGNORECASE),
            "Ortografia",
            "grupos étnicos",
        ),
    ]

    comments: list[AgentComment] = []
    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)):
            continue
        if _ref_block_type(refs[idx]) in {"direct_quote", "reference_entry"}:
            continue
        source_text = chunks[idx] or ""
        for pattern, category, replacement in patterns:
            match = pattern.search(source_text)
            if not match:
                continue
            issue_excerpt = match.group(0)
            if replacement.casefold() == issue_excerpt.casefold():
                continue
            comments.append(
                AgentComment(
                    agent="gramatica_ortografia",
                    category=category,
                    message="Há um erro ortográfico neste fragmento." if category == "Ortografia" else "A concordância está incorreta neste fragmento.",
                    paragraph_index=idx,
                    issue_excerpt=issue_excerpt,
                    suggested_fix=replacement,
                )
            )

        if re.search(r"\bo exerc[ií]cio realizado sustenta\b", source_text, re.IGNORECASE):
            match = re.search(r"\be sugerem\b", source_text, re.IGNORECASE)
            if match:
                comments.append(
                    AgentComment(
                        agent="gramatica_ortografia",
                        category="Concordância",
                        message="A concordância verbal está incorreta neste fragmento.",
                        paragraph_index=idx,
                        issue_excerpt=match.group(0),
                        suggested_fix="e sugere",
                    )
                )

    return comments


def _heuristic_synopsis_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        if _ref_block_type(refs[idx]) != "abstract_body":
            continue
        text = (chunks[idx] or "").strip()
        if not text:
            continue
        if _count_words(text) > 250:
            comments.append(
                AgentComment(
                    agent="sinopse_abstract",
                    category="Extensão",
                    message="Este resumo excede o limite de 250 palavras.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix="Reduzir o resumo para no máximo 250 palavras.",
                )
            )
        if _ref_align(refs[idx]) == "justify":
            continue
        comments.append(
            AgentComment(
                agent="sinopse_abstract",
                category="Formatação",
                message="O abstract deve estar justificado, mas este parágrafo está com outro alinhamento.",
                paragraph_index=idx,
                issue_excerpt=text,
                suggested_fix="Justificar o parágrafo do abstract.",
            )
        )
    return comments


def _heuristic_reference_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)):
            continue
        if _ref_block_type(refs[idx]) != "reference_entry":
            continue
        source_text = chunks[idx] or ""
        if not source_text.strip():
            continue

        leading_year_match = re.search(r"\((?P<year>(?:19|20)\d{2})\)", source_text)
        trailing_year_matches = list(re.finditer(r"\b(?:19|20)\d{2}\b", source_text))
        if leading_year_match and trailing_year_matches:
            leading_year = leading_year_match.group("year")
            trailing_year = trailing_year_matches[-1].group(0)
            if leading_year != trailing_year and trailing_year_matches[-1].start() > len(source_text) * 0.45:
                year_fragment = trailing_year
                prefix = source_text[max(0, trailing_year_matches[-1].start() - 8) : trailing_year_matches[-1].start()]
                if "," in prefix:
                    year_fragment = prefix.split(",")[-1].strip() + trailing_year
                comments.append(
                    AgentComment(
                        agent="referencias",
                        category="inconsistency",
                        message="Há inconsistência de ano nesta referência.",
                        paragraph_index=idx,
                        issue_excerpt=year_fragment,
                        suggested_fix=year_fragment.replace(trailing_year, leading_year),
                    )
                )

        glued_match = re.search(r"((?:19|20)\d{2})\.([A-ZÁ-Ú])", source_text)
        if glued_match:
            comments.append(
                AgentComment(
                    agent="referencias",
                    category="inconsistency",
                    message="Há duas referências coladas neste ponto.",
                    paragraph_index=idx,
                    issue_excerpt=glued_match.group(0),
                    suggested_fix=f"{glued_match.group(1)}. {glued_match.group(2)}",
                )
            )

        duplicated_place_match = re.search(
            r"(?P<place>[A-ZÀ-ÿ][^:.;]{2,60})\s*:\s*(?P=place),\s*(?P<year>(?:19|20)\d{2})",
            source_text,
        )
        if duplicated_place_match:
            repeated_fragment = duplicated_place_match.group(0).strip()
            comments.append(
                AgentComment(
                    agent="referencias",
                    category="inconsistency",
                    message="Há duplicação de local e editora no trecho final da referência.",
                    paragraph_index=idx,
                    issue_excerpt=repeated_fragment,
                    suggested_fix="Revisar a editora no trecho final, pois local e editora foram repetidos.",
                )
            )

        page_match = re.search(r"\bp\.(\d)", source_text)
        if page_match:
            issue_excerpt = source_text[page_match.start() : page_match.start() + 8].rstrip(" ,.;:")
            comments.append(
                AgentComment(
                    agent="referencias",
                    category="inconsistency",
                    message="A paginação está sem espaço após `p.`.",
                    paragraph_index=idx,
                    issue_excerpt=issue_excerpt,
                    suggested_fix=issue_excerpt.replace("p.", "p. ", 1),
                )
            )

        if "disponível em:" in _normalized_text(source_text) and "acesso em:" not in _normalized_text(source_text):
            url_fragment = source_text[source_text.casefold().find("disponível em:") :].strip()
            comments.append(
                AgentComment(
                    agent="referencias",
                    category="inconsistency",
                    message="A referência online informa a URL, mas não traz `Acesso em:` ao final.",
                    paragraph_index=idx,
                    issue_excerpt=url_fragment,
                    suggested_fix="Inserir `Acesso em:` com a data de consulta após a URL.",
                )
            )

        stripped_source = source_text.rstrip()
        if stripped_source and stripped_source[-1] not in ".!?":
            tail_fragment = stripped_source[-80:].strip()
            if re.search(r"\b(?:19|20)\d{2}$", stripped_source) or re.search(r"\bacesso em:\s*\d{1,2}\s+\w+\.\s+\d{4}$", _normalized_text(stripped_source)):
                comments.append(
                    AgentComment(
                        agent="referencias",
                        category="inconsistency",
                        message="A referência termina sem ponto final.",
                        paragraph_index=idx,
                        issue_excerpt=tail_fragment,
                        suggested_fix=f"{tail_fragment}.",
                    )
                )

    return comments


def _reference_citation_key(author_raw: str, year_raw: str) -> tuple[str, str] | None:
    author = _ascii_fold((author_raw or "").strip()).casefold()
    year = (year_raw or "").strip().casefold()
    if not author or not year:
        return None
    author = re.split(r"\s+(?:et\s+al\.?|e|and|&)\b", author, maxsplit=1)[0].strip()
    tokens = re.findall(r"[a-z0-9]+", author)
    if not tokens:
        return None
    if tokens[0] in {"de", "da", "do", "das", "dos", "e", "and", "et"}:
        return None
    return tokens[0], year


def _reference_citation_label(author_raw: str, year_raw: str) -> str:
    author = re.split(r"\s+(?:et\s+al\.?|e|and|&)\b", (author_raw or "").strip(), maxsplit=1)[0].strip()
    if not author:
        author = (author_raw or "").strip()
    return f"{author} ({(year_raw or '').strip()})".strip()


def _reference_body_citation_mentions(
    chunks: list[str], refs: list[str], body_limit: int
) -> list[tuple[int, str, tuple[str, str], str]]:
    mentions: list[tuple[int, str, tuple[str, str], str]] = []
    seen: set[tuple[int, str, tuple[str, str], str]] = set()

    def add_mention(idx: int, excerpt: str, author_raw: str, year_raw: str) -> None:
        key = _reference_citation_key(author_raw, year_raw)
        if key is None:
            return
        display = _reference_citation_label(author_raw, year_raw)
        mention = (idx, (excerpt or "").strip(), key, display)
        if mention in seen:
            return
        seen.add(mention)
        mentions.append(mention)

    for idx, (chunk, ref) in enumerate(zip(chunks[:body_limit], refs[:body_limit])):
        if _ref_block_type(ref) in {"reference_entry", "reference_heading", "caption", "table_cell"}:
            continue
        text = chunk or ""

        for match in re.finditer(r"\b([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’`\-]+(?:\s+et\s+al\.?)?)\s*\((\d{4}[a-z]?)\)", text):
            add_mention(idx, match.group(0), match.group(1), match.group(2))

        for parenthetical_match in re.finditer(r"\(([^)]*\d{4}[a-z]?[^)]*)\)", text):
            for segment in re.split(r";", parenthetical_match.group(1)):
                piece = segment.strip()
                match = re.search(r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’`\-]+)(?:[^0-9()]*)[, ]\s*(\d{4}[a-z]?)", piece)
                if match:
                    add_mention(idx, piece, match.group(1), match.group(2))

    return mentions


def _reference_body_citation_keys(chunks: list[str], refs: list[str], body_limit: int) -> set[tuple[str, str]]:
    return {key for _, _, key, _ in _reference_body_citation_mentions(chunks, refs, body_limit)}


def _reference_entry_key(text: str) -> tuple[str, str] | None:
    source = (text or "").strip()
    if not source:
        return None
    year_matches = re.findall(r"\b(?:19|20)\d{2}[a-z]?\b", source, flags=re.IGNORECASE)
    if not year_matches:
        return None
    year = year_matches[0].casefold()

    if "," in source:
        author_part = source.split(",", 1)[0]
    elif "." in source:
        author_part = source.split(".", 1)[0]
    else:
        author_part = source

    author_part = _ascii_fold(author_part).casefold().strip()
    tokens = re.findall(r"[a-z0-9]+", author_part)
    if not tokens:
        return None
    return tokens[0], year


def _reference_entry_label(text: str) -> str:
    source = (text or "").strip()
    if not source:
        return ""
    key = _reference_entry_key(source)
    if key is None:
        return source[:80]
    author_raw = source.split(",", 1)[0].strip() if "," in source else source.split(".", 1)[0].strip()
    return f"{author_raw} ({key[1]})"


def _summarize_reference_labels(labels: list[str], max_items: int = 6) -> str:
    cleaned = [label.strip() for label in labels if label.strip()]
    if not cleaned:
        return ""
    if len(cleaned) <= max_items:
        return "; ".join(cleaned)
    head = "; ".join(cleaned[:max_items])
    return f"{head}; e mais {len(cleaned) - max_items} item(ns)"


def _reference_body_format_comments(
    chunks: list[str], refs: list[str], body_limit: int, batch_indexes: list[int]
) -> list[AgentComment]:
    comments: list[AgentComment] = []
    pattern = re.compile(r"\b([A-ZÀ-Ý][A-Za-zÀ-ÿ'’`\-]+)\((\d{4}[a-z]?)\)")
    batch_set = set(batch_indexes)

    for idx, (chunk, ref) in enumerate(zip(chunks[:body_limit], refs[:body_limit])):
        if idx not in batch_set:
            continue
        if _ref_block_type(ref) in {"reference_entry", "reference_heading", "caption", "table_cell"}:
            continue
        text = chunk or ""
        for match in pattern.finditer(text):
            excerpt = match.group(0)
            comments.append(
                AgentComment(
                    agent="referencias",
                    category="citation_format",
                    message="A chamada autor-data está sem espaço entre o sobrenome e o ano.",
                    paragraph_index=idx,
                    issue_excerpt=excerpt,
                    suggested_fix=f"{match.group(1)} ({match.group(2)})",
                )
            )

    return comments


def _find_reference_citation_indexes(chunks: list[str], refs: list[str], body_limit: int) -> list[int]:
    indexes: list[int] = []
    narrative_re = re.compile(r"\b[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’`\-]+(?:\s+et\s+al\.?)?\s*\(\d{4}[a-z]?\)")
    parenthetical_re = re.compile(r"\([^)]*[A-Za-zÀ-ÿ][^)]*\b\d{4}[a-z]?[^)]*\)")

    for idx, (chunk, ref) in enumerate(zip(chunks[:body_limit], refs[:body_limit])):
        if _ref_block_type(ref) in {"reference_entry", "reference_heading", "caption", "table_cell"}:
            continue
        text = chunk or ""
        if narrative_re.search(text) or parenthetical_re.search(text):
            indexes.append(idx)

    return indexes


def _heuristic_reference_global_comments(chunks: list[str], refs: list[str], batch_indexes: list[int]) -> list[AgentComment]:
    reference_heading_idx = next((idx for idx, ref in enumerate(refs) if _ref_block_type(ref) == "reference_heading"), None)
    if reference_heading_idx is None:
        return []

    body_limit = reference_heading_idx
    citation_mentions = _reference_body_citation_mentions(chunks, refs, body_limit)
    citation_keys = {key for _, _, key, _ in citation_mentions}
    comments = _reference_body_format_comments(chunks, refs, body_limit, batch_indexes=batch_indexes)
    batch_set = set(batch_indexes)

    reference_entries: list[tuple[int, tuple[str, str], str]] = []
    for idx, (chunk, ref) in enumerate(zip(chunks[reference_heading_idx + 1 :], refs[reference_heading_idx + 1 :]), start=reference_heading_idx + 1):
        if _ref_block_type(ref) != "reference_entry":
            continue
        entry_key = _reference_entry_key(chunk)
        if entry_key is None:
            continue
        reference_entries.append((idx, entry_key, _reference_entry_label(chunk)))

    reference_keys = {key for _, key, _ in reference_entries}
    uncited_labels = [label for _, key, label in reference_entries if key not in citation_keys]
    missing_mentions = [(idx, excerpt, key, display) for idx, excerpt, key, display in citation_mentions if key not in reference_keys]
    missing_labels = [display for _, _, _, display in missing_mentions]

    for idx, excerpt, _, display in missing_mentions:
        if idx not in batch_set:
            continue
        comments.append(
            AgentComment(
                agent="referencias",
                category="citation_match",
                paragraph_index=idx,
                message="Esta citação no corpo do texto não tem correspondência clara na lista de referências.",
                issue_excerpt=excerpt,
                suggested_fix=f"Incluir ou revisar a referência correspondente a {display} na lista final.",
            )
        )

    if uncited_labels and reference_heading_idx in batch_set:
        comments.append(
            AgentComment(
                agent="referencias",
                category="inconsistency",
                paragraph_index=reference_heading_idx,
                message="Há referências na lista que não foram localizadas nas citações do corpo do texto.",
                issue_excerpt=chunks[reference_heading_idx],
                suggested_fix=f"Verificar estas obras: {_summarize_reference_labels(uncited_labels)}.",
            )
        )

    if missing_labels and reference_heading_idx in batch_set:
        comments.append(
            AgentComment(
                agent="referencias",
                category="inconsistency",
                paragraph_index=reference_heading_idx,
                message="Há citações no corpo do texto sem correspondência clara na lista de referências.",
                issue_excerpt=chunks[reference_heading_idx],
                suggested_fix=f"Incluir ou revisar as referências correspondentes a: {_summarize_reference_labels(missing_labels)}.",
            )
        )

    return comments


def _heuristic_table_figure_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    source_prefixes = ("fonte:", "elaboração:", "elaboracao:")

    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        if _ref_block_type(refs[idx]) != "caption":
            continue

        text = (chunks[idx] or "").strip()
        if not text:
            continue

        caption_match = re.match(r"^(?P<label>tabela|figura|quadro|gr[aá]fico)\s+(?P<number>\d+)\s*:\s*(?P<title>.+)$", text, re.IGNORECASE)
        if caption_match:
            label = caption_match.group("label").upper()
            number = caption_match.group("number")
            title = caption_match.group("title").strip()
            comments.append(
                AgentComment(
                    agent="tabelas_figuras",
                    category="Legenda",
                    message="Na legenda, o identificador deve ficar na primeira linha e o título descritivo na linha abaixo.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix=f"Separar em duas linhas: `{label} {number}` na primeira linha e `{title}` na linha abaixo.",
                )
            )

        found_source = False
        saw_block_content = False
        for next_idx in range(idx + 1, len(chunks)):
            next_text = (chunks[next_idx] or "").strip()
            next_norm = _normalized_text(next_text)
            next_type = _ref_block_type(refs[next_idx]) if next_idx < len(refs) else ""

            if next_norm.startswith(source_prefixes):
                found_source = True
                break
            if next_type == "table_cell":
                saw_block_content = True
                continue
            if next_type in {"caption", "heading", "reference_heading"}:
                break
            if next_type in {"paragraph", "reference_entry"}:
                break
            if not next_text:
                continue
            break

        if not found_source and (saw_block_content or caption_match):
            comments.append(
                AgentComment(
                    agent="tabelas_figuras",
                    category="Fonte",
                    message="O bloco está sem uma linha de fonte ou elaboração logo abaixo da legenda.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix="Adicionar uma linha própria com `Fonte:` ou `Elaboração:` abaixo do bloco.",
                )
            )

    return comments


def _is_same_top_level_heading(ref: str) -> bool:
    ref_norm = _normalized_text(ref)
    return "tipo=heading" in ref_norm and "heading 1" in ref_norm


def _is_final_section_heading(text: str) -> bool:
    folded = _ascii_fold(_strip_heading_prefix(text)).casefold()
    return folded in {"consideracoes finais", "conclusao", "conclusoes", "referencias", "referencias bibliograficas"}


def _heuristic_structure_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    intro_idx = next((idx for idx, chunk in enumerate(chunks) if _is_intro_heading(chunk)), None)
    if intro_idx is None:
        return []

    top_level_indexes = [
        idx
        for idx, ref in enumerate(refs)
        if idx >= intro_idx and (_is_same_top_level_heading(ref) or _ref_block_type(ref) == "reference_heading")
    ]
    ordinal_by_index = {idx: pos for pos, idx in enumerate(top_level_indexes, start=1)}
    numbered_indexes = {
        idx
        for idx in top_level_indexes
        if _ref_has_numbering(refs[idx]) or bool(_HEADING_NUMBER_PREFIX_RE.match((chunks[idx] or "").strip()))
    }
    require_numbering = bool(numbered_indexes)

    comments: list[AgentComment] = []
    target_indexes = sorted(set(batch_indexes) | set(top_level_indexes))
    for idx in target_indexes:
        if idx not in ordinal_by_index or not (0 <= idx < len(chunks)):
            continue
        text = (chunks[idx] or "").strip()
        is_numbered = bool(_HEADING_NUMBER_PREFIX_RE.match(text))
        if is_numbered:
            continue
        if require_numbering:
            comments.append(
                AgentComment(
                    agent="estrutura",
                    category="Numeração",
                    message="Este título de mesmo nível está sem a numeração usada nas demais seções.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix=f"{ordinal_by_index[idx]}. {text}",
                )
            )

    return comments


def _heuristic_typography_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        ref = refs[idx]
        if _ref_block_type(ref) != "heading":
            continue
        style_name = _ref_style_name(ref).casefold()
        if style_name not in {"heading 1", "heading 2", "heading 3"}:
            continue
        if not _ref_has_flag(ref, "italico"):
            continue
        text = (chunks[idx] or "").strip()
        if not text:
            continue
        comments.append(
            AgentComment(
                agent="tipografia",
                category="inconsistency",
                message="Neste subtítulo, o itálico destoa do padrão tipográfico do mesmo nível.",
                paragraph_index=idx,
                issue_excerpt=text,
                suggested_fix="Remover itálico do título.",
                auto_apply=False,
                format_spec="italic=false",
            )
        )
    return comments


def _find_metadata_like_indexes(chunks: list[str], refs: list[str], limit: int = 18) -> list[int]:
    metadata_rx = re.compile(
        r"\b(doi|jel|palavras-chave|cidade|editora|edição|edição:|ano|autor(?:es)?|afilia|institui|produto editorial)\b",
        re.IGNORECASE,
    )
    allowed_types = {"heading", "paragraph"}
    picked: list[int] = []
    for idx, chunk in enumerate(chunks[: min(limit, len(chunks))]):
        if _ref_block_type(refs[idx]) not in allowed_types:
            continue
        if idx <= 12 or metadata_rx.search(chunk):
            picked.append(idx)
    return picked


def _expand_neighbors(indexes: list[int], total: int, radius: int = 1) -> list[int]:
    expanded: set[int] = set()
    for idx in indexes:
        for candidate in range(max(0, idx - radius), min(total, idx + radius + 1)):
            expanded.add(candidate)
    return sorted(expanded)


def _has_neighbor_with_prefix(paragraph_index: int, refs: list[str], chunks: list[str], prefixes: tuple[str, ...], radius: int = 2) -> bool:
    for candidate in range(max(0, paragraph_index - radius), min(len(chunks), paragraph_index + radius + 1)):
        text = (chunks[candidate] or "").strip().casefold()
        if any(text.startswith(prefix.casefold()) for prefix in prefixes):
            return True
    return False


def _find_excerpt_index(excerpt: str, candidate_indexes: list[int], chunks: list[str]) -> int | None:
    needle = _normalized_text(excerpt)
    if not needle:
        return None

    for idx in candidate_indexes:
        if 0 <= idx < len(chunks) and needle in _normalized_text(chunks[idx]):
            return idx
    return None


def _remap_comment_index(comment: AgentComment, batch_indexes: list[int], chunks: list[str]) -> AgentComment:
    paragraph_index = comment.paragraph_index

    if paragraph_index is None:
        paragraph_index = _find_excerpt_index(comment.issue_excerpt, batch_indexes, chunks)
        if paragraph_index is None and batch_indexes:
            paragraph_index = batch_indexes[0]
    elif paragraph_index not in batch_indexes and 0 <= paragraph_index < len(batch_indexes):
        paragraph_index = batch_indexes[paragraph_index]

    if paragraph_index is not None and batch_indexes and paragraph_index not in batch_indexes:
        matched = _find_excerpt_index(comment.issue_excerpt, batch_indexes, chunks)
        if matched is not None:
            paragraph_index = matched

    matched = _find_excerpt_index(comment.issue_excerpt, batch_indexes, chunks)
    if matched is not None:
        paragraph_index = matched

    return AgentComment(
        agent=comment.agent,
        category=comment.category,
        message=comment.message,
        paragraph_index=paragraph_index,
        issue_excerpt=comment.issue_excerpt,
        suggested_fix=comment.suggested_fix,
        auto_apply=comment.auto_apply,
        format_spec=comment.format_spec,
        review_status=comment.review_status,
        approved_text=comment.approved_text,
        reviewer_note=comment.reviewer_note,
    )


def _limit_auto_apply(comment: AgentComment) -> AgentComment:
    if not comment.auto_apply:
        return comment
    return AgentComment(
        agent=comment.agent,
        category=comment.category,
        message=comment.message,
        paragraph_index=comment.paragraph_index,
        issue_excerpt=comment.issue_excerpt,
        suggested_fix=comment.suggested_fix,
        auto_apply=False,
        format_spec=comment.format_spec,
        review_status=comment.review_status,
        approved_text=comment.approved_text,
        reviewer_note=comment.reviewer_note,
    )


def _should_keep_comment(comment: AgentComment, agent: str, chunks: list[str], refs: list[str]) -> bool:
    if not (comment.message or "").strip():
        return False

    if comment.issue_excerpt and comment.suggested_fix and not comment.auto_apply:
        if _normalized_text(comment.issue_excerpt) == _normalized_text(comment.suggested_fix):
            return False

    ref = ""
    if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(refs):
        ref = refs[comment.paragraph_index]
    block_type = _ref_block_type(ref)

    if agent == "estrutura" and block_type in {
        "direct_quote",
        "reference_entry",
        "table_cell",
    }:
        return False
    if agent == "estrutura" and block_type == "caption":
        source_text = ""
        if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(chunks):
            source_text = chunks[comment.paragraph_index]
        issue_text = comment.issue_excerpt or source_text
        if _is_illustration_caption(issue_text) or _is_illustration_caption(source_text):
            structure_msg = _normalized_text(comment.message)
            structure_fix = _normalized_text(comment.suggested_fix)
            if any(token in structure_msg for token in {"seção", "secao", "subseção", "subsecao", "numerar a seção", "numerar a secao"}):
                return False
            if any(token in structure_fix for token in {"seção", "secao"}):
                return False
    if agent == "estrutura" and block_type not in {"heading", "caption"}:
        structure_msg = _normalized_text(comment.message)
        if any(token in structure_msg for token in {"não está numerada", "deveria ser numerada", "numerar a seção"}):
            return False
    if agent == "estrutura" and block_type == "caption":
        structure_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        if _is_illustration_caption(comment.issue_excerpt or "") and any(
            token in structure_blob for token in {"secao", "seÃ§Ã£o", "seã§ã£o", "subsecao", "subseÃ§Ã£o", "subseã§ã£o"}
        ):
            return False
    if agent == "estrutura" and block_type == "heading" and comment.issue_excerpt:
        if not _matches_whole_paragraph(comment, chunks):
            return False
    if agent == "estrutura" and block_type != "heading":
        structure_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        title_tokens = {
            "titulo",
            "título",
            "secao",
            "seÃ§Ã£o",
            "subsecao",
            "subseÃ§Ã£o",
            "numerada",
            "numerar",
        }
        if comment.issue_excerpt and not _matches_whole_paragraph(comment, chunks):
            if any(token in structure_blob for token in title_tokens):
                return False
    if agent == "estrutura" and comment.auto_apply:
        if not _is_safe_structure_auto_apply(comment, chunks):
            return False
    if agent == "estrutura":
        structure_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        issue_core = _normalized_text(_strip_heading_prefix(comment.issue_excerpt or ""))
        fix_core = _normalized_text(_strip_heading_prefix(comment.suggested_fix or ""))
        if issue_core and comment.suggested_fix and issue_core not in fix_core:
            return False
        if re.search(r"\bpar[aá]grafo\s+\d+\b", structure_blob):
            return False
        if not comment.auto_apply and any(token in structure_blob for token in {"alterar a numeracao", "alterar a numeração", "por exemplo", "secao anterior", "seção anterior"}):
            return False
        if not comment.auto_apply and comment.suggested_fix and _heading_word_count(comment.suggested_fix) > 8:
            return False
        if any(token in structure_blob for token in {"não possui subseções", "nao possui subsecoes", "deveria ter", "adicionar subseções", "adicionar subsecoes"}):
            return False

    if agent == "metadados":
        if block_type not in {"heading", "paragraph"}:
            return False
        if isinstance(comment.paragraph_index, int) and comment.paragraph_index >= 18:
            return False
        metadata_excerpt = _normalized_text(comment.issue_excerpt)
        metadata_message = _normalized_text(comment.message)
        if any(term in metadata_excerpt for term in {"não fornecido", "nao fornecido"}) and isinstance(comment.paragraph_index, int) and comment.paragraph_index > 12:
            return False
        if "placeholder" in metadata_message and "xxxxx" not in metadata_excerpt and "<td" not in metadata_excerpt:
            return False

    if agent == "tabelas_figuras":
        if not (comment.issue_excerpt or "").strip():
            return False
        issue_excerpt = _normalized_text(comment.issue_excerpt)
        table_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
        if block_type == "table_cell" and any(token in table_blob for token in {"subtitulo", "subtítulo", "fonte", "identificador", "legenda"}):
            return False
        if block_type != "caption" and any(token in table_blob for token in {"subtitulo", "subtítulo", "fonte"}):
            return False
        if block_type == "caption" and re.match(r"^(tabela|figura|quadro|gr[aÃ¡]fico)\s+\d+[:\s]", issue_excerpt):
            if any(token in table_blob for token in {"identificador", "titulo", "título", "subtitulo", "subtítulo"}):
                if any(token in table_blob for token in {"mesma linha", "fundidos", "linha da legenda", "linha propria", "linha própria"}):
                    pass
                else:
                    return False
        if re.match(r"^(tabela|figura|quadro)\s+\d+", issue_excerpt):
            source_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
            if "fonte" in source_blob:
                if "abaixo do bloco" in source_blob or "linha propria" in source_blob or "linha própria" in source_blob:
                    pass
                else:
                    return False
        if "fonte" in _normalized_text(comment.message) and isinstance(comment.paragraph_index, int):
            if _has_neighbor_with_prefix(comment.paragraph_index, refs, chunks, ("Fonte:", "Elaboração:"), radius=2):
                return False
        if re.match(r"^(tabela|figura|quadro|gr[aÃ¡]fico)\s+\d+[:\s]", issue_excerpt) and any(
            token in table_blob for token in {"falta o identificador", "nao possui um identificador", "nÃ£o possui um identificador"}
        ):
            return False
        if comment.auto_apply and not _is_safe_text_normalization_auto_apply(comment, chunks):
            return False

    if agent == "tipografia":
        spec = _parse_format_spec(comment.format_spec)
        if not spec:
            return False
        if any(key not in _ALLOWED_TYPOGRAPHY_KEYS for key in spec):
            return False
        if not _is_relevant_typography_spec(spec):
            return False
        if comment.issue_excerpt and not _matches_whole_paragraph(comment, chunks):
            return False
        if block_type == "paragraph" and isinstance(comment.paragraph_index, int) and comment.paragraph_index >= 24:
            return False
        if block_type in {"reference_entry", "reference_heading"}:
            return False
        if block_type not in {"heading", "caption", "paragraph"}:
            return False
        tipografia_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
        if "alterar para '" in (comment.suggested_fix or "").casefold() or 'alterar para "' in (comment.suggested_fix or "").casefold():
            return False
        if any(token in _normalized_text(comment.suggested_fix) for token in {"reescrever", "substituir texto", "alterar conteúdo"}):
            return False

    if agent == "referencias" and block_type not in {"reference_entry", "reference_heading"}:
        if comment.category in {"citation_format", "citation_match"} and block_type in {"paragraph", "direct_quote", "list_item"}:
            pass
        else:
            return False
    if agent == "referencias" and comment.auto_apply:
        if not _is_safe_text_normalization_auto_apply(comment, chunks):
            return False
    if agent == "referencias" and isinstance(comment.paragraph_index, int):
        current = (chunks[comment.paragraph_index] or "").casefold()
        current_text = chunks[comment.paragraph_index] or ""
        raw_message = (comment.message or "").casefold()
        message_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
        suggestion_blob = _normalized_text(comment.suggested_fix)
        if any(token in message_blob for token in {"adicionar o titulo", "adicionar a pagina", "adicionar a paginacao", "adicionar o ano", "ano de publicacao", "verificar e corrigir o ano"}):
            return False
        if any(token in message_blob for token in {"falta de informacoes", "falta de informações", "adicionar informacoes", "adicionar informações"}):
            return False
        if "caixa baixa" in message_blob or "caixa alta" in message_blob:
            return False
        if any(token in message_blob for token in {"italico", "itálico", "negrito", "destaque grafico", "destaque gráfico"}):
            return False
        if any(token in message_blob for token in {"verificar", "confirmar", "informacoes suficientes", "informações suficientes"}) and _years_in_text(current_text):
            return False
        if any(token in message_blob for token in {"pontuacao final", "pontuação final", "ponto final", "pontuacao ao final", "pontuação ao final"}):
            if (current_text or "").rstrip().endswith((".", "!", "?")):
                return False
        if "in:" in current_text.casefold() and ("in:" in raw_message and ("uso incorreto" in raw_message or "inserir" in raw_message)):
            return False
        if "uso incorreto" in raw_message and "n." in raw_message:
            return False
        if "v." in raw_message and "espa" in raw_message and "volume" in raw_message:
            if "v." not in current_text:
                return False
        if ":" in raw_message and "espa" in raw_message:
            if not re.search(r":\S", comment.issue_excerpt or ""):
                return False
        if ("pontuação entre o título e a editora" in raw_message or "pontuacao entre o titulo e a editora" in _normalized_text(raw_message)):
            if "texto para discussão" in current_text.casefold() or "texto para discussao" in _normalized_text(current_text):
                return False
        if "titulo e a editora" in message_blob and "texto para discuss" in _normalized_text(current_text):
            return False
        if "n." in raw_message and "ponto" in raw_message:
            if re.search(r"\bn\.\s*\d+\s*,", current_text, re.IGNORECASE):
                return False
        if ("ponto final após o número" in raw_message or "ponto final apos o numero" in _normalized_text(raw_message)):
            if re.search(r"\bn\.\s*\d+\s*,", comment.issue_excerpt or "", re.IGNORECASE):
                return False
        if any(token in message_blob for token in {"titulo", "título", "autor", "ano", "periodico", "periódico"}) and _looks_like_full_reference_rewrite(current_text, comment.suggested_fix):
            return False
        if any(token in message_blob for token in {"titulo", "título"}) and re.search(r"\bpp?\.\s*\d", current_text):
            return False
        if _normalized_text(comment.suggested_fix) == _normalized_text(current_text):
            return False
        if any(token in message_blob for token in {"padrao de formataÃ§Ã£o", "padrao de formatacao", "padrÃ£o de formataÃ§Ã£o"}):
            return False
        if any(token in suggestion_blob for token in {"[ano]", "[local]", "[editora]"}) or "[" in (comment.suggested_fix or ""):
            return False
        source_text = current_text
        if "titulo" in message_blob and _looks_like_all_caps_title(source_text):
            return False
        if "ano" in _normalized_text(comment.category) or "ano" in _normalized_text(comment.message):
            current_years = _years_in_text(current_text)
            suggestion_years = _years_in_text(comment.suggested_fix)
            if current_years and suggestion_years and suggestion_years != current_years:
                return False
            if re.search(r"\b(19|20)\d{2}\b", current) and "alterar o ano" in _normalized_text(comment.suggested_fix):
                return False

    if agent == "conformidade_estilos":
        suggestion = (comment.suggested_fix or "").strip().upper()
        if not _matches_whole_paragraph(comment, chunks):
            return False
        allowed = _STYLE_BY_BLOCK_TYPE.get(block_type)
        if allowed and suggestion and suggestion not in allowed:
            return False
        if block_type == "paragraph" and suggestion in {"TITULO_1", "TÍTULO_1", "TITULO_2", "TÍTULO_2", "TITULO_3", "TÍTULO_3"}:
            return False

    if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(chunks):
        if agent == "sinopse_abstract":
            source_text = chunks[comment.paragraph_index] or ""
            synopsis_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
            if ("portugu" in synopsis_blob and "ingl" in synopsis_blob) or any(
                token in synopsis_blob for token in {"português e inglês", "portugues e ingles"}
            ):
                return False
            if any(
                token in synopsis_blob
                for token in {"nao inicia com letra maiuscula", "não inicia com letra maiúscula", "iniciar a frase com letra maiuscula", "iniciar a frase com letra maiúscula"}
            ):
                return False
            quoted_terms = _quoted_terms(" ".join([comment.message or "", comment.suggested_fix or ""]))
            issue_blob = _normalized_text(comment.issue_excerpt)
            if quoted_terms and not any(_normalized_text(term) in issue_blob for term in quoted_terms):
                return False
            word_limit = _extract_word_limit(" ".join([comment.message or "", comment.suggested_fix or ""]))
            if word_limit is not None:
                counted_text = comment.issue_excerpt or source_text
                if _count_words(counted_text) <= word_limit:
                    return False
            if block_type == "keywords_content":
                repetition_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
                if any(token in repetition_blob for token in {"repet", "redundan"}):
                    if not _has_repeated_keyword_entries(comment.issue_excerpt or source_text):
                        return False
        if comment.issue_excerpt:
            excerpt_ok = _find_excerpt_index(comment.issue_excerpt, [comment.paragraph_index], chunks)
            if excerpt_ok is None and agent in {"gramatica_ortografia", "referencias"}:
                return False
        if agent == "gramatica_ortografia":
            source_text = chunks[comment.paragraph_index] or ""
            grammar_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
            if block_type == "direct_quote":
                return False
            if block_type == "reference_entry":
                return False
            if _looks_like_quoted_excerpt(comment.issue_excerpt):
                return False
            excerpt = (comment.issue_excerpt or "").strip()
            if _contains_quote_marks(source_text) and excerpt and len(excerpt) >= max(120, int(len(source_text) * 0.65)):
                return False
            if "pontua" in grammar_blob and excerpt and len(excerpt) > 120:
                return False
            if "concord" in grammar_blob and excerpt and len(excerpt) > 120:
                return False
            if any(token in grammar_blob for token in {"clareza", "simplificada", "simplificar", "reestruturar", "reescr"}):
                return False
            if _adds_coordination_comma(excerpt or source_text, comment.suggested_fix):
                return False
            if _is_demonstrative_swap(excerpt or source_text, comment.suggested_fix):
                return False
            if _drops_article_before_possessive(excerpt or source_text, comment.suggested_fix):
                return False
            if _introduces_plural_copula_for_singular_head(excerpt or source_text, comment.suggested_fix):
                return False
            if "observam-se que" in _normalized_text(comment.suggested_fix):
                return False
            if _normalized_text(comment.suggested_fix) == _normalized_text(source_text):
                return False
            if _removes_terminal_period_only(comment.issue_excerpt or source_text, comment.suggested_fix):
                return False

    return True


def _tokenize_structure_text(value: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", (value or "").casefold())


def _is_safe_structure_auto_apply(comment: AgentComment, chunks: list[str]) -> bool:
    if not isinstance(comment.paragraph_index, int) or not (0 <= comment.paragraph_index < len(chunks)):
        return False
    issue = (comment.issue_excerpt or "").strip()
    suggestion = (comment.suggested_fix or "").strip()
    source = (chunks[comment.paragraph_index] or "").strip()
    if not issue or not suggestion or not source:
        return False
    if _normalized_text(issue) != _normalized_text(source):
        return False
    return _tokenize_structure_text(issue) == _tokenize_structure_text(suggestion) == _tokenize_structure_text(source)


def _is_safe_text_normalization_auto_apply(comment: AgentComment, chunks: list[str]) -> bool:
    if not isinstance(comment.paragraph_index, int) or not (0 <= comment.paragraph_index < len(chunks)):
        return False
    issue = (comment.issue_excerpt or "").strip()
    suggestion = (comment.suggested_fix or "").strip()
    source = (chunks[comment.paragraph_index] or "").strip()
    if not issue or not suggestion or not source:
        return False
    if _normalized_text(issue) != _normalized_text(source):
        return False
    return _tokenize_structure_text(issue) == _tokenize_structure_text(suggestion) == _tokenize_structure_text(source)


def _matches_whole_paragraph(comment: AgentComment, chunks: list[str]) -> bool:
    if not isinstance(comment.paragraph_index, int) or not (0 <= comment.paragraph_index < len(chunks)):
        return False
    issue = (comment.issue_excerpt or "").strip()
    source = (chunks[comment.paragraph_index] or "").strip()
    if not issue or not source:
        return False
    return _normalized_text(issue) == _normalized_text(source)


def _heuristic_comments_for_agent(agent: str, batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    if agent == "gramatica_ortografia":
        comments.extend(_heuristic_grammar_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "sinopse_abstract":
        comments.extend(_heuristic_synopsis_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "tabelas_figuras":
        comments.extend(_heuristic_table_figure_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "referencias":
        comments.extend(_heuristic_reference_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
        comments.extend(_heuristic_reference_global_comments(chunks=chunks, refs=refs, batch_indexes=batch_indexes))
    if agent == "estrutura":
        comments.extend(_heuristic_structure_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "tipografia":
        comments.extend(_heuristic_typography_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    return comments


def _basic_comment_rejection_reason(comment: AgentComment) -> str | None:
    if not (comment.message or "").strip():
        return "mensagem vazia"

    if comment.issue_excerpt and comment.suggested_fix and not comment.auto_apply:
        if _normalized_text(comment.issue_excerpt) == _normalized_text(comment.suggested_fix):
            return "sugestão idêntica ao trecho"
    return None


def _comment_rejection_reason(comment: AgentComment, agent: str, chunks: list[str], refs: list[str]) -> str | None:
    basic_reason = _basic_comment_rejection_reason(comment)
    if basic_reason is not None:
        return basic_reason

    if isinstance(comment.paragraph_index, int) and 0 <= comment.paragraph_index < len(chunks):
        source_text = chunks[comment.paragraph_index] or ""
        ref = refs[comment.paragraph_index] if comment.paragraph_index < len(refs) else ""
        block_type = _ref_block_type(ref)

        if agent == "sinopse_abstract":
            word_limit = _extract_word_limit(" ".join([comment.message or "", comment.suggested_fix or ""]))
            if word_limit is not None:
                counted_text = comment.issue_excerpt or source_text
                if _count_words(counted_text) <= word_limit:
                    return "alegação de limite de palavras não confirmada"
            if block_type == "keywords_content":
                repetition_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
                if any(token in repetition_blob for token in {"repet", "redundan"}):
                    if not _has_repeated_keyword_entries(comment.issue_excerpt or source_text):
                        return "alegação de repetição não confirmada"
        if agent == "gramatica_ortografia":
            excerpt = comment.issue_excerpt or source_text
            if _is_grammar_rewrite_or_regency_comment(comment.message, comment.suggested_fix):
                return "comentário gramatical de reescrita ou regência discutível"
            if _removes_diacritic_only_word(excerpt, comment.suggested_fix):
                return "remoção de acento não confirmada"
        if agent == "referencias" and block_type in {"reference_entry", "reference_heading"}:
            if _is_reference_missing_data_speculation(comment.message, comment.suggested_fix):
                return "completude bibliográfica sem evidência local"

    if not _should_keep_comment(comment, agent=agent, chunks=chunks, refs=refs):
        return "descartado por regra de verificação"
    return None


def _summarize_verification(decisions: list[VerificationDecision]) -> VerificationSummary:
    accepted_count = sum(1 for decision in decisions if decision.accepted)
    rejected_count = sum(1 for decision in decisions if not decision.accepted)
    return VerificationSummary(
        decisions=decisions[:],
        accepted_count=accepted_count,
        rejected_count=rejected_count,
    )


def _verify_batch_comments(
    comments: list[AgentComment],
    agent: str,
    batch_indexes: list[int],
    chunks: list[str],
    refs: list[str],
    existing_comments: list[AgentComment] | None = None,
    batch_index: int | None = None,
) -> tuple[list[AgentComment], list[VerificationDecision]]:
    candidates: list[tuple[str, AgentComment]] = []
    for comment in comments:
        remapped = _limit_auto_apply(_remap_comment_index(comment, batch_indexes=batch_indexes, chunks=chunks))
        candidates.append(("llm", remapped))
    for comment in _heuristic_comments_for_agent(agent=agent, batch_indexes=batch_indexes, chunks=chunks, refs=refs):
        candidates.append(("heuristic", comment))

    accepted: list[AgentComment] = []
    decisions: list[VerificationDecision] = []
    seen_existing = {_comment_key(item) for item in (existing_comments or [])}
    seen_batch: set[tuple[str, str, int | None, str, str, str, bool, str]] = set()

    for source, candidate in candidates:
        key = _comment_key(candidate)
        if key in seen_existing or key in seen_batch:
            decisions.append(
                VerificationDecision(
                    comment=candidate,
                    accepted=False,
                    reason="comentário duplicado",
                    source=source,
                    batch_index=batch_index,
                )
            )
            continue

        reason = _basic_comment_rejection_reason(candidate)
        if reason is None and source == "llm":
            reason = _comment_rejection_reason(candidate, agent=agent, chunks=chunks, refs=refs)
        if reason is not None:
            decisions.append(
                VerificationDecision(
                    comment=candidate,
                    accepted=False,
                    reason=reason,
                    source=source,
                    batch_index=batch_index,
                )
            )
            continue

        accepted.append(candidate)
        seen_batch.add(key)
        decisions.append(
            VerificationDecision(
                comment=candidate,
                accepted=True,
                reason="aceito",
                source=source,
                batch_index=batch_index,
            )
        )

    return accepted, decisions


def _format_batch_status(status: str, decisions: list[VerificationDecision]) -> str:
    summary = _summarize_verification(decisions)
    base = (status or "").strip()
    suffix = f"verif: {summary.accepted_count} aceitos, {summary.rejected_count} rejeitados"
    return f"{base} | {suffix}" if base else suffix


def _normalize_batch_comments(
    comments: list[AgentComment],
    agent: str,
    batch_indexes: list[int],
    chunks: list[str],
    refs: list[str],
) -> list[AgentComment]:
    accepted, _ = _verify_batch_comments(
        comments=comments,
        agent=agent,
        batch_indexes=batch_indexes,
        chunks=chunks,
        refs=refs,
        existing_comments=[],
    )
    return accepted


_REVIEWER_ENABLED_AGENTS = {"sinopse_abstract", "gramatica_ortografia"}


def _review_comments_with_llm(
    comments: list[AgentComment],
    agent: str,
    question: str,
    excerpt: str,
    profile_key: str | None,
) -> tuple[list[AgentComment], str]:
    if agent not in _REVIEWER_ENABLED_AGENTS or not comments:
        return comments, "revisor ignorado"

    prompt = build_comment_review_prompt(agent, profile_key=profile_key)
    payload = {
        "question": _sanitize_for_llm(question),
        "document_excerpt": _sanitize_for_llm(excerpt),
        "comments_json": _sanitize_for_llm(_serialize_comments(comments)),
    }
    try:
        response = _invoke_with_model_fallback(prompt, payload, operation=f"revisor {agent}")
        if response is None:
            return comments, "revisor indisponível"
    except LLMConnectionFailure as exc:
        return comments, f"revisor indisponível por conexão: {_connection_error_summary(exc.original)}"
    except Exception:
        return comments, "revisor indisponível"

    raw = response.content if isinstance(response.content, str) else str(response.content)
    reviews, status = _parse_comment_reviews(raw)
    if not reviews:
        return comments, status

    verdict_by_key = {
        _comment_review_key(
            item.get("paragraph_index"),
            str(item.get("issue_excerpt") or ""),
            str(item.get("suggested_fix") or ""),
        ): item
        for item in reviews
    }

    approved: list[AgentComment] = []
    rejected = 0
    for comment in comments:
        review = verdict_by_key.get(_comment_review_key(comment.paragraph_index, comment.issue_excerpt, comment.suggested_fix))
        if review and review.get("decision") == "reject":
            rejected += 1
            continue
        approved.append(comment)

    return approved, f"{status} | revisor: {len(approved)} aprovados, {rejected} rejeitados"


def _agent_node(agent: str):
    def run(state: ChatState) -> ChatState:
        if get_chat_model() is None:
            return {
                "comments": state.get("comments", []),
                "batch_status": "modelo indisponível",
            }

        prompt = build_agent_prompt(agent, profile_key=state.get("profile_key"))
        payload = {
            "question": _sanitize_for_llm(state["question"]),
            "document_excerpt": _sanitize_for_llm(state["document_excerpt"]),
        }
        try:
            response = _invoke_with_model_fallback(prompt, payload, operation=f"agente {agent}")
            if response is None:
                return {
                    "comments": state.get("comments", []),
                    "batch_status": "modelo indisponível",
                }
        except LLMConnectionFailure as exc:
            return {
                "comments": state.get("comments", []),
                "batch_status": f"falha de conexão da LLM após retries: {_connection_error_summary(exc.original)}",
            }
        except Exception as exc:
            if _is_json_body_error(exc):
                return {
                    "comments": state.get("comments", []),
                    "batch_status": "falha de payload da LLM",
                }
            raise
        raw = response.content if isinstance(response.content, str) else str(response.content)
        items, status = _parse_comments_with_status(raw, agent=agent)
        reviewed_items, review_status = _review_comments_with_llm(
            items,
            agent=agent,
            question=state["question"],
            excerpt=state["document_excerpt"],
            profile_key=state.get("profile_key"),
        )
        merged = [*state.get("comments", []), *reviewed_items]
        combined_status = status if review_status == "revisor ignorado" else f"{status} | {review_status}"
        return {"comments": merged, "batch_status": combined_status}

    return run


def _coordinator_node(state: ChatState) -> ChatState:
    comments = state.get("comments", [])
    if get_chat_model() is None:
        if comments:
            points = "\n".join(f"- [{agent_short_label(c.agent)}] {c.message}" for c in comments[:8])
            answer = "Resumo dos agentes:\n" + points
        else:
            answer = "Não foi possível consultar a LLM. Configure OPENAI_API_KEY no .env."
        return {"answer": answer}

    prompt = build_coordinator_prompt(profile_key=state.get("profile_key"))
    payload = {
        "question": _sanitize_for_llm(state["question"]),
        "document_excerpt": _sanitize_for_llm(state["document_excerpt"]),
        "comments_json": _sanitize_for_llm(_serialize_comments(comments)),
    }
    try:
        response = _invoke_with_model_fallback(prompt, payload, operation="coordenador")
        if response is None:
            return {"answer": _partial_answer_from_comments(comments, "Resumo parcial (modelo indisponível no coordenador):")}
    except LLMConnectionFailure as exc:
        return {
            "answer": _partial_answer_from_comments(
                comments,
                "Resumo parcial (falha de conexão da LLM no coordenador: "
                f"{_connection_error_summary(exc.original)}):",
            )
        }
    except Exception as exc:
        if _is_json_body_error(exc):
            if comments:
                points = "\n".join(f"- [{agent_short_label(c.agent)}] {c.message}" for c in comments[:12])
                return {"answer": "Resumo parcial (falha de payload da LLM no coordenador):\n" + points}
            return {"answer": "Falha ao montar payload para a LLM nesta execução."}
        raise
    answer = response.content if isinstance(response.content, str) else str(response.content)
    return {"answer": answer}


def _build_graph(agent_order: list[str], include_coordinator: bool = True):
    graph = StateGraph(ChatState)

    for agent in agent_order:
        graph.add_node(agent, _agent_node(agent))

    if include_coordinator:
        graph.add_node("coordenador", _coordinator_node)

    if not agent_order and include_coordinator:
        graph.add_edge(START, "coordenador")
    else:
        if agent_order:
            graph.add_edge(START, agent_order[0])
            for idx in range(len(agent_order) - 1):
                graph.add_edge(agent_order[idx], agent_order[idx + 1])
            if include_coordinator:
                graph.add_edge(agent_order[-1], "coordenador")
        elif include_coordinator:
            graph.add_edge(START, "coordenador")

    if include_coordinator:
        graph.add_edge("coordenador", END)
    elif agent_order:
        graph.add_edge(agent_order[-1], END)
    else:
        graph.add_edge(START, END)
    return graph.compile()


def _build_batches(
    chunks: list[str],
    refs: list[str],
    indexes: list[int],
    max_chars: int = 12000,
    max_chunks: int = 28,
) -> list[list[int]]:
    if not chunks or not indexes:
        return []

    batches: list[list[int]] = []
    current: list[int] = []
    current_chars = 0

    for idx in indexes:
        if idx < 0 or idx >= len(chunks):
            continue
        chunk = chunks[idx]
        ref = refs[idx] if idx < len(refs) else "sem referência"
        line = f"[{idx}] ({ref}) {chunk}"
        line_len = len(line) + 1

        if current and (len(current) >= max_chunks or current_chars + line_len > max_chars):
            batches.append(current)
            current = []
            current_chars = 0

        current.append(idx)
        current_chars += line_len

    if current:
        batches.append(current)

    return batches


def _expand_section_ranges(sections: list[Section], keywords: tuple[str, ...]) -> list[int]:
    selected: list[int] = []
    for sec in sections:
        title = sec.title.lower()
        if any(k in title for k in keywords):
            selected.extend(range(sec.start_idx, sec.end_idx + 1))
    return sorted(dict.fromkeys(selected))


def _find_content_indexes(chunks: list[str], pattern: str) -> list[int]:
    rx = re.compile(pattern, re.IGNORECASE)
    out: list[int] = []
    for idx, chunk in enumerate(chunks):
        if rx.search(chunk):
            out.append(idx)
    return out


def _agent_scope_indexes(agent: str, chunks: list[str], refs: list[str], sections: list[Section]) -> list[int]:
    total = len(chunks)
    if total == 0:
        return []

    all_indexes = list(range(total))
    head_20 = list(range(max(1, int(total * 0.20))))
    tail_30_start = max(0, int(total * 0.70))
    tail_30 = list(range(tail_30_start, total))

    if agent == "metadados":
        sec = _expand_section_ranges(sections, ("metadad", "ficha catalogr", "capa", "titulo", "autoria"))
        head_candidates = _find_metadata_like_indexes(chunks, refs, limit=18)
        picked = sorted(dict.fromkeys([*sec, *head_candidates]))
        return picked or head_candidates or list(range(min(12, total)))

    if agent == "sinopse_abstract":
        sec = _expand_section_ranges(
            sections,
            ("sinopse", "abstract", "resumo", "summary"),
        )
        content = _find_content_indexes(chunks, r"\b(sinopse|abstract|resumo|summary|palavras-chave|keywords|jel)\b")
        typed = _indexes_by_ref_type(
            refs,
            {"abstract_heading", "abstract_body", "keywords_label", "keywords_content", "jel_code"},
        )
        picked = _expand_neighbors(sorted(dict.fromkeys([*sec, *content, *typed])), total=total, radius=1)
        return picked or head_20

    if agent == "estrutura":
        typed = _indexes_by_ref_type(refs, {"heading", "reference_heading"})
        section_starts = sorted(dict.fromkeys(sec.start_idx for sec in sections))
        intro_start = next(
            (
                idx
                for idx, chunk in enumerate(chunks)
                if _is_intro_heading(chunk) and _is_implicit_heading_candidate(idx, chunks, refs)
            ),
            None,
        )
        if intro_start is None:
            intro_start = next((idx for idx in sorted(dict.fromkeys([*typed, *section_starts])) if 0 <= idx < len(chunks) and _is_intro_heading(chunks[idx])), None)

        implicit = [
            idx
            for idx in range(intro_start if intro_start is not None else 0, total)
            if _is_implicit_heading_candidate(idx, chunks, refs)
        ]
        heading_candidates = sorted(dict.fromkeys([*typed, *section_starts, *implicit]))
        if not heading_candidates:
            return typed or head_20

        scoped = [idx for idx in heading_candidates if intro_start is None or idx >= intro_start]
        explicit_scoped = [idx for idx in scoped if idx in set(typed) or idx in set(section_starts)]
        implicit_short_scoped = [
            idx for idx in scoped if idx not in set(explicit_scoped) and 0 <= idx < len(chunks) and _heading_word_count(chunks[idx]) <= 4
        ]
        picked = sorted(dict.fromkeys([*explicit_scoped, *implicit_short_scoped]))
        return picked or scoped or heading_candidates

    if agent == "tabelas_figuras":
        sec = _expand_section_ranges(sections, ("tabela", "figura", "quadro", "grafico", "gráfico", "anexo"))
        content = _find_content_indexes(chunks, r"\b(tabela|figura|quadro|gr[aá]fico|imagem)\b")
        typed = _indexes_by_ref_type(refs, {"caption", "table_cell"})
        picked = _expand_neighbors(sorted(dict.fromkeys([*sec, *content, *typed])), total=total, radius=2)
        return picked or typed or all_indexes

    if agent == "referencias":
        sec = _expand_section_ranges(sections, ("refer", "bibliograf", "references", "bibliography"))
        reference_heading_idx = next((idx for idx, ref in enumerate(refs) if _ref_block_type(ref) == "reference_heading"), total)
        citation_like = _find_reference_citation_indexes(chunks, refs, body_limit=reference_heading_idx)
        if sec:
            return sorted(dict.fromkeys([*citation_like, *sec]))
        content = _find_content_indexes(chunks, r"\b(doi|http://|https://|et al\.|v\.\s*\d+|n\.\s*\d+)\b")
        typed = _indexes_by_ref_type(refs, {"reference_entry", "reference_heading"})
        picked = sorted(dict.fromkeys([*citation_like, *content, *typed]))
        if not picked:
            return tail_30
        return picked

    if agent == "tipografia":
        typed = _indexes_by_ref_type(refs, {"heading", "caption", "reference_entry", "reference_heading"})
        styled = [
            idx
            for idx, ref in enumerate(refs)
            if _ref_block_type(ref) == "paragraph" and _style_name_looks_explicit(_ref_style_name(ref)) and idx < 24
        ]
        picked = sorted(dict.fromkeys([*typed, *styled]))
        return picked or typed or head_20

    if agent == "gramatica_ortografia":
        return all_indexes

    return all_indexes


def run_conversation(
    paragraphs: list[str],
    refs: list[str],
    sections: list[Section],
    question: str,
    selected_agents: list[str] | None = None,
    on_agent_done: Callable[[str, int, int], None] | None = None,
    on_agent_progress: Callable[[str, int, int, int, int], None] | None = None,
    on_agent_batch_status: Callable[[str, int, int, str], None] | None = None,
    profile_key: str = "GENERIC",
) -> ConversationResult:
    agent_order = [a for a in (selected_agents or AGENT_ORDER) if a in AGENT_ORDER]
    if not paragraphs:
        return ConversationResult(answer="Documento vazio ou sem texto extraído.", comments=[])
    agent_apps = {agent: _build_graph([agent], include_coordinator=False) for agent in agent_order}

    final_comments: list[AgentComment] = []
    verification_decisions: list[VerificationDecision] = []
    interrupted_reason = ""
    interrupted_agent = ""

    for agent in agent_order:
        scoped_indexes = _agent_scope_indexes(agent, paragraphs, refs, sections)
        batches = _build_batches(paragraphs, refs, scoped_indexes)
        if not batches:
            continue

        stop_processing = False
        for batch_idx, batch_indexes in enumerate(batches, start=1):
            excerpt = build_excerpt(indexes=batch_indexes, chunks=paragraphs, refs=refs, max_chars=1_000_000)
            comments_before_batch = len(final_comments)
            initial_state: ChatState = {
                "question": question,
                "document_excerpt": excerpt,
                "profile_key": profile_key,
                "comments": final_comments,
                "answer": "",
            }

            for update in agent_apps[agent].stream(initial_state, stream_mode="updates"):
                if not update:
                    continue
                node, payload = next(iter(update.items()))
                if not isinstance(payload, dict):
                    continue
                if node != agent:
                    continue

                current_comments = payload.get("comments", final_comments)
                if isinstance(current_comments, list):
                    old_comments = current_comments[:comments_before_batch]
                    batch_comments = current_comments[comments_before_batch:]
                    verified_comments, batch_decisions = _verify_batch_comments(
                        comments=batch_comments,
                        agent=agent,
                        batch_indexes=batch_indexes,
                        chunks=paragraphs,
                        refs=refs,
                        existing_comments=old_comments,
                        batch_index=batch_idx,
                    )
                    final_comments = [*old_comments, *verified_comments]
                    verification_decisions.extend(batch_decisions)
                else:
                    batch_decisions = []
                batch_status = _format_batch_status(str(payload.get("batch_status", "") or ""), batch_decisions)
                total = len(final_comments)
                new_count = sum(1 for decision in batch_decisions if decision.accepted)
                if on_agent_done is not None:
                    on_agent_done(agent, new_count, total)
                if on_agent_batch_status is not None:
                    on_agent_batch_status(agent, batch_idx, len(batches), batch_status)
                if on_agent_progress is not None:
                    on_agent_progress(agent, batch_idx, len(batches), new_count, total)
                if "falha de conexao da llm" in _folded_text(batch_status):
                    interrupted_reason = batch_status
                    interrupted_agent = agent
                    stop_processing = True
                    break

            if stop_processing:
                break

        if stop_processing:
            break

    if interrupted_reason:
        final_answer = _partial_answer_from_comments(
            final_comments,
            "Resumo parcial (execução interrompida por falha de conexão da LLM"
            + (f" no agente {interrupted_agent}" if interrupted_agent else "")
            + f": {interrupted_reason}).",
        )
    else:
        coordinator_state: ChatState = {
            "question": question,
            "document_excerpt": (
                "Revisão por escopo de agente concluída. "
                f"Total de trechos no documento: {len(paragraphs)}. "
                f"Agentes executados: {', '.join(agent_order)}."
            ),
            "profile_key": profile_key,
            "comments": final_comments,
            "answer": "",
        }
        final_answer = _coordinator_node(coordinator_state).get("answer", "")

    return ConversationResult(
        answer=final_answer,
        comments=final_comments,
        verification=_summarize_verification(verification_decisions),
    )
