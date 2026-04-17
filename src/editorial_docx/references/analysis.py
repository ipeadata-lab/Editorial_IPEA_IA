from __future__ import annotations

from ..abnt_citation_parser import extract_citation_candidates
from ..abnt_matcher import compare_citations_to_references
from ..abnt_reference_parser import parse_reference_entry
from ..abnt_validator import validate_reference_entry
from ..models import (
    ReferenceAbntIssueRecord,
    ReferenceAnchor,
    ReferenceBodyCitation,
    ReferenceEntryRecord,
    ReferencePipelineArtifact,
)
from ..review_patterns import _is_non_body_reference_context, _ref_block_type
from ..agents.heuristics.references import NON_AUTHOR_REFERENCE_TOKENS


def build_reference_pipeline_artifact(chunks: list[str], refs: list[str]) -> ReferencePipelineArtifact:
    reference_heading_idx = next((idx for idx, ref in enumerate(refs) if _ref_block_type(ref) == "reference_heading"), None)
    if reference_heading_idx is None:
        return ReferencePipelineArtifact()

    body_limit = reference_heading_idx
    citation_candidates = extract_citation_candidates(
        chunks,
        refs,
        body_limit,
        is_non_body_context=_is_non_body_reference_context,
        blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS,
    )
    body_citations = [
        ReferenceBodyCitation(
            paragraph_index=item.paragraph_index,
            excerpt=item.excerpt,
            label=item.label,
            key=item.key,
        )
        for item in citation_candidates
    ]

    parsed_entries: list[tuple[int, object]] = [
        (idx, parsed)
        for idx, (chunk, ref) in enumerate(zip(chunks[reference_heading_idx + 1 :], refs[reference_heading_idx + 1 :]), start=reference_heading_idx + 1)
        if _ref_block_type(ref) == "reference_entry"
        for parsed in [parse_reference_entry(chunk, blocked_author_tokens=NON_AUTHOR_REFERENCE_TOKENS)]
        if parsed is not None
    ]
    reference_entries = [
        ReferenceEntryRecord(
            paragraph_index=idx,
            raw_text=parsed.raw_text,
            label=parsed.label,
            key=parsed.key,
            document_type=parsed.document_type,
            publication_year=parsed.publication_year,
        )
        for idx, parsed in parsed_entries
    ]

    match_result = compare_citations_to_references(citation_candidates, [parsed for _, parsed in parsed_entries])
    entry_index_by_id = {id(parsed): idx for idx, parsed in parsed_entries}

    exact_anchors = [
        ReferenceAnchor(
            citation_paragraph_index=item.citation.paragraph_index,
            citation_excerpt=item.citation.excerpt,
            citation_label=item.citation.label,
            reference_paragraph_index=entry_index_by_id.get(id(item.reference)),
            reference_label=item.reference.label,
            status="exact",
            confidence=1.0,
        )
        for item in match_result.exact_matches
    ]
    probable_anchors = [
        ReferenceAnchor(
            citation_paragraph_index=item.citation.paragraph_index,
            citation_excerpt=item.citation.excerpt,
            citation_label=item.citation.label,
            reference_paragraph_index=entry_index_by_id.get(id(item.reference)),
            reference_label=item.reference.label,
            status=item.match_type,
            confidence=item.confidence,
        )
        for item in match_result.probable_matches
    ]
    missing_citations = [
        ReferenceBodyCitation(
            paragraph_index=item.paragraph_index,
            excerpt=item.excerpt,
            label=item.label,
            key=item.key,
        )
        for item in match_result.missing_citations
    ]
    uncited_references = [
        ReferenceEntryRecord(
            paragraph_index=entry_index_by_id.get(id(item), -1),
            raw_text=item.raw_text,
            label=item.label,
            key=item.key,
            document_type=item.document_type,
            publication_year=item.publication_year,
        )
        for item in match_result.uncited_references
    ]
    abnt_issues = [
        ReferenceAbntIssueRecord(
            paragraph_index=idx,
            code=issue.code,
            message=issue.message,
            suggested_fix=issue.suggested_fix,
            category=issue.category,
        )
        for idx, parsed in parsed_entries
        for issue in validate_reference_entry(parsed)
    ]

    return ReferencePipelineArtifact(
        body_citations=body_citations,
        reference_entries=reference_entries,
        exact_anchors=exact_anchors,
        probable_anchors=probable_anchors,
        missing_citations=missing_citations,
        uncited_references=uncited_references,
        abnt_issues=abnt_issues,
    )


__all__ = ["build_reference_pipeline_artifact"]
