from __future__ import annotations

from dataclasses import dataclass

from .abnt_reference_parser import ParsedReferenceEntry
from .abnt_rules import rule_set_for_document_type


@dataclass(frozen=True)
class AbntValidationIssue:
    code: str
    message: str
    suggested_fix: str
    category: str


def _missing_requirement(entry: ParsedReferenceEntry, code: str) -> bool:
    if code == "container":
        return not entry.container_title
    if code == "place":
        return not entry.place
    if code == "publisher":
        return not entry.publisher
    if code == "institution":
        return not entry.institution
    if code == "in":
        return not entry.has_in
    if code == "url":
        return not (entry.has_url or entry.has_doi)
    if code == "access_date":
        return not entry.has_access_date
    return False


def validate_reference_entry(entry: ParsedReferenceEntry) -> list[AbntValidationIssue]:
    issues: list[AbntValidationIssue] = []
    for requirement in rule_set_for_document_type(entry.document_type).requirements:
        if _missing_requirement(entry, requirement.code):
            issues.append(
                AbntValidationIssue(
                    code=requirement.code,
                    message=requirement.message,
                    suggested_fix=requirement.suggested_fix,
                    category=requirement.category,
                )
            )
    if entry.has_url and not entry.has_access_date:
        issues.append(
            AbntValidationIssue(
                code="access_date",
                message="A referência online informa a URL, mas não traz `Acesso em:` ao final.",
                suggested_fix="Inserir `Acesso em:` com a data de consulta após a URL.",
                category="reference_format",
            )
        )
    if "Disponível em:" in entry.raw_text or "Disponivel em:" in entry.raw_text:
        if not (entry.has_url or entry.has_doi):
            issues.append(
                AbntValidationIssue(
                    code="url",
                    message="A referência informa `Disponível em:`, mas não traz um endereço eletrônico válido.",
                    suggested_fix="Completar a referência com a URL ou DOI correspondente.",
                    category="reference_format",
                )
            )
    return issues


__all__ = ["AbntValidationIssue", "validate_reference_entry"]
