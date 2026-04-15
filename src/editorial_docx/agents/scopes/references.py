from __future__ import annotations

from ...review_heuristics import _find_reference_citation_indexes
from ...review_patterns import _indexes_by_ref_type, _ref_block_type
from .shared import expand_section_ranges, find_content_indexes


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    sec = expand_section_ranges(sections, ("refer", "bibliograf", "references", "bibliography"))
    reference_heading_idx = next((idx for idx, ref in enumerate(refs) if _ref_block_type(ref) == "reference_heading"), total)
    citation_like = _find_reference_citation_indexes(chunks, refs, body_limit=reference_heading_idx)
    if sec:
        return sorted(dict.fromkeys([*citation_like, *sec]))
    content = find_content_indexes(chunks, r"\b(doi|http://|https://|et al\.|v\.\s*\d+|n\.\s*\d+)\b")
    typed = _indexes_by_ref_type(refs, {"reference_entry", "reference_heading"})
    picked = sorted(dict.fromkeys([*citation_like, *content, *typed]))
    if not picked:
        return list(range(max(0, int(total * 0.70)), total))
    return picked
