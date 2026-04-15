from __future__ import annotations

import re

from ...models import AgentComment
from ...review_patterns import _folded_text, _ref_block_type


def heuristic_table_figure_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    source_prefixes = ("fonte:", "elaboração:", "elaboracao:")

    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        block_type = _ref_block_type(refs[idx])
        text = (chunks[idx] or "").strip()
        if block_type != "caption":
            continue
        norm = _folded_text(text)
        if re.match(r"^(tabela|figura|quadro|grafico)\s+\d+[:\s]", norm, flags=re.IGNORECASE):
            identifier = re.match(r"^((?:Tabela|Figura|Quadro|Gráfico)\s+\d+)[:\s]+(.+)$", text)
            fix = text
            if identifier:
                fix = f"Separar em duas linhas: `{identifier.group(1).upper()}` na primeira linha e `{identifier.group(2).strip()}` na linha abaixo."
            comments.append(
                AgentComment(
                    agent="tabelas_figuras",
                    category="Legenda",
                    message="Na legenda, o identificador deve ficar na primeira linha e o título descritivo na linha abaixo.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix=fix,
                )
            )
        next_idx = idx + 1
        if next_idx >= len(chunks):
            continue
        neighbor = (chunks[next_idx] or "").strip().casefold()
        if not any(neighbor.startswith(prefix) for prefix in source_prefixes) and _ref_block_type(refs[next_idx]) != "caption":
            comments.append(
                AgentComment(
                    agent="tabelas_figuras",
                    category="Fonte",
                    message="O bloco está sem uma linha de fonte ou elaboração logo abaixo da legenda.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix="Adicionar uma linha própria com `Fonte:` ou `Elaboração:` abaixo do bloco.",
                )
            )

    return comments


__all__ = ["heuristic_table_figure_comments"]
