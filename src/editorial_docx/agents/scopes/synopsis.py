from __future__ import annotations

from ...review_patterns import _indexes_by_ref_type, _ref_block_type
from .shared import expand_neighbors, expand_section_ranges, find_content_indexes


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    """Builds scope."""
    abstract_keywords = ("sinopse", "abstract", "resumo", "summary")
    sec = expand_section_ranges(sections, abstract_keywords)
    heading_content = [
        idx
        for idx in find_content_indexes(chunks, r"\b(sinopse|abstract|resumo|summary)\b")
        if _ref_block_type(refs[idx] if idx < len(refs) else "") in {"heading", "abstract_heading"}
    ]
    abstract_heading_typed = _indexes_by_ref_type(refs, {"abstract_heading"})
    abstract_body_typed = _indexes_by_ref_type(refs, {"abstract_body"})

    title_markers = sorted(dict.fromkeys([*sec, *heading_content, *abstract_heading_typed]))
    if not title_markers:
        return []

    support_content = find_content_indexes(chunks, r"\b(palavras-chave|keywords|jel)\b")
    support_typed = _indexes_by_ref_type(refs, {"keywords_label", "keywords_content", "jel_code"})
    picked = expand_neighbors(
        sorted(dict.fromkeys([*title_markers, *abstract_body_typed, *support_content, *support_typed])),
        total=total,
        radius=1,
    )
    return picked
