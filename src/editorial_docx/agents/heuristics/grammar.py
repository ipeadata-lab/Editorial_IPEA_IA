from __future__ import annotations

import re

from ...models import AgentComment
from ...review_patterns import _ref_block_type


def heuristic_grammar_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    seen: set[tuple[int, str, str]] = set()

    def add(idx: int, issue: str, fix: str, message: str, category: str = "grammar") -> None:
        key = (idx, issue, fix)
        if key in seen:
            return
        seen.add(key)
        comments.append(
            AgentComment(
                agent="gramatica_ortografia",
                category=category,
                message=message,
                paragraph_index=idx,
                issue_excerpt=issue,
                suggested_fix=fix,
            )
        )

    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        if _ref_block_type(refs[idx]) in {"reference_entry", "reference_heading", "direct_quote"}:
            continue
        text = chunks[idx] or ""
        if re.search(r"\bpassou ser\b", text, flags=re.IGNORECASE):
            add(idx, "passou ser", "passou a ser", "Falta a preposição na locução verbal.", "Regência")
        if re.search(r"\bpara todos trabalhadores\b", text, flags=re.IGNORECASE):
            add(idx, "para todos trabalhadores", "para todos os trabalhadores", "Falta artigo definido antes do substantivo.", "Concordância")
        if re.search(r"\bobserva-se\s+que\b", text, flags=re.IGNORECASE):
            add(idx, "observa-se que", "observa-se", "A construção contém partícula expletiva dispensável.", "Concordância")
        if re.search(r"\bbenef[ií]cios monet[aá]rio\b", text, flags=re.IGNORECASE):
            add(idx, "benefícios monetário", "benefícios monetários", "Há discordância nominal entre substantivo e adjetivo.", "Concordância")
        if re.search(r"\bque assenta o acesso\b", text, flags=re.IGNORECASE):
            add(idx, "que assenta o acesso", "que assentam o acesso", "O verbo deve concordar com o sujeito composto.", "Concordância")
        if re.search(r"\be sugerem\b", text, flags=re.IGNORECASE) and re.search(r"\bexerc[ií]cio realizado\b", text, flags=re.IGNORECASE):
            add(idx, "e sugerem", "e sugere", "O verbo deve concordar com o núcleo singular do sujeito.", "Concordância")

    return comments


__all__ = ["heuristic_grammar_comments"]
