from __future__ import annotations

from dataclasses import dataclass

from .abnt_document_types import (
    ABNT_TYPE_ARTICLE,
    ABNT_TYPE_BOOK,
    ABNT_TYPE_CHAPTER,
    ABNT_TYPE_GENERIC,
    ABNT_TYPE_INSTITUTIONAL_REPORT,
    ABNT_TYPE_LEGAL,
    ABNT_TYPE_ONLINE,
    ABNT_TYPE_THESIS,
)


@dataclass(frozen=True)
class AbntRuleRequirement:
    code: str
    message: str
    suggested_fix: str
    category: str = "reference_format"


@dataclass(frozen=True)
class AbntRuleSet:
    document_type: str
    requirements: tuple[AbntRuleRequirement, ...]


ABNT_RULES: dict[str, AbntRuleSet] = {
    ABNT_TYPE_ARTICLE: AbntRuleSet(
        document_type=ABNT_TYPE_ARTICLE,
        requirements=(
            AbntRuleRequirement(
                code="container",
                message="Referência de artigo sem identificação clara do periódico ou publicação de acolhimento.",
                suggested_fix="Completar a referência com o título do periódico ou publicação.",
            ),
        ),
    ),
    ABNT_TYPE_BOOK: AbntRuleSet(
        document_type=ABNT_TYPE_BOOK,
        requirements=(
            AbntRuleRequirement(
                code="place",
                message="Referência de livro sem local de publicação claramente identificado.",
                suggested_fix="Completar a referência com o local de publicação.",
            ),
            AbntRuleRequirement(
                code="publisher",
                message="Referência de livro sem editora claramente identificada.",
                suggested_fix="Completar a referência com a editora.",
            ),
        ),
    ),
    ABNT_TYPE_CHAPTER: AbntRuleSet(
        document_type=ABNT_TYPE_CHAPTER,
        requirements=(
            AbntRuleRequirement(
                code="in",
                message="Referência de capítulo sem marcador `In:` claramente informado.",
                suggested_fix="Identificar a obra de acolhimento com `In:`.",
            ),
            AbntRuleRequirement(
                code="container",
                message="Referência de capítulo sem identificação clara da obra de acolhimento.",
                suggested_fix="Completar a referência com o título da obra de acolhimento.",
            ),
        ),
    ),
    ABNT_TYPE_THESIS: AbntRuleSet(
        document_type=ABNT_TYPE_THESIS,
        requirements=(
            AbntRuleRequirement(
                code="institution",
                message="Tese ou dissertação sem instituição claramente identificada.",
                suggested_fix="Completar a referência com a instituição responsável.",
            ),
        ),
    ),
    ABNT_TYPE_ONLINE: AbntRuleSet(
        document_type=ABNT_TYPE_ONLINE,
        requirements=(
            AbntRuleRequirement(
                code="url",
                message="Referência online sem endereço eletrônico claramente informado.",
                suggested_fix="Completar a referência com a URL ou DOI.",
            ),
            AbntRuleRequirement(
                code="access_date",
                message="A referência online informa a URL, mas não traz `Acesso em:` ao final.",
                suggested_fix="Inserir `Acesso em:` com a data de consulta após a URL.",
            ),
        ),
    ),
    ABNT_TYPE_INSTITUTIONAL_REPORT: AbntRuleSet(
        document_type=ABNT_TYPE_INSTITUTIONAL_REPORT,
        requirements=(
            AbntRuleRequirement(
                code="institution",
                message="Documento institucional sem instituição ou órgão claramente identificados.",
                suggested_fix="Completar a referência com o órgão ou instituição responsável.",
            ),
        ),
    ),
    ABNT_TYPE_LEGAL: AbntRuleSet(
        document_type=ABNT_TYPE_LEGAL,
        requirements=(),
    ),
    ABNT_TYPE_GENERIC: AbntRuleSet(
        document_type=ABNT_TYPE_GENERIC,
        requirements=(),
    ),
}


def rule_set_for_document_type(document_type: str) -> AbntRuleSet:
    return ABNT_RULES.get(document_type, ABNT_RULES[ABNT_TYPE_GENERIC])


__all__ = ["ABNT_RULES", "AbntRuleRequirement", "AbntRuleSet", "rule_set_for_document_type"]
