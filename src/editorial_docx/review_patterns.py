from __future__ import annotations

import re
import unicodedata

from .models import AgentComment

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
    r"^\s*(?:tabela|figura|quadro|imagem|grafico)\s+\d+\b",
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
_SAFE_QUOTED_EXCERPT_RE = re.compile(r'^\s*["\u201c\u201d\'\u2018\u2019\u00ab\u00bb].+["\u201c\u201d\'\u2018\u2019\u00ab\u00bb]\s*$')
_SAFE_QUOTE_CHAR_RE = re.compile(r'["\u201c\u201d\'\u2018\u2019\u00ab\u00bb]')
_HEADING_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:(?:\d+(?:\.\d+)*)\.?|[ivxlcdm]+\.?)\s+", re.IGNORECASE)
_REF_NUMBERING_RE = re.compile(r"\bnumerado=sim\b", re.IGNORECASE)


def _normalized_text(value: str) -> str:
    return " ".join((value or "").split())


def _ascii_fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _folded_text(value: str) -> str:
    return _ascii_fold(_normalized_text(value)).casefold()


def _parse_format_spec(raw: str) -> dict[str, str]:
    spec: dict[str, str] = {}
    for piece in (raw or "").split(";"):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
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
    folded = _folded_text(text)
    if _ILLUSTRATION_LABEL_RE.match(folded):
        return True
    return bool(re.match(r"^\s*gr\S*fico\s+\d+\b", _normalized_text(text), flags=re.IGNORECASE))


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


def _diacritic_count(text: str) -> int:
    return sum(1 for ch in unicodedata.normalize("NFD", text or "") if unicodedata.category(ch) == "Mn")


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


def _is_numbered_heading_context(index: int, chunks: list[str], refs: list[str]) -> bool:
    if not (0 <= index < len(chunks)):
        return False
    ref_type = _ref_block_type(refs[index]) if index < len(refs) else ""
    if ref_type not in {"", "paragraph", "heading"}:
        return False

    text = (chunks[index] or "").strip()
    if not text or not _HEADING_NUMBER_PREFIX_RE.match(text):
        return False
    if _heading_word_count(text) == 0 or _heading_word_count(text) > 6:
        return False

    stripped = _strip_heading_prefix(text)
    if not stripped or stripped[-1] in ".!?;:":
        return False
    if _contains_quote_marks(stripped):
        return False

    folded = _ascii_fold(stripped).casefold()
    if folded.startswith(("fonte", "elaboracao", "nota", "figura", "grafico", "tabela", "quadro", "imagem")):
        return False

    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9]+", stripped)
    if not tokens:
        return False
    lowercase_allowed = {"a", "as", "o", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das", "dos", "e"}
    first_token = _ascii_fold(tokens[0]).casefold()
    if first_token not in lowercase_allowed and stripped[0].islower():
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


def _is_non_body_reference_context(
    ref: str,
    text: str,
    *,
    index: int | None = None,
    chunks: list[str] | None = None,
    refs: list[str] | None = None,
) -> bool:
    block_type = _ref_block_type(ref)
    if block_type in {
        "reference_entry",
        "reference_heading",
        "caption",
        "table_cell",
        "heading",
        "title",
        "abstract_heading",
        "keywords_label",
        "keywords_content",
        "jel_code",
    }:
        return True
    if _is_illustration_caption(text):
        return True
    style_name = _ref_style_name(ref).casefold()
    if "caption" in style_name or "legenda" in style_name:
        return True
    if index is not None and chunks is not None and refs is not None and (
        _is_implicit_heading_candidate(index, chunks, refs) or _is_numbered_heading_context(index, chunks, refs)
    ):
        return True
    return False


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
    return issue[:-1].rstrip() == suggestion.rstrip()


def _years_in_text(text: str) -> list[str]:
    return re.findall(r"\b(?:19|20)\d{2}[a-z]?\b", text or "", flags=re.IGNORECASE)


def _quoted_terms(text: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r'["“”\'‘’«»]([^"“”\'‘’«»]+)["“”\'‘’«»]', text or "")]


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", text or "")


def _count_words(text: str) -> int:
    return len(_word_tokens(text))


def _extract_word_limit(text: str) -> int | None:
    match = re.search(r"\b(?:at[eé]|até|max(?:imo)?|m[aá]ximo)\s+(\d{2,4})\s+palavras\b", _folded_text(text))
    if match:
        return int(match.group(1))
    return None


def _split_keyword_entries(text: str) -> list[str]:
    source = (text or "").strip()
    if not source:
        return []
    return [item.strip(" ;,.") for item in re.split(r"[;,]", source) if item.strip(" ;,.")]


def _has_repeated_keyword_entries(text: str) -> bool:
    entries = [_folded_text(item) for item in _split_keyword_entries(text)]
    seen: set[str] = set()
    for item in entries:
        if not item:
            continue
        if item in seen:
            return True
        seen.add(item)
    return False


