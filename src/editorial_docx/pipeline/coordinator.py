from __future__ import annotations

from .runtime import build_coordinator_answer


def build_coordinator_excerpt(total_chunks: int, agent_order: list[str], toc: list[str]) -> str:
    """Monta um resumo compacto do documento para a síntese final."""
    return (
        "Revisão por escopo de agente concluída. "
        f"Total de trechos no documento: {total_chunks}. "
        f"Agentes executados: {', '.join(agent_order)}.\n"
        "Sumário resumido:\n"
        + "\n".join(f"- {line}" for line in toc[:12])
    )


def coordinate_answer(question: str, comments) -> str:
    """Gera a resposta final consolidada a partir dos comentários aceitos."""
    return build_coordinator_answer(question=question, comments=comments)


__all__ = ["build_coordinator_excerpt", "coordinate_answer"]
