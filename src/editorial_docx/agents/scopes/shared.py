from __future__ import annotations

import re

from ...document_loader import Section


def expand_neighbors(indexes: list[int], total: int, radius: int = 1) -> list[int]:
    expanded: set[int] = set()
    for idx in indexes:
        for candidate in range(max(0, idx - radius), min(total, idx + radius + 1)):
            expanded.add(candidate)
    return sorted(expanded)


def expand_section_ranges(sections: list[Section], keywords: tuple[str, ...]) -> list[int]:
    selected: list[int] = []
    for sec in sections:
        title = sec.title.lower()
        if any(keyword in title for keyword in keywords):
            selected.extend(range(sec.start_idx, sec.end_idx + 1))
    return sorted(dict.fromkeys(selected))


def find_content_indexes(chunks: list[str], pattern: str) -> list[int]:
    rx = re.compile(pattern, re.IGNORECASE)
    out: list[int] = []
    for idx, chunk in enumerate(chunks):
        if rx.search(chunk):
            out.append(idx)
    return out