def _punctuation_only_change(issue_excerpt: str, suggested_fix: str) -> bool:
    issue = _normalized_text(issue_excerpt)
    fix = _normalized_text(suggested_fix)
    if not issue or not fix:
        return False
    issue_tokens = re.findall(r"[A-Za-zÀ-ÿ0-9]+", issue)
    fix_tokens = re.findall(r"[A-Za-zÀ-ÿ0-9]+", fix)
    return issue_tokens == fix_tokens and issue != fix


def _adds_coordination_comma(issue_excerpt: str, suggested_fix: str) -> bool:
    issue = _normalized_text(issue_excerpt)
    fix = _normalized_text(suggested_fix)
    if not issue or not fix:
        return False
    coordination_patterns = (
        (r"\be\b", r", e\b"),
        (r"\bou\b", r", ou\b"),
        (r"\bnem\b", r", nem\b"),
    )
    for token_rx, comma_rx in coordination_patterns:
        if re.search(token_rx, issue, flags=re.IGNORECASE) and re.search(comma_rx, fix, flags=re.IGNORECASE):
            issue_plain = re.sub(r"\s+", " ", issue)
            fix_without_comma = re.sub(r",\s+(?=(e|ou|nem)\b)", " ", fix, flags=re.IGNORECASE)
            if _folded_text(issue_plain) == _folded_text(fix_without_comma):
                return True
    return False


