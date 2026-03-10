from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .models import AgentComment

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W_NS, "r": R_NS}

COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
COMMENTS_PART = "word/comments.xml"


def _qname(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _parse_xml(raw: bytes) -> etree._Element:
    return etree.fromstring(raw)


def _serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone="yes")


def _ensure_comments_part(parts: dict[str, bytes]) -> etree._Element:
    comments_raw = parts.get(COMMENTS_PART)
    if comments_raw is None:
        root = etree.Element(_qname(W_NS, "comments"), nsmap={"w": W_NS})
        parts[COMMENTS_PART] = _serialize_xml(root)
        return root
    return _parse_xml(comments_raw)


def _ensure_comments_content_type(content_types_root: etree._Element) -> None:
    for override in content_types_root.findall(_qname(CT_NS, "Override")):
        if override.get("PartName") == "/word/comments.xml":
            return
    etree.SubElement(
        content_types_root,
        _qname(CT_NS, "Override"),
        PartName="/word/comments.xml",
        ContentType=COMMENTS_CONTENT_TYPE,
    )


def _ensure_comments_relationship(rels_root: etree._Element) -> None:
    for rel in rels_root.findall(_qname(PR_NS, "Relationship")):
        if rel.get("Type") == COMMENTS_REL_TYPE:
            return

    existing_ids = {
        rel.get("Id")
        for rel in rels_root.findall(_qname(PR_NS, "Relationship"))
        if rel.get("Id", "").startswith("rId") and rel.get("Id", "")[3:].isdigit()
    }

    next_idx = 1
    while f"rId{next_idx}" in existing_ids:
        next_idx += 1

    etree.SubElement(
        rels_root,
        _qname(PR_NS, "Relationship"),
        Id=f"rId{next_idx}",
        Type=COMMENTS_REL_TYPE,
        Target="comments.xml",
    )


def _next_comment_id(comments_root: etree._Element) -> int:
    ids = []
    for comment in comments_root.findall(_qname(W_NS, "comment")):
        raw_id = comment.get(_qname(W_NS, "id"))
        if raw_id and raw_id.isdigit():
            ids.append(int(raw_id))
    return (max(ids) + 1) if ids else 0


def _paragraph_text(paragraph: etree._Element) -> str:
    text_parts = []
    for t in paragraph.findall(".//w:t", namespaces=NS):
        text_parts.append(t.text or "")
    return "".join(text_parts)


def _attach_comment(paragraph: etree._Element, comment_id: int) -> None:
    children = list(paragraph)
    start = etree.Element(_qname(W_NS, "commentRangeStart"))
    start.set(_qname(W_NS, "id"), str(comment_id))

    end = etree.Element(_qname(W_NS, "commentRangeEnd"))
    end.set(_qname(W_NS, "id"), str(comment_id))

    reference_run = etree.Element(_qname(W_NS, "r"))
    rpr = etree.SubElement(reference_run, _qname(W_NS, "rPr"))
    rstyle = etree.SubElement(rpr, _qname(W_NS, "rStyle"))
    rstyle.set(_qname(W_NS, "val"), "CommentReference")
    cref = etree.SubElement(reference_run, _qname(W_NS, "commentReference"))
    cref.set(_qname(W_NS, "id"), str(comment_id))

    if children:
        first = children[0]
        first.addprevious(start)

        anchor = children[-1]
        anchor.addnext(end)
        end.addnext(reference_run)
    else:
        paragraph.append(start)
        paragraph.append(end)
        paragraph.append(reference_run)


def _append_comment(comments_root: etree._Element, comment_id: int, author: str, text: str) -> None:
    comment = etree.SubElement(comments_root, _qname(W_NS, "comment"))
    comment.set(_qname(W_NS, "id"), str(comment_id))
    comment.set(_qname(W_NS, "author"), author)
    comment.set(_qname(W_NS, "date"), datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    p = etree.SubElement(comment, _qname(W_NS, "p"))
    r = etree.SubElement(p, _qname(W_NS, "r"))
    t = etree.SubElement(r, _qname(W_NS, "t"))
    t.text = text


def extract_paragraphs(input_path: Path) -> list[str]:
    with zipfile.ZipFile(input_path, "r") as zin:
        document_root = _parse_xml(zin.read("word/document.xml"))
    paragraphs = document_root.findall(".//w:p", namespaces=NS)
    return [_paragraph_text(p).strip() for p in paragraphs if _paragraph_text(p).strip()]


def apply_comments_to_docx(input_path: Path, comments: list[AgentComment]) -> bytes:
    with zipfile.ZipFile(input_path, "r") as zin:
        parts = {name: zin.read(name) for name in zin.namelist()}

    document_root = _parse_xml(parts["word/document.xml"])
    content_types_root = _parse_xml(parts["[Content_Types].xml"])
    rels_root = _parse_xml(parts["word/_rels/document.xml.rels"])
    comments_root = _ensure_comments_part(parts)

    _ensure_comments_content_type(content_types_root)
    _ensure_comments_relationship(rels_root)

    paragraphs = document_root.findall(".//w:p", namespaces=NS)
    non_empty_indexes = [i for i, p in enumerate(paragraphs) if _paragraph_text(p).strip()]

    comment_id = _next_comment_id(comments_root)
    for item in comments:
        paragraph_index = item.paragraph_index
        # Agent indexes are based on extracted non-empty chunks.
        if paragraph_index is not None and 0 <= paragraph_index < len(non_empty_indexes):
            paragraph_index = non_empty_indexes[paragraph_index]
        if paragraph_index is None and non_empty_indexes:
            paragraph_index = non_empty_indexes[0]
        if paragraph_index is None or paragraph_index < 0 or paragraph_index >= len(paragraphs):
            continue
            
        comment_lines = [f"[{item.category}] {item.message}"]
        if item.issue_excerpt:
            comment_lines.append(f"Trecho com problema: {item.issue_excerpt}")
        if item.suggested_fix:
            comment_lines.append(f"Sugestão: {item.suggested_fix}")
        message = "\n".join(comment_lines)
        author = f"Agente: {item.agent}"

        _append_comment(comments_root, comment_id, author=author, text=message)
        _attach_comment(paragraphs[paragraph_index], comment_id)
        comment_id += 1

    parts["word/document.xml"] = _serialize_xml(document_root)
    parts["[Content_Types].xml"] = _serialize_xml(content_types_root)
    parts["word/_rels/document.xml.rels"] = _serialize_xml(rels_root)
    parts[COMMENTS_PART] = _serialize_xml(comments_root)

    from io import BytesIO

    out = BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, raw in parts.items():
            zout.writestr(name, raw)
    return out.getvalue()
