from __future__ import annotations

import re

from ...models import AgentComment
from ...review_patterns import _folded_text, _heading_word_count, _ref_block_type, _ref_has_numbering


def is_same_top_level_heading(ref: str) -> bool:
    return _ref_block_type(ref) in {"heading", "reference_heading"} and "numerado=sim" in ref.casefold()


def is_final_section_heading(text: str) -> bool:
    folded = _folded_text(text)
    return folded.startswith("consideracoes finais") or folded.startswith("conclus")


def heuristic_structure_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    heading_indexes = [idx for idx in batch_indexes if 0 <= idx < len(refs) and _ref_block_type(refs[idx]) in {"heading", "reference_heading"}]
    if not heading_indexes:
        return comments

    if any(_ref_has_numbering(refs[idx]) for idx in heading_indexes):
        next_number = 1
        for idx in heading_indexes:
            text = (chunks[idx] or "").strip()
            if _ref_has_numbering(refs[idx]):
                match = re.match(r"^\s*(\d+)", text)
                if match:
                    next_number = int(match.group(1)) + 1
                else:
                    next_number += 1
                continue
            if _heading_word_count(text) == 0:
                continue
            comments.append(
                AgentComment(
                    agent="estrutura",
                    category="numeração e hierarquia de seções",
                    message="Este título deveria seguir a sequência de numeração das seções.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix=f"{next_number}. {text}",
                    auto_apply=True,
                )
            )
            next_number += 1
    return comments


__all__ = ["heuristic_structure_comments", "is_final_section_heading", "is_same_top_level_heading"]
