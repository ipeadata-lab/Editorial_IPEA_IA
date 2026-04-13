from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .docx_utils import extract_paragraphs_with_metadata


@dataclass(slots=True)
class Section:
    title: str
    start_idx: int
    end_idx: int


@dataclass(slots=True)
class LoadedDocument:
    kind: str
    chunks: list[str]
    refs: list[str]
    sections: list[Section]
    toc: list[str]


_HEADING_RE = re.compile(r"^(\d+(?:\.?\d+)*)\s+[A-ZÀ-Ü].+")
_REF_TYPE_RE = re.compile(r"\btipo=([a-z_]+)\b", re.IGNORECASE)


def _is_heading(text: str) -> bool:
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

    explicit = {"SINOPSE", "ABSTRACT", "REFERÊNCIAS", "INTRODUÇÃO", "APÊNDICE"}
    if t.upper() in explicit:
        return True

    return False


def _ref_block_type(ref: str) -> str:
    match = _REF_TYPE_RE.search(ref or "")
    return match.group(1).lower() if match else ""


def _build_sections(chunks: list[str], refs: list[str] | None = None) -> list[Section]:
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
    items = extract_paragraphs_with_metadata(path)
    chunks = [item.text for item in items]
    refs = [item.ref_label for item in items]
    sections = _build_sections(chunks, refs)
    toc = [f"{s.title} [{s.start_idx}-{s.end_idx}]" for s in sections]
    return LoadedDocument(kind="docx", chunks=chunks, refs=refs, sections=sections, toc=toc)


def _load_pdf(path: Path) -> LoadedDocument:
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
    return LoadedDocument(kind="pdf", chunks=chunks, refs=refs, sections=sections, toc=toc)


def load_document(path: Path) -> LoadedDocument:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _load_docx(path)
    if suffix == ".pdf":
        return _load_pdf(path)
    raise ValueError(f"Formato não suportado: {path.suffix}")