def _is_demonstrative_swap(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_tokens = re.findall(r"[A-Za-zÀ-ÿ]+", _folded_text(issue_excerpt))
    fix_tokens = re.findall(r"[A-Za-zÀ-ÿ]+", _folded_text(suggested_fix))
    if len(issue_tokens) != len(fix_tokens) or not issue_tokens:
        return False
    demonstratives = {
        "esse",
        "essa",
        "esses",
        "essas",
        "este",
        "esta",
        "estes",
        "estas",
        "desse",
        "dessa",
        "desses",
        "dessas",
        "deste",
        "desta",
        "destes",
        "destas",
        "isso",
        "isto",
        "aquele",
        "aquela",
        "aqueles",
        "aquelas",
        "aquele",
    }
    differing_pairs = [(src, dst) for src, dst in zip(issue_tokens, fix_tokens) if src != dst]
    if len(differing_pairs) != 1:
        return False
    src, dst = differing_pairs[0]
    return src in demonstratives and dst in demonstratives


def _drops_article_before_possessive(issue_excerpt: str, suggested_fix: str) -> bool:
    issue = _folded_text(issue_excerpt)
    fix = _folded_text(suggested_fix)
    if not issue or not fix:
        return False
    possessives = (
        "meu",
        "minha",
        "meus",
        "minhas",
        "seu",
        "sua",
        "seus",
        "suas",
        "nosso",
        "nossa",
        "nossos",
        "nossas",
        "vosso",
        "vossa",
        "vossos",
        "vossas",
    )
    article_pattern = r"\b(o|a|os|as)\s+(" + "|".join(possessives) + r")\b"
    matches = list(re.finditer(article_pattern, issue))
    if not matches:
        return False
    for match in matches:
        collapsed = issue[: match.start()] + match.group(2) + issue[match.end() :]
        if _folded_text(collapsed) == fix:
            return True
    return False


def _removes_diacritic_only_word(issue_excerpt: str, suggested_fix: str) -> bool:
    issue_tokens = _word_tokens(issue_excerpt)
    fix_tokens = _word_tokens(suggested_fix)
    if len(issue_tokens) != len(fix_tokens) or not issue_tokens:
        return False
    changed = [
        (issue_token, fix_token)
        for issue_token, fix_token in zip(issue_tokens, fix_tokens)
        if _folded_text(issue_token) != _folded_text(fix_token)
    ]
    if changed:
        return False
    return any(
        issue_token != fix_token and _diacritic_count(issue_token) > _diacritic_count(fix_token)
        for issue_token, fix_token in zip(issue_tokens, fix_tokens)
    )


def _introduces_plural_copula_for_singular_head(issue_excerpt: str, suggested_fix: str) -> bool:
    issue = _folded_text(issue_excerpt)
    fix = _folded_text(suggested_fix)
    singular_heads = {
        "conjunto",
        "grupo",
        "lista",
        "serie",
        "sequencia",
        "politica",
        "programa",
        "sistema",
        "regime",
        "quadro",
        "mercado",
        "resultado",
        "efeito",
        "conhecimento",
    }
    if not any(re.search(rf"\b{head}\b", issue) for head in singular_heads):
        return False
    singular_patterns = (r"\be\b", r"\bfoi\b", r"\besta\b", r"\bsera\b")
    plural_patterns = (r"\bsao\b", r"\bforam\b", r"\bestao\b", r"\bserao\b")
    return any(re.search(pattern, issue) for pattern in singular_patterns) and any(re.search(pattern, fix) for pattern in plural_patterns)


def _looks_like_full_reference_rewrite(source_text: str, suggested_fix: str) -> bool:
    source_tokens = _word_tokens(source_text)
    fix_tokens = _word_tokens(suggested_fix)
    if len(source_tokens) < 12 or len(fix_tokens) < 12:
        return False
    overlap = len(set(_folded_text(token) for token in source_tokens) & set(_folded_text(token) for token in fix_tokens))
    return overlap >= max(6, int(len(set(source_tokens)) * 0.6))


def _is_reference_missing_data_speculation(message: str, suggested_fix: str) -> bool:
    blob = _folded_text(" ".join([message or "", suggested_fix or ""]))
    speculation_tokens = {
        "falta local",
        "falta editora",
        "falta ano",
        "falta pagina",
        "falta paginacao",
        "falta de informacoes",
        "falta de informacoes sobre o periodico",
        "informacoes sobre o periodico",
        "local do periodico",
        "falta cidade",
        "adicionar doi",
        "completar doi",
        "adicionar url",
        "adicionar numero",
        "adicionar volume",
        "volume e numero",
        "adicionar edicao",
        "local/editora",
        "inserir o local",
        "adicionar a paginacao",
    }
    return any(token in blob for token in speculation_tokens)


def _is_grammar_rewrite_or_regency_comment(message: str, suggested_fix: str) -> bool:
    blob = _folded_text(" ".join([message or "", suggested_fix or ""]))
    risky_tokens = {
        "reescrev",
        "reformular",
        "reformular o trecho",
        "regencia",
        "clareza",
        "mais claro",
        "mais fluido",
        "melhor fluidez",
        "ajustar estilo",
        "transitivo direto",
        "nao exige a preposicao",
    }
    return any(token in blob for token in risky_tokens)


def _comment_key(item: AgentComment) -> tuple[str, str, int | None, str, str, str, bool, str]:
    return (
        item.agent,
        _normalized_text(item.category),
        item.paragraph_index if isinstance(item.paragraph_index, int) else None,
        _normalized_text(item.message),
        _normalized_text(item.issue_excerpt),
        _normalized_text(item.suggested_fix),
        bool(item.auto_apply),
        _normalized_text(item.format_spec),
    )


def _comment_review_key(paragraph_index: int | None, issue_excerpt: str, suggested_fix: str) -> tuple[int | None, str, str]:
    return (
        paragraph_index if isinstance(paragraph_index, int) else None,
        _normalized_text(issue_excerpt),
        _normalized_text(suggested_fix),
    )


def _dedupe_comments(items: list[AgentComment]) -> list[AgentComment]:
    best: dict[tuple[str, str, int | None, str, str, str, bool, str], AgentComment] = {}
    for item in items:
        key = _comment_key(item)
        if key not in best:
            best[key] = item
    return list(best.values())


def _find_metadata_like_indexes(chunks: list[str], refs: list[str], limit: int = 18) -> list[int]:
    metadata_rx = re.compile(r"\b(titulo|título|autor|autora|isbn|issn|doi|palavras-chave|keywords|jel)\b", re.IGNORECASE)
    allowed_types = {"title", "author_line", "document_label", "heading", "paragraph"}
    picked: list[int] = []
    for idx, chunk in enumerate(chunks[: min(limit, len(chunks))]):
        if _ref_block_type(refs[idx]) not in allowed_types:
            continue
        if idx <= 12 or metadata_rx.search(chunk):
            picked.append(idx)
    return picked


__all__ = [
    "_ALLOWED_TYPOGRAPHY_KEYS",
    "_HEADING_NUMBER_PREFIX_RE",
    "_STYLE_BY_BLOCK_TYPE",
    "_adds_coordination_comma",
    "_ascii_fold",
    "_comment_key",
    "_comment_review_key",
    "_contains_quote_marks",
    "_count_words",
    "_dedupe_comments",
    "_diacritic_count",
    "_drops_article_before_possessive",
    "_extract_word_limit",
    "_find_metadata_like_indexes",
    "_folded_text",
    "_has_repeated_keyword_entries",
    "_heading_word_count",
    "_indexes_by_ref_type",
    "_introduces_plural_copula_for_singular_head",
    "_is_demonstrative_swap",
    "_is_grammar_rewrite_or_regency_comment",
    "_is_illustration_caption",
    "_is_implicit_heading_candidate",
    "_is_intro_heading",
    "_is_non_body_reference_context",
    "_is_numbered_heading_context",
    "_is_reference_missing_data_speculation",
    "_is_relevant_typography_spec",
    "_looks_like_all_caps_title",
    "_looks_like_full_reference_rewrite",
    "_looks_like_quoted_excerpt",
    "_normalized_text",
    "_parse_format_spec",
    "_punctuation_only_change",
    "_quoted_terms",
    "_ref_align",
    "_ref_block_type",
    "_ref_has_flag",
    "_ref_has_numbering",
    "_ref_style_name",
    "_removes_diacritic_only_word",
    "_removes_terminal_period_only",
    "_split_keyword_entries",
    "_strip_heading_prefix",
    "_style_name_looks_explicit",
    "_word_tokens",
    "_years_in_text",
]
