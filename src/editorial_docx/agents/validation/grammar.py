from __future__ import annotations

from ...review_patterns import (
    _adds_coordination_comma,
    _contains_quote_marks,
    _drops_article_before_possessive,
    _introduces_plural_copula_for_singular_head,
    _is_demonstrative_swap,
    _is_grammar_rewrite_or_regency_comment,
    _looks_like_quoted_excerpt,
    _normalized_text,
    _removes_diacritic_only_word,
    _removes_terminal_period_only,
)
from .shared import ValidationContext, find_excerpt_index


def keep_rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    source_text = ctx.source_text
    grammar_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
    grammar_blob_folded = ctx.folded_blob

    if ctx.block_type == "direct_quote":
        return "descartado por regra de verificação"
    if ctx.block_type == "reference_entry":
        return "descartado por regra de verificação"
    if _looks_like_quoted_excerpt(comment.issue_excerpt):
        return "descartado por regra de verificação"
    excerpt = (comment.issue_excerpt or "").strip()
    if _contains_quote_marks(source_text) and excerpt and len(excerpt) >= max(120, int(len(source_text) * 0.65)):
        return "descartado por regra de verificação"
    if "pontua" in grammar_blob and excerpt and len(excerpt) > 120:
        return "descartado por regra de verificação"
    if "concord" in grammar_blob and excerpt and len(excerpt) > 120:
        return "descartado por regra de verificação"
    if any(token in grammar_blob_folded for token in {"duplicacao local", "repeticao imediata"}):
        return "descartado por regra de verificação"
    if any(token in grammar_blob for token in {"clareza", "simplificada", "simplificar", "reestruturar", "reescr"}):
        return "descartado por regra de verificação"
    if _adds_coordination_comma(excerpt or source_text, comment.suggested_fix):
        return "descartado por regra de verificação"
    if _is_demonstrative_swap(excerpt or source_text, comment.suggested_fix):
        return "descartado por regra de verificação"
    if _drops_article_before_possessive(excerpt or source_text, comment.suggested_fix):
        return "descartado por regra de verificação"
    if _introduces_plural_copula_for_singular_head(excerpt or source_text, comment.suggested_fix):
        return "descartado por regra de verificação"
    if "observam-se que" in _normalized_text(comment.suggested_fix):
        return "descartado por regra de verificação"
    if _normalized_text(comment.suggested_fix) == _normalized_text(source_text):
        return "descartado por regra de verificação"
    if _removes_terminal_period_only(comment.issue_excerpt or source_text, comment.suggested_fix):
        return "descartado por regra de verificação"
    if comment.issue_excerpt and find_excerpt_index(comment.issue_excerpt, [comment.paragraph_index], ctx.chunks) is None:
        return "descartado por regra de verificação"
    return None


def detailed_rejection_reason(ctx: ValidationContext) -> str | None:
    excerpt = ctx.comment.issue_excerpt or ctx.source_text
    if _is_grammar_rewrite_or_regency_comment(ctx.comment.message, ctx.comment.suggested_fix):
        return "comentário gramatical de reescrita ou regência discutível"
    if _removes_diacritic_only_word(excerpt, ctx.comment.suggested_fix):
        return "remoção de acento não confirmada"
    return None
