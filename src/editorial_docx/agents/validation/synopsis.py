from __future__ import annotations

from ...review_patterns import _count_words, _extract_word_limit, _has_repeated_keyword_entries, _normalized_text, _quoted_terms
from .shared import ValidationContext


def keep_rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    source_text = ctx.source_text
    synopsis_blob = _normalized_text(" ".join([comment.message or "", comment.suggested_fix or ""]))
    if ("portugu" in synopsis_blob and "ingl" in synopsis_blob) or any(
        token in synopsis_blob for token in {"português e inglês", "portugues e ingles"}
    ):
        return "descartado por regra de verificação"
    if any(
        token in synopsis_blob
        for token in {"nao inicia com letra maiuscula", "não inicia com letra maiúscula", "iniciar a frase com letra maiuscula", "iniciar a frase com letra maiúscula"}
    ):
        return "descartado por regra de verificação"
    quoted_terms = _quoted_terms(" ".join([comment.message or "", comment.suggested_fix or ""]))
    issue_blob = _normalized_text(comment.issue_excerpt)
    if quoted_terms and not any(_normalized_text(term) in issue_blob for term in quoted_terms):
        return "descartado por regra de verificação"
    word_limit = _extract_word_limit(" ".join([comment.message or "", comment.suggested_fix or ""]))
    if word_limit is not None:
        counted_text = comment.issue_excerpt or source_text
        if _count_words(counted_text) <= word_limit:
            return "descartado por regra de verificação"
    if ctx.block_type == "keywords_content":
        repetition_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
        if any(token in repetition_blob for token in {"repet", "redundan"}):
            if not _has_repeated_keyword_entries(comment.issue_excerpt or source_text):
                return "descartado por regra de verificação"
    return None


def detailed_rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    source_text = ctx.source_text
    word_limit = _extract_word_limit(" ".join([comment.message or "", comment.suggested_fix or ""]))
    if word_limit is not None:
        counted_text = comment.issue_excerpt or source_text
        if _count_words(counted_text) <= word_limit:
            return "alegação de limite de palavras não confirmada"
    if ctx.block_type == "keywords_content":
        repetition_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
        if any(token in repetition_blob for token in {"repet", "redundan"}):
            if not _has_repeated_keyword_entries(comment.issue_excerpt or source_text):
                return "alegação de repetição não confirmada"
    return None
