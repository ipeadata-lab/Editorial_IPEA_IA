from __future__ import annotations

import json
import re
from collections.abc import Callable
from json import JSONDecodeError
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .context_selector import build_excerpt
from .document_loader import Section
from .llm import get_chat_model
from .models import AgentComment, ConversationResult
from .prompts import AGENT_ORDER, AgentCommentsPayload, build_agent_prompt, build_coordinator_prompt


class ChatState(TypedDict, total=False):
    question: str
    document_excerpt: str
    profile_key: str
    comments: list[AgentComment]
    answer: str
    batch_status: str


_CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_SURROGATE_RE = re.compile(r"[\uD800-\uDFFF]")
_REF_TYPE_RE = re.compile(r"\btipo=([a-z_]+)\b", re.IGNORECASE)
_ALLOWED_TYPOGRAPHY_KEYS = {
    "font",
    "size_pt",
    "bold",
    "italic",
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
_STYLE_BY_BLOCK_TYPE = {
    "heading": {"TITULO_1", "TITULO_2", "TITULO_3", "TÍTULO_1", "TÍTULO_2", "TÍTULO_3"},
    "paragraph": {"TEXTO"},
    "table_cell": {"TEXTO_TABELA"},
    "reference_entry": {"TEXTO_REFERENCIA"},
    "reference_heading": {"TITULO_1", "TÍTULO_1"},
    "caption": {"TEXTO", "FONTE_TABELA_GRAFICO", "TEXTO_TABELA"},
}


def _sanitize_for_llm(text: str) -> str:
    # Remove characters that frequently break JSON payload parsing upstream.
    cleaned = _CTRL_RE.sub(" ", text or "")
    cleaned = _SURROGATE_RE.sub(" ", cleaned)
    return cleaned.replace("\ufeff", " ").strip()


def _is_json_body_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "could not parse the json body of your request" in msg


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
                "auto_apply": c.auto_apply,
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
                auto_apply=item.auto_apply,
                format_spec=item.format_spec,
            )
        )

    if not out:
        return [], "json válido sem comentários"
    return out, status


def _parse_comments(raw: str, agent: str) -> list[AgentComment]:
    items, _ = _parse_comments_with_status(raw, agent)
    return items


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


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
    strong_keys = {"font", "size_pt", "bold", "italic", "align", "left_indent_pt"}
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
        issue_excerpt = _normalized_text(comment.issue_excerpt)
        table_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
        if block_type == "table_cell" and any(token in table_blob for token in {"subtitulo", "subtítulo", "fonte", "identificador", "legenda"}):
            return False
        if block_type != "caption" and any(token in table_blob for token in {"subtitulo", "subtítulo", "fonte"}):
            return False
        if re.match(r"^(tabela|figura|quadro)\s+\d+", issue_excerpt):
            if "fonte" in _normalized_text(comment.message) or "fonte" in _normalized_text(comment.suggested_fix):
                return False
        if "fonte" in _normalized_text(comment.message) and isinstance(comment.paragraph_index, int):
            if _has_neighbor_with_prefix(comment.paragraph_index, refs, chunks, ("Fonte:", "Elaboração:"), radius=2):
                return False
        if comment.auto_apply and not _is_safe_text_normalization_auto_apply(comment, chunks):
            return False

    if agent == "tipografia":
        if not comment.auto_apply:
            return False
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
        if any(token in tipografia_blob for token in {"caixa alta", "caixa baixa", "capitaliza", "maiusc", "minusc"}):
            return False
        if "alterar para '" in (comment.suggested_fix or "").casefold() or 'alterar para "' in (comment.suggested_fix or "").casefold():
            return False
        if any(token in _normalized_text(comment.suggested_fix) for token in {"reescrever", "substituir texto", "alterar conteúdo"}):
            return False

    if agent == "referencias" and block_type not in {"reference_entry", "reference_heading"}:
        return False
    if agent == "referencias" and comment.auto_apply:
        if not _is_safe_text_normalization_auto_apply(comment, chunks):
            return False
    if agent == "referencias" and isinstance(comment.paragraph_index, int):
        current = (chunks[comment.paragraph_index] or "").casefold()
        message_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
        if any(token in message_blob for token in {"adicionar o titulo", "adicionar a pagina", "adicionar a paginacao", "adicionar o ano", "ano de publicacao", "verificar e corrigir o ano"}):
            return False
        if "caixa baixa" in message_blob or "caixa alta" in message_blob:
            return False
        source_text = chunks[comment.paragraph_index] or ""
        if "titulo" in message_blob and _looks_like_all_caps_title(source_text):
            return False
        if "ano" in _normalized_text(comment.category) or "ano" in _normalized_text(comment.message):
            if re.search(r"\b(19|20)\d{2}\b", current):
                if "alterar o ano" in _normalized_text(comment.suggested_fix):
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
        if comment.issue_excerpt:
            excerpt_ok = _find_excerpt_index(comment.issue_excerpt, [comment.paragraph_index], chunks)
            if excerpt_ok is None and agent in {"gramatica_ortografia", "referencias"}:
                return False
        if agent == "gramatica_ortografia":
            msg = _normalized_text(comment.message)
            if not any(term in msg for term in {"ortografia", "acentua", "pontua", "concord", "crase", "regência", "regencia"}):
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


