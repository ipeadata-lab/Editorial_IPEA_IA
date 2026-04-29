from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .docx_utils import extract_docx_user_comments, extract_paragraphs_with_metadata
from .models import DocumentUserComment
from .normalized_document import NormalizedDocument, build_normalized_document


@dataclass(slots=True)
class Section:
    title: str
    start_idx: int
    end_idx: int


@dataclass(slots=True)
class LoadedDocument:
    source_path: Path
    kind: str
    chunks: list[str]
    refs: list[str]
    sections: list[Section]
    toc: list[str]
    user_comments: list[DocumentUserComment]
    normalized_document: NormalizedDocument


_HEADING_RE = re.compile(r"^(\d+(?:\.?\d+)*)\s+[A-ZÀ-Ü].+")
_REF_TYPE_RE = re.compile(r"\btipo=([a-z_]+)\b", re.IGNORECASE)


def _is_heading(text: str) -> bool:
    """Handles is heading."""
    t = text.strip()
    if not t:
        return False
    if _HEADING_RE.match(t):
        return True

    words = t.split()
    if len(words) <= 14:
        letters = [ch for ch in t if ch.isalpha()]
        if letters:
            upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
            if upper_ratio >= 0.75:
                return True

    explicit = {"SINOPSE", "ABSTRACT", "REFERÊNCIAS", "INTRODUÇÃO", "APÊNDICE", "RESUMO"}
    if t.upper() in explicit:
        return True

    return False


def _ref_block_type(ref: str) -> str:
    """Handles ref block type."""
    match = _REF_TYPE_RE.search(ref or "")
    return match.group(1).lower() if match else ""


def _build_sections(chunks: list[str], refs: list[str] | None = None) -> list[Section]:
    """Handles build sections."""
    refs = refs or []
    headings = []
    for idx, text in enumerate(chunks):
        ref = refs[idx] if idx < len(refs) else ""
        ref_type = _ref_block_type(ref)
        if ref_type in {"heading", "reference_heading"} or (not ref_type and _is_heading(text)):
            headings.append((idx, text.strip()))
    if not headings:
        return [Section(title="Documento", start_idx=0, end_idx=max(0, len(chunks) - 1))] if chunks else []

    sections: list[Section] = []
    for i, (start_idx, title) in enumerate(headings):
        end_idx = headings[i + 1][0] - 1 if i + 1 < len(headings) else len(chunks) - 1
        sections.append(Section(title=title[:140], start_idx=start_idx, end_idx=end_idx))
    return sections


def _load_docx(path: Path) -> LoadedDocument:
    """Handles load docx."""
    items = extract_paragraphs_with_metadata(path)
    chunks = [item.text for item in items]
    refs = [item.ref_label for item in items]
    sections = _build_sections(chunks, refs)
    toc = [f"{s.title} [{s.start_idx}-{s.end_idx}]" for s in sections]
    user_comments = extract_docx_user_comments(path)
    return LoadedDocument(
        source_path=path,
        kind="docx",
        chunks=chunks,
        refs=refs,
        sections=sections,
        toc=toc,
        user_comments=user_comments,
        normalized_document=build_normalized_document(
            input_path=path,
            kind="docx",
            chunks=chunks,
            refs=refs,
            sections=sections,
            toc=toc,
            user_comments=user_comments,
        ),
    )


def _load_pdf(path: Path) -> LoadedDocument:
    """Handles load pdf."""
    reader = PdfReader(str(path))
    chunks: list[str] = []
    refs: list[str] = []

    for page_idx, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        block: list[str] = []
        block_idx = 1
        for line in lines:
            block.append(line)
            if len(" ".join(block)) >= 500:
                chunk = " ".join(block).strip()
                if chunk:
                    chunks.append(chunk)
                    refs.append(f"página {page_idx}, bloco {block_idx}")
                    block_idx += 1
                block = []

        if block:
            chunk = " ".join(block).strip()
            if chunk:
                chunks.append(chunk)
                refs.append(f"página {page_idx}, bloco {block_idx}")

    sections = _build_sections(chunks, refs)
    toc = [f"{s.title} [{s.start_idx}-{s.end_idx}]" for s in sections]
    user_comments: list[DocumentUserComment] = []
    return LoadedDocument(
        source_path=path,
        kind="pdf",
        chunks=chunks,
        refs=refs,
        sections=sections,
        toc=toc,
        user_comments=user_comments,
        normalized_document=build_normalized_document(
            input_path=path,
            kind="pdf",
            chunks=chunks,
            refs=refs,
            sections=sections,
            toc=toc,
            user_comments=user_comments,
        ),
    )


def load_normalized_document(path: Path) -> LoadedDocument:
    """Loads normalized document."""
    normalized = NormalizedDocument.from_json(path.read_text(encoding="utf-8"))
    chunks = [block.text for block in normalized.blocks]
    refs = [block.ref_label for block in normalized.blocks]
    sections = [Section(title=section.title, start_idx=section.start_idx, end_idx=section.end_idx) for section in normalized.sections]
    return LoadedDocument(
        source_path=path,
        kind=normalized.metadata.kind or "normalized",
        chunks=chunks,
        refs=refs,
        sections=sections,
        toc=normalized.toc[:],
        user_comments=normalized.user_comments[:],
        normalized_document=normalized,
    )


def load_document(path: Path) -> LoadedDocument:
    """Loads document."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _load_docx(path)
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix == ".json":
        return load_normalized_document(path)
    raise ValueError(f"Formato não suportado: {path.suffix}")
