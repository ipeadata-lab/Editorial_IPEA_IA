from __future__ import annotations

from .grammar import heuristic_grammar_comments
from .references import heuristic_reference_comments, heuristic_reference_global_comments
from .structure import heuristic_structure_comments
from .synopsis import heuristic_synopsis_comments
from .tables_figures import heuristic_table_figure_comments
from .typography import heuristic_typography_comments


def heuristic_comments_for_agent(agent: str, batch_indexes: list[int], chunks: list[str], refs: list[str]):
    comments = []
    if agent == "gramatica_ortografia":
        comments.extend(heuristic_grammar_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "sinopse_abstract":
        comments.extend(heuristic_synopsis_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "tabelas_figuras":
        comments.extend(heuristic_table_figure_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "referencias":
        comments.extend(heuristic_reference_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
        comments.extend(heuristic_reference_global_comments(chunks=chunks, refs=refs, batch_indexes=batch_indexes))
    if agent == "estrutura":
        comments.extend(heuristic_structure_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    if agent == "tipografia":
        comments.extend(heuristic_typography_comments(batch_indexes=batch_indexes, chunks=chunks, refs=refs))
    return comments


__all__ = ["heuristic_comments_for_agent"]