def _normalize_batch_comments(
    comments: list[AgentComment],
    agent: str,
    batch_indexes: list[int],
    chunks: list[str],
    refs: list[str],
) -> list[AgentComment]:
    normalized: list[AgentComment] = []
    for comment in comments:
        remapped = _remap_comment_index(comment, batch_indexes=batch_indexes, chunks=chunks)
        if _should_keep_comment(remapped, agent=agent, chunks=chunks, refs=refs):
            normalized.append(remapped)
    return normalized


def _agent_node(agent: str):
    def run(state: ChatState) -> ChatState:
        model = get_chat_model()
        if model is None:
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
            response = (prompt | model).invoke(payload)
        except Exception as exc:
            if _is_json_body_error(exc):
                return {
                    "comments": state.get("comments", []),
                    "batch_status": "falha de payload da LLM",
                }
            raise
        raw = response.content if isinstance(response.content, str) else str(response.content)
        items, status = _parse_comments_with_status(raw, agent=agent)
        merged = [*state.get("comments", []), *items]
        return {"comments": merged, "batch_status": status}

    return run


def _coordinator_node(state: ChatState) -> ChatState:
    model = get_chat_model()
    comments = state.get("comments", [])
    if model is None:
        if comments:
            points = "\n".join(f"- [{c.agent}] {c.message}" for c in comments[:8])
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
        response = (prompt | model).invoke(payload)
    except Exception as exc:
        if _is_json_body_error(exc):
            if comments:
                points = "\n".join(f"- [{c.agent}] {c.message}" for c in comments[:12])
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
        content = _find_content_indexes(chunks, r"\b(sinopse|abstract|resumo|summary)\b")
        picked = sorted(dict.fromkeys([*sec, *content]))
        return picked or head_20

    if agent == "estrutura":
        typed = _indexes_by_ref_type(refs, {"heading", "reference_heading"})
        section_starts = sorted(dict.fromkeys(sec.start_idx for sec in sections))
        picked = sorted(dict.fromkeys([*typed, *section_starts]))
        return picked or typed or head_20

    if agent == "tabelas_figuras":
        sec = _expand_section_ranges(sections, ("tabela", "figura", "quadro", "grafico", "gráfico", "anexo"))
        content = _find_content_indexes(chunks, r"\b(tabela|figura|quadro|gr[aá]fico|imagem)\b")
        typed = _indexes_by_ref_type(refs, {"caption", "table_cell"})
        picked = _expand_neighbors(sorted(dict.fromkeys([*sec, *content, *typed])), total=total, radius=2)
        return picked or typed or all_indexes

    if agent == "referencias":
        sec = _expand_section_ranges(sections, ("refer", "bibliograf", "references", "bibliography"))
        if sec:
            return sec
        content = _find_content_indexes(chunks, r"\b(doi|http://|https://|et al\.|v\.\s*\d+|n\.\s*\d+)\b")
        typed = _indexes_by_ref_type(refs, {"reference_entry", "reference_heading"})
        picked = sorted(dict.fromkeys([*content, *typed]))
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
    previous_count = 0

    for agent in agent_order:
        scoped_indexes = _agent_scope_indexes(agent, paragraphs, refs, sections)
        batches = _build_batches(paragraphs, refs, scoped_indexes)
        if not batches:
            continue

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
                    final_comments = [
                        *old_comments,
                        *_normalize_batch_comments(
                            batch_comments,
                            agent=agent,
                            batch_indexes=batch_indexes,
                            chunks=paragraphs,
                            refs=refs,
                        ),
                    ]
                batch_status = str(payload.get("batch_status", "") or "")
                total = len(final_comments)
                new_count = max(total - previous_count, 0)
                previous_count = total
                if on_agent_done is not None:
                    on_agent_done(agent, new_count, total)
                if on_agent_batch_status is not None:
                    on_agent_batch_status(agent, batch_idx, len(batches), batch_status)
                if on_agent_progress is not None:
                    on_agent_progress(agent, batch_idx, len(batches), new_count, total)

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

    return ConversationResult(answer=final_answer, comments=final_comments)
