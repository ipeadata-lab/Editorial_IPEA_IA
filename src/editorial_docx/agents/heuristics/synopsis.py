from __future__ import annotations

import re

from ...models import AgentComment
from ...review_patterns import _folded_text, _ref_align, _ref_block_type


def heuristic_synopsis_comments(batch_indexes: list[int], chunks: list[str], refs: list[str]) -> list[AgentComment]:
    comments: list[AgentComment] = []
    for idx in batch_indexes:
        if not (0 <= idx < len(chunks)) or idx >= len(refs):
            continue
        block_type = _ref_block_type(refs[idx])
        text = chunks[idx] or ""
        if block_type == "abstract_body" and _ref_align(refs[idx]) and _ref_align(refs[idx]) != "justify":
            comments.append(
                AgentComment(
                    agent="sinopse_abstract",
                    category="alignment",
                    message="O abstract deve estar justificado, mas este parágrafo está com outro alinhamento.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix="Justificar o parágrafo do abstract.",
                )
            )
        if block_type == "keywords_content":
            entries = [item.strip() for item in re.split(r"[;,]", text) if item.strip()]
            folded = [_folded_text(item) for item in entries]
            if len(set(folded)) != len(folded):
                comments.append(
                    AgentComment(
                        agent="sinopse_abstract",
                        category="keywords",
                        message="Há repetição de palavras-chave na lista.",
                        paragraph_index=idx,
                        issue_excerpt=text,
                        suggested_fix="Remover as repetições e manter apenas entradas únicas.",
                    )
                )
        if block_type == "abstract_body" and len(re.findall(r"[A-Za-zÀ-ÿ0-9]+", text)) > 250:
            comments.append(
                AgentComment(
                    agent="sinopse_abstract",
                    category="Extensão",
                    message="O abstract ultrapassa o limite de 250 palavras.",
                    paragraph_index=idx,
                    issue_excerpt=text,
                    suggested_fix="Reduzir o abstract para até 250 palavras.",
                )
            )
    return comments


__all__ = ["heuristic_synopsis_comments"]
