from __future__ import annotations

from . import grammar, metadata, references, structure, style_conformity, synopsis, tables_figures, typography
from .shared import ValidationContext


def keep_rejection_reason(ctx: ValidationContext) -> str | None:
    if ctx.agent == "estrutura":
        return structure.rejection_reason(ctx)
    if ctx.agent == "metadados":
        return metadata.rejection_reason(ctx)
    if ctx.agent == "tabelas_figuras":
        return tables_figures.rejection_reason(ctx)
    if ctx.agent == "tipografia":
        return typography.rejection_reason(ctx)
    if ctx.agent == "referencias":
        return references.rejection_reason(ctx)
    if ctx.agent == "conformidade_estilos":
        return style_conformity.rejection_reason(ctx)
    if ctx.agent == "sinopse_abstract":
        return synopsis.keep_rejection_reason(ctx)
    if ctx.agent == "gramatica_ortografia":
        return grammar.keep_rejection_reason(ctx)
    return None


def detailed_rejection_reason(ctx: ValidationContext) -> str | None:
    if ctx.agent == "sinopse_abstract":
        return synopsis.detailed_rejection_reason(ctx)
    if ctx.agent == "gramatica_ortografia":
        return grammar.detailed_rejection_reason(ctx)
    return None
