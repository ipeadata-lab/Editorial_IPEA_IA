from __future__ import annotations

from ...review_patterns import _indexes_by_ref_type, _ref_block_type, _ref_style_name, _style_name_looks_explicit


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    typed = _indexes_by_ref_type(refs, {"heading", "caption", "reference_entry", "reference_heading"})
    styled = [
        idx
        for idx, ref in enumerate(refs)
        if _ref_block_type(ref) == "paragraph" and _style_name_looks_explicit(_ref_style_name(ref)) and idx < 24
    ]
    return sorted(dict.fromkeys([*typed, *styled])) or typed or list(range(max(1, int(total * 0.20))))
