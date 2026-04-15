from __future__ import annotations

from ...review_patterns import _find_metadata_like_indexes
from .shared import expand_section_ranges


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    sec = expand_section_ranges(sections, ("metadad", "ficha catalogr", "capa", "titulo", "autoria"))
    head_candidates = _find_metadata_like_indexes(chunks, refs, limit=18)
    picked = sorted(dict.fromkeys([*sec, *head_candidates]))
    return picked or head_candidates or list(range(min(12, total)))
