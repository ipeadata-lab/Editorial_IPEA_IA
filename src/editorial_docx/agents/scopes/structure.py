from __future__ import annotations

from ...review_patterns import (
    _heading_word_count,
    _indexes_by_ref_type,
    _is_implicit_heading_candidate,
    _is_intro_heading,
)


def build_scope(chunks: list[str], refs: list[str], sections, total: int) -> list[int]:
    typed = _indexes_by_ref_type(refs, {"heading", "reference_heading"})
    section_starts = sorted(dict.fromkeys(sec.start_idx for sec in sections))
    intro_start = next(
        (
            idx
            for idx, chunk in enumerate(chunks)
            if _is_intro_heading(chunk) and _is_implicit_heading_candidate(idx, chunks, refs)
        ),
        None,
    )
    if intro_start is None:
        intro_start = next(
            (idx for idx in sorted(dict.fromkeys([*typed, *section_starts])) if 0 <= idx < len(chunks) and _is_intro_heading(chunks[idx])),
            None,
        )

    implicit = [
        idx
        for idx in range(intro_start if intro_start is not None else 0, total)
        if _is_implicit_heading_candidate(idx, chunks, refs)
    ]
    heading_candidates = sorted(dict.fromkeys([*typed, *section_starts, *implicit]))
    if not heading_candidates:
        return typed or list(range(max(1, int(total * 0.20))))

    scoped = [idx for idx in heading_candidates if intro_start is None or idx >= intro_start]
    explicit_scoped = [idx for idx in scoped if idx in set(typed) or idx in set(section_starts)]
    implicit_short_scoped = [
        idx for idx in scoped if idx not in set(explicit_scoped) and 0 <= idx < len(chunks) and _heading_word_count(chunks[idx]) <= 4
    ]
    return sorted(dict.fromkeys([*explicit_scoped, *implicit_short_scoped])) or scoped or heading_candidates
