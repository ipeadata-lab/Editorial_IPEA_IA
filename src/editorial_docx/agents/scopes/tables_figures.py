from __future__ import annotations

from ...review_patterns import _indexes_by_ref_type
from .shared import expand_neighbors, expand_section_ranges, find_content_indexes


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    sec = expand_section_ranges(sections, ("tabela", "figura", "quadro", "grafico", "gráfico", "anexo"))
    content = find_content_indexes(chunks, r"\b(tabela|figura|quadro|gr[aá]fico|imagem)\b")
    typed = _indexes_by_ref_type(refs, {"caption", "table_cell"})
    picked = expand_neighbors(sorted(dict.fromkeys([*sec, *content, *typed])), total=total, radius=2)
    return picked or typed or list(range(total))
