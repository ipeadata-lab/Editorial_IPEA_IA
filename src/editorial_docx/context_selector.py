from __future__ import annotations

import re

from .document_loader import Section

_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÿ0-9]{3,}")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def select_chunk_indexes(
    question: str,
    chunks: list[str],
    sections: list[Section],
    max_chunks: int = 36,
) -> list[int]:
    if not chunks:
        return []

    q = _tokens(question)
    if not q:
        return list(range(min(max_chunks, len(chunks))))

    section_scores: list[tuple[float, Section]] = []
    for s in sections:
        title_tokens = _tokens(s.title)
        score = float(len(q & title_tokens))
        section_scores.append((score, s))

    section_scores.sort(key=lambda x: x[0], reverse=True)
    picked_ranges: list[range] = []
    for score, section in section_scores[:3]:
        if score <= 0:
            continue
        picked_ranges.append(range(section.start_idx, section.end_idx + 1))

    candidate_idxs: set[int] = set()
    for r in picked_ranges:
        candidate_idxs.update(r)
    if not candidate_idxs:
        candidate_idxs = set(range(len(chunks)))

    scored: list[tuple[int, int]] = []
    for idx in sorted(candidate_idxs):
        c_tokens = _tokens(chunks[idx])
        overlap = len(q & c_tokens)
        scored.append((overlap, idx))

    scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    selected = [idx for score, idx in scored if score > 0][:max_chunks]

    if len(selected) < min(10, max_chunks):
        fallback = [idx for _, idx in scored][:max_chunks]
        selected = list(dict.fromkeys(selected + fallback))[:max_chunks]

    expanded: set[int] = set()
    for idx in selected:
        expanded.add(idx)
        if idx - 1 >= 0:
            expanded.add(idx - 1)
        if idx + 1 < len(chunks):
            expanded.add(idx + 1)

    out = sorted(expanded)
    return out[:max_chunks]


def build_excerpt(indexes: list[int], chunks: list[str], refs: list[str], max_chars: int = 12000) -> str:
    lines: list[str] = []
    total = 0
    for idx in indexes:
        if idx < 0 or idx >= len(chunks):
            continue
        ref = refs[idx] if idx < len(refs) else "sem referência"
        line = f"[{idx}] ({ref}) {chunks[idx]}"
        total += len(line) + 1
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)
