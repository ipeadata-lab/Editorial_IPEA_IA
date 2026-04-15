from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import DocumentUserComment
from .review_patterns import _ref_block_type, _ref_style_name
from .token_utils import count_tokens


@dataclass(slots=True)
class NormalizedBlock:
    index: int
    text: str
    ref_label: str
    block_type: str
    style_name: str
    section_title: str = ""
    token_count: int = 0


@dataclass(slots=True)
class NormalizedSection:
    title: str
    start_idx: int
    end_idx: int


@dataclass(slots=True)
class NormalizedReference:
    index: int
    text: str


@dataclass(slots=True)
class SourceMetadata:
    input_path: str
    kind: str
    generated_at: str
    paragraph_count: int
    reference_count: int
    user_comment_count: int


@dataclass(slots=True)
class NormalizedDocument:
    metadata: SourceMetadata
    toc: list[str]
    blocks: list[NormalizedBlock] = field(default_factory=list)
    sections: list[NormalizedSection] = field(default_factory=list)
    user_comments: list[DocumentUserComment] = field(default_factory=list)
    references: list[NormalizedReference] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "metadata": asdict(self.metadata),
            "toc": self.toc[:],
            "blocks": [asdict(item) for item in self.blocks],
            "sections": [asdict(item) for item in self.sections],
            "user_comments": [asdict(item) for item in self.user_comments],
            "references": [asdict(item) for item in self.references],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "NormalizedDocument":
        metadata_raw = data.get("metadata") or {}
        return cls(
            metadata=SourceMetadata(
                input_path=str(metadata_raw.get("input_path") or ""),
                kind=str(metadata_raw.get("kind") or ""),
                generated_at=str(metadata_raw.get("generated_at") or ""),
                paragraph_count=int(metadata_raw.get("paragraph_count") or 0),
                reference_count=int(metadata_raw.get("reference_count") or 0),
                user_comment_count=int(metadata_raw.get("user_comment_count") or 0),
            ),
            toc=[str(item) for item in (data.get("toc") or [])],
            blocks=[
                NormalizedBlock(
                    index=int(item.get("index") or 0),
                    text=str(item.get("text") or ""),
                    ref_label=str(item.get("ref_label") or ""),
                    block_type=str(item.get("block_type") or ""),
                    style_name=str(item.get("style_name") or ""),
                    section_title=str(item.get("section_title") or ""),
                    token_count=int(item.get("token_count") or 0),
                )
                for item in (data.get("blocks") or [])
                if isinstance(item, dict)
            ],
            sections=[
                NormalizedSection(
                    title=str(item.get("title") or ""),
                    start_idx=int(item.get("start_idx") or 0),
                    end_idx=int(item.get("end_idx") or 0),
                )
                for item in (data.get("sections") or [])
                if isinstance(item, dict)
            ],
            user_comments=[
                DocumentUserComment(
                    comment_id=int(item.get("comment_id") or 0),
                    author=str(item.get("author") or ""),
                    text=str(item.get("text") or ""),
                    paragraph_index=(int(item["paragraph_index"]) if item.get("paragraph_index") is not None else None),
                    anchor_excerpt=str(item.get("anchor_excerpt") or ""),
                    paragraph_text=str(item.get("paragraph_text") or ""),
                )
                for item in (data.get("user_comments") or [])
                if isinstance(item, dict)
            ],
            references=[
                NormalizedReference(
                    index=int(item.get("index") or 0),
                    text=str(item.get("text") or ""),
                )
                for item in (data.get("references") or [])
                if isinstance(item, dict)
            ],
        )

    @classmethod
    def from_json(cls, raw: str) -> "NormalizedDocument":
        return cls.from_dict(json.loads(raw))

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def build_normalized_document(
    *,
    input_path: Path,
    kind: str,
    chunks: list[str],
    refs: list[str],
    sections,
    toc: list[str],
    user_comments: list[DocumentUserComment],
) -> NormalizedDocument:
    """Constrói um artefato persistível e independente da etapa de review."""
    section_title_by_index: dict[int, str] = {}
    normalized_sections: list[NormalizedSection] = []
    for section in sections:
        normalized_sections.append(
            NormalizedSection(title=section.title, start_idx=section.start_idx, end_idx=section.end_idx)
        )
        for index in range(section.start_idx, section.end_idx + 1):
            section_title_by_index[index] = section.title

    blocks: list[NormalizedBlock] = []
    references: list[NormalizedReference] = []
    for index, (chunk, ref) in enumerate(zip(chunks, refs)):
        block_type = _ref_block_type(ref)
        block = NormalizedBlock(
            index=index,
            text=chunk,
            ref_label=ref,
            block_type=block_type,
            style_name=_ref_style_name(ref),
            section_title=section_title_by_index.get(index, ""),
            token_count=count_tokens(chunk),
        )
        blocks.append(block)
        if block_type == "reference_entry" and (chunk or "").strip():
            references.append(NormalizedReference(index=index, text=chunk))

    metadata = SourceMetadata(
        input_path=str(input_path),
        kind=kind,
        generated_at=datetime.now(timezone.utc).isoformat(),
        paragraph_count=len(chunks),
        reference_count=len(references),
        user_comment_count=len(user_comments),
    )
    return NormalizedDocument(
        metadata=metadata,
        toc=toc[:],
        blocks=blocks,
        sections=normalized_sections,
        user_comments=list(user_comments),
        references=references,
    )


__all__ = [
    "NormalizedBlock",
    "NormalizedDocument",
    "NormalizedReference",
    "NormalizedSection",
    "SourceMetadata",
    "build_normalized_document",
]
