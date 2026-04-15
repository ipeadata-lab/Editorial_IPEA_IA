from __future__ import annotations

import re

from ...review_patterns import (
    _is_non_body_reference_context,
    _looks_like_all_caps_title,
    _looks_like_full_reference_rewrite,
    _normalized_text,
    _years_in_text,
)
from .shared import ValidationContext, is_safe_text_normalization_auto_apply


def rejection_reason(ctx: ValidationContext) -> str | None:
    comment = ctx.comment
    block_type = ctx.block_type
    if block_type not in {"reference_entry", "reference_heading"}:
        if comment.category not in {"citation_format", "citation_match"} or block_type not in {"paragraph", "direct_quote", "list_item"}:
            return "descartado por regra de verificação"
    if comment.auto_apply and not is_safe_text_normalization_auto_apply(comment, ctx.chunks):
        return "descartado por regra de verificação"
    if not isinstance(comment.paragraph_index, int):
        return None

    current = (ctx.chunks[comment.paragraph_index] or "").casefold()
    current_text = ctx.chunks[comment.paragraph_index] or ""
    current_ref = ctx.refs[comment.paragraph_index] if comment.paragraph_index < len(ctx.refs) else ""
    raw_message = (comment.message or "").casefold()
    message_blob = _normalized_text(" ".join([comment.category or "", comment.message or "", comment.suggested_fix or ""]))
    suggestion_blob = _normalized_text(comment.suggested_fix)

    if any(token in ctx.folded_blob for token in {"falta de informacoes", "adicionar informacoes", "caixa baixa", "caixa alta", "italico", "negrito", "destaque grafico"}):
        return "descartado por regra de verificação"
    if "titulo" in ctx.folded_blob and _looks_like_all_caps_title(current_text):
        return "descartado por regra de verificação"
    if "ponto final apos o numero" in _normalized_text(raw_message):
        if re.search(r"\bn\.\s*\d+\s*,", comment.issue_excerpt or "", re.IGNORECASE):
            return "descartado por regra de verificação"
    if comment.category in {"citation_format", "citation_match"} and _is_non_body_reference_context(
        current_ref,
        current_text,
        index=comment.paragraph_index,
        chunks=ctx.chunks,
        refs=ctx.refs,
    ):
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"adicionar o titulo", "adicionar a pagina", "adicionar a paginacao", "adicionar o ano", "ano de publicacao", "verificar e corrigir o ano"}):
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"falta de informacoes", "falta de informações", "adicionar informacoes", "adicionar informações"}):
        return "descartado por regra de verificação"
    if "caixa baixa" in message_blob or "caixa alta" in message_blob:
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"italico", "itálico", "negrito", "destaque grafico", "destaque gráfico"}):
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"verificar", "confirmar", "informacoes suficientes", "informações suficientes"}) and _years_in_text(current_text):
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"pontuacao final", "pontuação final", "ponto final", "pontuacao ao final", "pontuação ao final"}):
        if current_text.rstrip().endswith((".", "!", "?")):
            return "descartado por regra de verificação"
    if "in:" in current_text.casefold() and ("in:" in raw_message and ("uso incorreto" in raw_message or "inserir" in raw_message)):
        return "descartado por regra de verificação"
    if "uso incorreto" in raw_message and "n." in raw_message:
        return "descartado por regra de verificação"
    if "v." in raw_message and "espa" in raw_message and "volume" in raw_message and "v." not in current_text:
        return "descartado por regra de verificação"
    if ":" in raw_message and "espa" in raw_message and not re.search(r":\S", comment.issue_excerpt or ""):
        return "descartado por regra de verificação"
    if ("pontuação entre o título e a editora" in raw_message or "pontuacao entre o titulo e a editora" in _normalized_text(raw_message)):
        if "texto para discussão" in current_text.casefold() or "texto para discussao" in _normalized_text(current_text):
            return "descartado por regra de verificação"
    if "titulo e a editora" in message_blob and "texto para discuss" in _normalized_text(current_text):
        return "descartado por regra de verificação"
    if "n." in raw_message and "ponto" in raw_message:
        if re.search(r"\bn\.\s*\d+\s*,", current_text, re.IGNORECASE):
            return "descartado por regra de verificação"
    if ("ponto final após o número" in raw_message or "ponto final apos o numero" in _normalized_text(raw_message)):
        if re.search(r"\bn\.\s*\d+\s*,", comment.issue_excerpt or "", re.IGNORECASE):
            return "descartado por regra de verificação"
    if any(token in message_blob for token in {"titulo", "título", "autor", "ano", "periodico", "periódico"}) and _looks_like_full_reference_rewrite(current_text, comment.suggested_fix):
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"titulo", "título"}) and re.search(r"\bpp?\.\s*\d", current_text):
        return "descartado por regra de verificação"
    if _normalized_text(comment.suggested_fix) == _normalized_text(current_text):
        return "descartado por regra de verificação"
    if any(token in message_blob for token in {"padrao de formatação", "padrao de formatacao", "padrão de formatação"}):
        return "descartado por regra de verificação"
    if any(token in suggestion_blob for token in {"[ano]", "[local]", "[editora]"}) or "[" in (comment.suggested_fix or ""):
        return "descartado por regra de verificação"
    if "titulo" in message_blob and _looks_like_all_caps_title(current_text):
        return "descartado por regra de verificação"
    if "ano" in _normalized_text(comment.category) or "ano" in _normalized_text(comment.message):
        current_years = _years_in_text(current_text)
        suggestion_years = _years_in_text(comment.suggested_fix)
        if current_years and suggestion_years and suggestion_years != current_years:
            return "descartado por regra de verificação"
        if re.search(r"\b(19|20)\d{2}\b", current) and "alterar o ano" in _normalized_text(comment.suggested_fix):
            return "descartado por regra de verificação"
    return None
