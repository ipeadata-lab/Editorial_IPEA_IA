from __future__ import annotations

from ...review_patterns import _indexes_by_ref_type
from .shared import expand_neighbors, expand_section_ranges, find_content_indexes


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    sec = expand_section_ranges(sections, ("sinopse", "abstract", "resumo", "summary"))
    content = find_content_indexes(chunks, r"\b(sinopse|abstract|resumo|summary|palavras-chave|keywords|jel)\b")
    typed = _indexes_by_ref_type(refs, {"abstract_heading", "abstract_body", "keywords_label", "keywords_content", "jel_code"})
    picked = expand_neighbors(sorted(dict.fromkeys([*sec, *content, *typed])), total=total, radius=1)
    return picked or list(range(max(1, int(total * 0.20))))
