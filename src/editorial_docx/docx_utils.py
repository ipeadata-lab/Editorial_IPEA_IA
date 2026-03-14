from __future__ import annotations

import re
import zipfile
import unicodedata
from dataclasses import dataclass
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


@dataclass(slots=True)
class ExtractedParagraph:
    text: str
    ref_label: str
    block_type: str
    style_name: str = ""


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


def _normalize_text_with_mapping(text: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    mapping: list[int] = []

    for idx, char in enumerate(text or ""):
        decomposed = unicodedata.normalize("NFD", char)
        stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
        if not stripped:
            continue

        lowered = stripped.lower()
        if lowered.isspace():
            normalized_chars.append(" ")
            mapping.append(idx)
            continue

        for part in lowered:
            normalized_chars.append(part)
            mapping.append(idx)

    collapsed_chars: list[str] = []
    collapsed_mapping: list[int] = []
    prev_space = False
    for char, idx in zip(normalized_chars, mapping):
        is_space = char.isspace()
        if is_space and prev_space:
            continue
        collapsed_chars.append(" " if is_space else char)
        collapsed_mapping.append(idx)
        prev_space = is_space

    return "".join(collapsed_chars), collapsed_mapping


def _find_excerpt_span(text: str, target: str) -> tuple[int, int] | None:
    if not text or not target:
        return None

    direct = re.search(re.escape(target), text, flags=re.IGNORECASE)
    if direct:
        return direct.span()

    normalized_text, text_mapping = _normalize_text_with_mapping(text)
    normalized_target, _ = _normalize_text_with_mapping(target)
    normalized_target = normalized_target.strip()
    if not normalized_text or not normalized_target:
        return None

    start = normalized_text.find(normalized_target)
    if start != -1:
        end = start + len(normalized_target) - 1
        return text_mapping[start], text_mapping[end] + 1

    return None


def _load_style_map(parts: dict[str, bytes]) -> dict[str, str]:
    raw = parts.get("word/styles.xml")
    if raw is None:
        return {}

    root = _parse_xml(raw)
    style_map: dict[str, str] = {}
    for style in root.findall(".//w:style", namespaces=NS):
        style_id = style.get(_qname(W_NS, "styleId")) or ""
        name = style.find("w:name", namespaces=NS)
        if style_id and name is not None:
            style_map[style_id] = name.get(_qname(W_NS, "val"), "")
    return style_map


def _paragraph_style_name(paragraph: etree._Element, style_map: dict[str, str]) -> str:
    style = paragraph.find("./w:pPr/w:pStyle", namespaces=NS)
    if style is None:
        return ""
    style_id = style.get(_qname(W_NS, "val"), "")
    return style_map.get(style_id, style_id)


def _paragraph_has_numbering(paragraph: etree._Element) -> bool:
    return paragraph.find("./w:pPr/w:numPr", namespaces=NS) is not None


def _has_ancestor(paragraph: etree._Element, tag: str) -> bool:
    parent = paragraph.getparent()
    while parent is not None:
        if parent.tag == _qname(W_NS, tag):
            return True
        parent = parent.getparent()
    return False


_REFERENCE_ENTRY_RE = re.compile(
    r"\b(doi|dispon[ií]vel em|acesso em|et al\.|https?://|v\.\s*\d+|n\.\s*\d+)\b",
    re.IGNORECASE,
)


def _looks_like_heading(
    text: str,
    style_name: str = "",
    position_ratio: float = 1.0,
    previous_text: str = "",
    next_text: str = "",
) -> bool:
    t = (text or "").strip()
    normalized_style = (style_name or "").strip().lower()
    if not t or len(t) > 140:
        return False

    style_heading = any(token in normalized_style for token in ("heading", "titulo", "título"))
    numbered_heading = bool(re.match(r"^\d+(?:\.\d+)*[.)]?\s+\S+", t))
    explicit_heading = t.upper() in {"SINOPSE", "ABSTRACT", "REFERÊNCIAS", "INTRODUÇÃO", "APÊNDICE", "ANEXO"}
    short_line = len(t) <= 90
    next_is_body = len((next_text or "").strip()) >= 80
    previous_looks_title = len((previous_text or "").strip()) <= 120
    early_document = position_ratio <= 0.20

    if style_heading and (short_line or numbered_heading or explicit_heading):
        return True
    if numbered_heading and (short_line or next_is_body):
        return True

    words = t.split()
    if 1 <= len(words) <= 14:
        letters = [ch for ch in t if ch.isalpha()]
        if letters:
            upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
            if upper_ratio >= 0.85 and (next_is_body or early_document or previous_looks_title):
                return True

    if explicit_heading:
        return True
    return False


def _classify_paragraph(
    paragraph: etree._Element,
    text: str,
    style_name: str,
    visible_index: int = 0,
    total_visible: int = 1,
    previous_text: str = "",
    next_text: str = "",
) -> str:
    normalized_style = (style_name or "").strip().lower()
    normalized_text = (text or "").strip()
    normalized_text_lower = normalized_text.lower()

    if _has_ancestor(paragraph, "tbl"):
        return "table_cell"
    if "caption" in normalized_style or "legenda" in normalized_style:
        return "caption"
    if (
        "heading" in normalized_style
        or "titulo" in normalized_style
        or "título" in normalized_style
        or _looks_like_heading(
            normalized_text,
            style_name=style_name,
            position_ratio=(visible_index / max(total_visible, 1)),
            previous_text=previous_text,
            next_text=next_text,
        )
    ):
        if "refer" in normalized_text_lower:
            return "reference_heading"
        return "heading"
    if "quote" in normalized_style or "citacao" in normalized_style or "citação" in normalized_style:
        return "direct_quote"
    if _paragraph_has_numbering(paragraph) or re.match(r"^\s*(?:[•\-–—]|[a-z0-9]+[.)])\s+", normalized_text, re.IGNORECASE):
        return "list_item"
    if _REFERENCE_ENTRY_RE.search(normalized_text):
        return "reference_entry"
    if normalized_text.startswith(("\"", "“")) and normalized_text.endswith(("\"", "”")):
        return "direct_quote"
    return "paragraph"


def extract_paragraphs_with_metadata(input_path: Path) -> list[ExtractedParagraph]:
    with zipfile.ZipFile(input_path, "r") as zin:
        parts = {name: zin.read(name) for name in zin.namelist()}

    style_map = _load_style_map(parts)
    document_root = _parse_xml(parts["word/document.xml"])
    paragraphs = document_root.findall(".//w:p", namespaces=NS)

    items: list[ExtractedParagraph] = []
    visible_paragraphs = [(paragraph, _paragraph_text(paragraph).strip()) for paragraph in paragraphs]
    visible_paragraphs = [(paragraph, text) for paragraph, text in visible_paragraphs if text]
    total_visible = len(visible_paragraphs)

    for visible_index, (paragraph, text) in enumerate(visible_paragraphs, start=1):
        style_name = _paragraph_style_name(paragraph, style_map)
        previous_text = visible_paragraphs[visible_index - 2][1] if visible_index - 2 >= 0 else ""
        next_text = visible_paragraphs[visible_index][1] if visible_index < total_visible else ""
        block_type = _classify_paragraph(
            paragraph,
            text,
            style_name,
            visible_index=visible_index,
            total_visible=total_visible,
            previous_text=previous_text,
            next_text=next_text,
        )
        ref_bits = [f"parágrafo {visible_index}", f"tipo={block_type}"]
        if style_name:
            ref_bits.append(f"estilo={style_name}")
        items.append(
            ExtractedParagraph(
                text=text,
                ref_label=" | ".join(ref_bits),
                block_type=block_type,
                style_name=style_name,
            )
        )
    return items


def _clone_run_with_text(run: etree._Element, text: str) -> etree._Element:
    cloned_run = etree.Element(_qname(W_NS, "r"))
    rpr = run.find("w:rPr", namespaces=NS)
    if rpr is not None:
        cloned_run.append(etree.fromstring(etree.tostring(rpr)))
    text_node = etree.SubElement(cloned_run, _qname(W_NS, "t"))
    if text.startswith(" ") or text.endswith(" ") or "  " in text:
        text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text_node.text = text
    return cloned_run


def _run_text(run: etree._Element) -> str:
    return "".join(node.text or "" for node in run.findall(".//w:t", namespaces=NS))


def _can_split_run(run: etree._Element) -> bool:
    allowed = {_qname(W_NS, "rPr"), _qname(W_NS, "t")}
    return all(child.tag in allowed for child in run)


def _split_run_at_offset(run: etree._Element, offset: int) -> tuple[etree._Element, etree._Element] | None:
    text = _run_text(run)
    if not text or offset <= 0 or offset >= len(text) or not _can_split_run(run):
        return None

    parent = run.getparent()
    if parent is None:
        return None

    left_run = _clone_run_with_text(run, text[:offset])
    right_run = _clone_run_with_text(run, text[offset:])
    run.addprevious(left_run)
    left_run.addnext(right_run)
    parent.remove(run)
    return left_run, right_run


def _apply_yellow_highlight(run: etree._Element) -> None:
    rpr = run.find("w:rPr", namespaces=NS)
    if rpr is None:
        rpr = etree.Element(_qname(W_NS, "rPr"))
        run.insert(0, rpr)
    highlight = rpr.find("w:highlight", namespaces=NS)
    if highlight is None:
        highlight = etree.SubElement(rpr, _qname(W_NS, "highlight"))
    highlight.set(_qname(W_NS, "val"), "yellow")


def _attach_comment(paragraph: etree._Element, comment_id: int, issue_excerpt: str | None = None) -> None:
    excerpt_span = _find_excerpt_span(_paragraph_text(paragraph), issue_excerpt or "")
    if excerpt_span:
        anchored = _attach_comment_to_span(paragraph, comment_id, excerpt_span)
        if anchored:
            return

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


def _attach_comment_to_span(paragraph: etree._Element, comment_id: int, span: tuple[int, int]) -> bool:
    start_offset, end_offset = span
    if start_offset < 0 or end_offset <= start_offset:
        return False

    runs = [run for run in paragraph.findall("w:r", namespaces=NS) if _run_text(run)]
    if not runs:
        return False

    positions: list[tuple[etree._Element, int, int]] = []
    cursor = 0
    for run in runs:
        text = _run_text(run)
        run_start = cursor
        run_end = cursor + len(text)
        positions.append((run, run_start, run_end))
        cursor = run_end

    start_entry = next((item for item in positions if item[1] <= start_offset < item[2]), None)
    end_entry = next((item for item in positions if item[1] < end_offset <= item[2]), None)
    if start_entry is None or end_entry is None:
        return False

    start_run, start_run_start, _ = start_entry
    end_run, end_run_start, end_run_end = end_entry

    if start_run is end_run:
        split_end = _split_run_at_offset(end_run, end_offset - end_run_start)
        if split_end is None and end_offset != end_run_end:
            return False
        target_run = split_end[0] if split_end is not None else end_run
        split_start = _split_run_at_offset(target_run, start_offset - start_run_start)
        if split_start is None and start_offset != start_run_start:
            return False
        first_selected = split_start[1] if split_start is not None else target_run
        last_selected = first_selected
    else:
        split_end = _split_run_at_offset(end_run, end_offset - end_run_start)
        if split_end is None and end_offset != end_run_end:
            return False
        last_selected = split_end[0] if split_end is not None else end_run

        split_start = _split_run_at_offset(start_run, start_offset - start_run_start)
        if split_start is None and start_offset != start_run_start:
            return False
        first_selected = split_start[1] if split_start is not None else start_run

    selected_runs: list[etree._Element] = []
    collecting = False
    for child in paragraph:
        if child is first_selected:
            collecting = True
        if collecting and child.tag == _qname(W_NS, "r"):
            selected_runs.append(child)
        if child is last_selected:
            break

    if not selected_runs:
        return False

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

    selected_runs[0].addprevious(start)
    selected_runs[-1].addnext(end)
    end.addnext(reference_run)
    for run in selected_runs:
        _apply_yellow_highlight(run)
    return True


def _append_comment_paragraph(comment: etree._Element, text: str) -> None:
    p = etree.SubElement(comment, _qname(W_NS, "p"))
    r = etree.SubElement(p, _qname(W_NS, "r"))
    t = etree.SubElement(r, _qname(W_NS, "t"))
    if text.startswith(" ") or text.endswith(" ") or "  " in text:
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text


def _append_comment(comments_root: etree._Element, comment_id: int, author: str, paragraphs: list[str]) -> None:
    comment = etree.SubElement(comments_root, _qname(W_NS, "comment"))
    comment.set(_qname(W_NS, "id"), str(comment_id))
    comment.set(_qname(W_NS, "author"), author)
    comment.set(_qname(W_NS, "date"), datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    for paragraph in paragraphs:
        _append_comment_paragraph(comment, paragraph)


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _build_review_note(item: AgentComment) -> str:
    if item.agent == "tipografia" and item.auto_apply:
        return "Ajuste tipográfico aplicado automaticamente."
    if item.agent == "estrutura" and item.auto_apply:
        return "Normalização estrutural aplicada automaticamente."
    if item.agent == "referencias" and item.auto_apply:
        return "Normalização de referência aplicada automaticamente."
    if item.agent == "tabelas_figuras" and item.auto_apply:
        return "Normalização de tabela/figura aplicada automaticamente."
    if item.review_status != "resolvido":
        return ""

    approved = _normalized_text(item.approved_text)
    suggestion = _normalized_text(item.suggested_fix)

    if approved and suggestion and approved == suggestion:
        return "Sugestão aplicada no painel assistido."
    if approved:
        return "Modificado pelo autor no painel assistido."
    return "Revisado no painel assistido."


def extract_paragraphs(input_path: Path) -> list[str]:
    return [item.text for item in extract_paragraphs_with_metadata(input_path)]


def _bool_from_spec(value: str) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "sim", "yes"}:
        return True
    if normalized in {"false", "0", "nao", "não", "no"}:
        return False
    return None


def _parse_format_spec(raw: str) -> dict[str, str]:
    spec: dict[str, str] = {}
    for part in (raw or "").split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            spec[key] = value
    return spec


def _text_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", (value or "").casefold())


def _is_safe_heading_normalization(item: AgentComment, original_text: str) -> bool:
    if item.agent != "estrutura" or not item.auto_apply:
        return False
    issue = (item.issue_excerpt or "").strip()
    suggestion = (item.suggested_fix or "").strip()
    original = (original_text or "").strip()
    if not issue or not suggestion or not original:
        return False
    if _normalized_text(issue) != _normalized_text(original):
        return False
    return _text_tokens(issue) == _text_tokens(suggestion) == _text_tokens(original)


def _is_safe_plain_text_normalization(item: AgentComment, original_text: str) -> bool:
    if item.agent not in {"estrutura", "referencias", "tabelas_figuras"} or not item.auto_apply:
        return False
    issue = (item.issue_excerpt or "").strip()
    suggestion = (item.suggested_fix or "").strip()
    original = (original_text or "").strip()
    if not issue or not suggestion or not original:
        return False
    if _normalized_text(issue) != _normalized_text(original):
        return False
    return _text_tokens(issue) == _text_tokens(suggestion) == _text_tokens(original)


def _replace_paragraph_text(paragraph: etree._Element, new_text: str) -> None:
    ppr = paragraph.find("w:pPr", namespaces=NS)
    first_run = paragraph.find("w:r", namespaces=NS)
    preserved_rpr = None
    if first_run is not None:
        existing_rpr = first_run.find("w:rPr", namespaces=NS)
        if existing_rpr is not None:
            preserved_rpr = etree.fromstring(etree.tostring(existing_rpr))

    for child in list(paragraph):
        if child is ppr:
            continue
        paragraph.remove(child)

    run = etree.SubElement(paragraph, _qname(W_NS, "r"))
    if preserved_rpr is not None:
        run.append(preserved_rpr)
    text_node = etree.SubElement(run, _qname(W_NS, "t"))
    text_node.text = new_text


def _ensure_child(parent: etree._Element, tag: str) -> etree._Element:
    child = parent.find(tag, namespaces=NS)
    if child is None:
        child = etree.SubElement(parent, _qname(W_NS, tag.split(":")[-1]))
    return child


def _set_on_off(rpr: etree._Element, tag: str, enabled: bool | None) -> None:
    existing = rpr.find(tag, namespaces=NS)
    if enabled is None:
        return
    if enabled:
        if existing is None:
            etree.SubElement(rpr, _qname(W_NS, tag.split(":")[-1]))
    elif existing is not None:
        rpr.remove(existing)


def _apply_run_formatting(run: etree._Element, spec: dict[str, str]) -> None:
    rpr = run.find("w:rPr", namespaces=NS)
    if rpr is None:
        rpr = etree.Element(_qname(W_NS, "rPr"))
        run.insert(0, rpr)

    font = spec.get("font")
    if font:
        rfonts = rpr.find("w:rFonts", namespaces=NS)
        if rfonts is None:
            rfonts = etree.SubElement(rpr, _qname(W_NS, "rFonts"))
        for attr in ("ascii", "hAnsi", "cs"):
            rfonts.set(_qname(W_NS, attr), font)

    if "size_pt" in spec:
        try:
            size_half_points = str(int(round(float(spec["size_pt"]) * 2)))
        except ValueError:
            size_half_points = ""
        if size_half_points:
            sz = rpr.find("w:sz", namespaces=NS)
            if sz is None:
                sz = etree.SubElement(rpr, _qname(W_NS, "sz"))
            sz.set(_qname(W_NS, "val"), size_half_points)
            szcs = rpr.find("w:szCs", namespaces=NS)
            if szcs is None:
                szcs = etree.SubElement(rpr, _qname(W_NS, "szCs"))
            szcs.set(_qname(W_NS, "val"), size_half_points)

    _set_on_off(rpr, "w:b", _bool_from_spec(spec.get("bold", "")))
    _set_on_off(rpr, "w:i", _bool_from_spec(spec.get("italic", "")))


def _apply_paragraph_formatting(paragraph: etree._Element, spec: dict[str, str]) -> None:
    ppr = paragraph.find("w:pPr", namespaces=NS)
    if ppr is None:
        ppr = etree.Element(_qname(W_NS, "pPr"))
        paragraph.insert(0, ppr)

    align_map = {
        "left": "left",
        "center": "center",
        "right": "right",
        "justify": "both",
    }
    align = align_map.get((spec.get("align") or "").strip().lower())
    if align:
        jc = ppr.find("w:jc", namespaces=NS)
        if jc is None:
            jc = etree.SubElement(ppr, _qname(W_NS, "jc"))
        jc.set(_qname(W_NS, "val"), align)

    if any(key in spec for key in ("space_before_pt", "space_after_pt", "line_spacing")):
        spacing = ppr.find("w:spacing", namespaces=NS)
        if spacing is None:
            spacing = etree.SubElement(ppr, _qname(W_NS, "spacing"))
        if "space_before_pt" in spec:
            try:
                spacing.set(_qname(W_NS, "before"), str(int(round(float(spec["space_before_pt"]) * 20))))
            except ValueError:
                pass
        if "space_after_pt" in spec:
            try:
                spacing.set(_qname(W_NS, "after"), str(int(round(float(spec["space_after_pt"]) * 20))))
            except ValueError:
                pass
        if "line_spacing" in spec:
            try:
                spacing.set(_qname(W_NS, "line"), str(int(round(float(spec["line_spacing"]) * 240))))
                spacing.set(_qname(W_NS, "lineRule"), "auto")
            except ValueError:
                pass

    if "left_indent_pt" in spec:
        try:
            left = str(int(round(float(spec["left_indent_pt"]) * 20)))
        except ValueError:
            left = ""
        if left:
            ind = ppr.find("w:ind", namespaces=NS)
            if ind is None:
                ind = etree.SubElement(ppr, _qname(W_NS, "ind"))
            ind.set(_qname(W_NS, "left"), left)

    runs = paragraph.findall("w:r", namespaces=NS)
    for run in runs:
        _apply_run_formatting(run, spec)


def _apply_auto_formatting(paragraphs: list[etree._Element], non_empty_indexes: list[int], comments: list[AgentComment]) -> None:
    for item in comments:
        paragraph_index = item.paragraph_index
        if paragraph_index is None or not (0 <= paragraph_index < len(non_empty_indexes)):
            continue
        paragraph = paragraphs[non_empty_indexes[paragraph_index]]
        if item.agent == "tipografia" and item.auto_apply:
            spec = _parse_format_spec(item.format_spec)
            if not spec:
                continue
            _apply_paragraph_formatting(paragraph, spec)
            continue
        if _is_safe_heading_normalization(item, _paragraph_text(paragraph)):
            _replace_paragraph_text(paragraph, item.suggested_fix.strip())
            continue
        if _is_safe_plain_text_normalization(item, _paragraph_text(paragraph)):
            _replace_paragraph_text(paragraph, item.suggested_fix.strip())


def _resolve_docx_paragraph_index(item: AgentComment, non_empty_indexes: list[int]) -> int | None:
    paragraph_index = item.paragraph_index
    if paragraph_index is not None and 0 <= paragraph_index < len(non_empty_indexes):
        return non_empty_indexes[paragraph_index]
    if paragraph_index is None and non_empty_indexes:
        return non_empty_indexes[0]
    return None


def _build_comment_lines_for_item(item: AgentComment, ordinal: int) -> list[str]:
    lines = [f"{ordinal}. [{item.agent}/{item.category}] {item.message}"]
    if item.suggested_fix:
        lines.append(f"Sugestão: {item.suggested_fix}")
    review_note = _build_review_note(item)
    if review_note:
        lines.append(review_note)
    if item.reviewer_note:
        lines.append(f"Observação: {item.reviewer_note}")
    return lines


def _spans_overlap(left: tuple[int, int] | None, right: tuple[int, int] | None) -> bool:
    if left is None or right is None:
        return False
    return not (left[1] <= right[0] or right[1] <= left[0])


def _group_comments_for_paragraph(paragraph_text: str, items: list[AgentComment]) -> list[list[AgentComment]]:
    groups: list[dict[str, object]] = []

    for item in items:
        excerpt = (item.issue_excerpt or "").strip()
        span = _find_excerpt_span(paragraph_text, excerpt) if excerpt else None
        normalized_excerpt = _normalized_text(excerpt)

        if span is None:
            if groups:
                groups[0]["items"].append(item)  # type: ignore[index]
            else:
                groups.append({"span": None, "excerpt": normalized_excerpt, "items": [item]})
            continue

        matched_group: dict[str, object] | None = None
        for group in groups:
            group_span = group["span"]  # type: ignore[index]
            group_excerpt = group["excerpt"]  # type: ignore[index]
            if _spans_overlap(span, group_span if isinstance(group_span, tuple) else None):
                matched_group = group
                break
            if normalized_excerpt and normalized_excerpt == group_excerpt:
                matched_group = group
                break

        if matched_group is None:
            groups.append({"span": span, "excerpt": normalized_excerpt, "items": [item]})
        else:
            matched_group["items"].append(item)  # type: ignore[index]

    groups.sort(key=lambda group: (group["span"][0] if isinstance(group["span"], tuple) else -1))  # type: ignore[index]
    return [group["items"] for group in groups]  # type: ignore[index]


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
    _apply_auto_formatting(paragraphs, non_empty_indexes, comments)
    visible_comments = [item for item in comments if not item.auto_apply]

    grouped_comments: dict[int, list[AgentComment]] = {}
    for item in visible_comments:
        paragraph_index = _resolve_docx_paragraph_index(item, non_empty_indexes)
        if paragraph_index is None or paragraph_index < 0 or paragraph_index >= len(paragraphs):
            continue
        grouped_comments.setdefault(paragraph_index, []).append(item)

    comment_id = _next_comment_id(comments_root)
    for paragraph_index in sorted(grouped_comments):
        paragraph_text = _paragraph_text(paragraphs[paragraph_index])
        comment_groups = _group_comments_for_paragraph(paragraph_text, grouped_comments[paragraph_index])
        for items in comment_groups:
            comment_lines = ["Achados consolidados neste trecho:"]
            for ordinal, item in enumerate(items, start=1):
                comment_lines.extend(_build_comment_lines_for_item(item, ordinal))
            agents = ", ".join(sorted({item.agent for item in items}))
            author = f"Revisão: {agents}"[:255]
            anchor_excerpt = next((item.issue_excerpt for item in items if (item.issue_excerpt or "").strip()), "")

            _append_comment(comments_root, comment_id, author=author, paragraphs=comment_lines)
            _attach_comment(paragraphs[paragraph_index], comment_id, issue_excerpt=anchor_excerpt)
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
