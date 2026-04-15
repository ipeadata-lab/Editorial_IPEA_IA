from __future__ import annotations

from . import default, grammar, metadata, references, structure, synopsis, tables_figures, typography


def scope_indexes_for_agent(agent: str, chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    if agent == "metadados":
        return metadata.build_scope(chunks, refs, sections, total)
    if agent == "sinopse_abstract":
        return synopsis.build_scope(chunks, refs, sections, total)
    if agent == "estrutura":
        return structure.build_scope(chunks, refs, sections, total)
    if agent == "tabelas_figuras":
        return tables_figures.build_scope(chunks, refs, sections, total)
    if agent == "referencias":
        return references.build_scope(chunks, refs, sections, total)
    if agent == "tipografia":
        return typography.build_scope(chunks, refs, sections, total)
    if agent == "gramatica_ortografia":
        return grammar.build_scope(chunks, refs, sections, total)
    return default.build_scope(chunks, refs, sections, total)
