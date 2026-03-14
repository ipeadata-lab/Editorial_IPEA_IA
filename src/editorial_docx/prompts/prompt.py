from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile

from langchain_core.prompts import ChatPromptTemplate
from lxml import etree

from .profiles import get_prompt_profile
from .schemas import agent_output_contract_text

PROMPTS_DIR = Path(__file__).resolve().parent
AUX_NORMAS_DIR = PROMPTS_DIR.parent / "auxiliar_normas"
AUX_UTILIDADES_DIR = PROMPTS_DIR.parent / "auxiliar_utilidades"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

PROMPT_FILES = {
    "metadados": PROMPTS_DIR / "metadados.md",
    "sinopse_abstract": PROMPTS_DIR / "sinopse_abstract.md",
    "gramatica_ortografia": PROMPTS_DIR / "gramatica_ortografia.md",
    "tipografia": PROMPTS_DIR / "tipografia.md",
    "tabelas_figuras": PROMPTS_DIR / "tabelas_figuras.md",
    "estrutura": PROMPTS_DIR / "estrutura.md",
    "referencias": PROMPTS_DIR / "referencias.md",
    "coordenador": PROMPTS_DIR / "coordenador.md",
}

AGENT_ORDER = [
    "metadados",
    "sinopse_abstract",
    "gramatica_ortografia",
    "tabelas_figuras",
    "referencias",
    "estrutura",
    "tipografia",
]

_PROFILE_BLOCK_RE = re.compile(r"(?ms)^\s*([A-Z][A-Z0-9_]*)\s*=\s*\"\"\"\s*(.*?)\s*\"\"\"")


def _extract_tag_block(raw_text: str, tag_name: str, anchor: str | None = None) -> str:
    if anchor:
        anchor_idx = raw_text.find(anchor)
        if anchor_idx != -1:
            raw_text = raw_text[anchor_idx:]
    pattern = re.compile(rf"(?s)<{tag_name}\b[^>]*>.*?</{tag_name}>")
    match = pattern.search(raw_text)
    return match.group(0).strip() if match else ""


def _load_reference_support_context() -> str:
    snippets: list[str] = []

    def first_existing(filename: str) -> Path | None:
        for base_dir in (AUX_NORMAS_DIR, AUX_UTILIDADES_DIR):
            candidate = base_dir / filename
            if candidate.exists():
                return candidate
        return None

    csl_path = first_existing("custom_2026_associacao-brasileira-de-normas-tecnicas-ipea.csl")
    if csl_path is not None:
        raw = csl_path.read_text(encoding="utf-8", errors="replace")
        for macro_name in ("author", "title", "publisher", "access", "container-title", "event"):
            block = _extract_tag_block(raw, "macro", anchor=f'<macro name="{macro_name}"')
            if block:
                snippets.append(f"[CSL:{macro_name}]\n{block}")

    cls_path = first_existing("td.cls")
    if cls_path is not None:
        raw = cls_path.read_text(encoding="utf-8", errors="replace")
        cslsetup_match = re.search(r"(?ms)\\cslsetup\s*\{.*?^\s*\}", raw)
        if cslsetup_match:
            snippets.append(f"[TD-CLS:cslsetup]\n{cslsetup_match.group(0).strip()}")

    preamble_path = first_existing("preamble.tex")
    if preamble_path is not None:
        raw = preamble_path.read_text(encoding="utf-8", errors="replace")
        hypersetup_match = re.search(r"(?ms)\\hypersetup\s*\{.*?hidelinks\}", raw)
        if hypersetup_match:
            snippets.append(f"[PREAMBLE:hypersetup]\n{hypersetup_match.group(0).strip()}")

    if not snippets:
        return ""

    return "Normas auxiliares locais do projeto para usar como referencia prioritaria:\n" + "\n\n".join(snippets)


@lru_cache(maxsize=1)
def _load_editorial_tasks() -> dict[str, str]:
    docx_path = AUX_UTILIDADES_DIR / "Agente IA Editorial (tarefas) (1).docx"
    if not docx_path.exists():
        return {}

    with ZipFile(docx_path, "r") as zf:
        root = etree.fromstring(zf.read("word/document.xml"))

    ns = {"w": W_NS}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
        if text:
            paragraphs.append(text)

    by_prefix: dict[str, str] = {}
    for text in paragraphs:
        if "]" in text:
            prefix = text.split("]", 1)[0] + "]"
            by_prefix[prefix] = text
    return by_prefix


def _build_tasks_context(agent_name: str) -> str:
    tasks = _load_editorial_tasks()
    prefix_map = {
        "estrutura": ["1.1]", "1.6]", "1.8]"],
        "sinopse_abstract": ["1.3]"],
        "gramatica_ortografia": ["1.4]", "1.5]"],
        "tabelas_figuras": ["1.6]"],
        "referencias": ["1.8]"],
        "tipografia": ["1.1]", "1.3]", "1.4]", "1.5]", "1.6]", "1.8]"],
    }
    prefixes = prefix_map.get(agent_name, [])
    snippets = [tasks[prefix] for prefix in prefixes if prefix in tasks]
    if not snippets:
        return ""
    return "Diretrizes operacionais do arquivo de tarefas editorial:\n" + "\n\n".join(snippets)


@lru_cache(maxsize=1)
def _load_typography_support_context() -> str:
    snippets = [
        "[TUTORIAL:tipografia]",
        "- titulo_publicacao: font=Times New Roman; size_pt=14; bold=true; align=center; space_before_pt=6; space_after_pt=54; line_spacing=1.0; left_indent_pt=0",
        "- texto: font=Times New Roman; size_pt=12; bold=false; italic=false; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.5; left_indent_pt=0",
        "- nota_rodape: font=Times New Roman; size_pt=10; bold=false; italic=false; align=justify; space_before_pt=0; space_after_pt=4; line_spacing=1.0; left_indent_pt=0",
        "- titulo_1: font=Times New Roman; size_pt=12; bold=true; align=justify; space_before_pt=18; space_after_pt=6; line_spacing=1.0; left_indent_pt=35.4",
        "- titulo_2: font=Times New Roman; size_pt=12; bold=true; align=justify; space_before_pt=18; space_after_pt=6; line_spacing=1.0; left_indent_pt=35.4",
        "- titulo_3: font=Times New Roman; size_pt=12; bold=true; align=justify; space_before_pt=18; space_after_pt=6; line_spacing=1.0; left_indent_pt=35.4",
        "- titulo_tabela_grafico: font=Times New Roman; size_pt=12; bold=false; align=justify; space_before_pt=18; space_after_pt=2; line_spacing=1.0; left_indent_pt=0",
        "- subtitulo_tabela_grafico: font=Times New Roman; size_pt=12; bold=true; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.0; left_indent_pt=0",
        "- texto_tabela: font=Times New Roman; size_pt=11; bold=false; align=left; space_before_pt=0; space_after_pt=0; line_spacing=1.0; left_indent_pt=0",
        "- fonte_tabela_grafico: font=Times New Roman; size_pt=10; bold=false; align=justify; space_before_pt=2; space_after_pt=12; line_spacing=1.0; left_indent_pt=0",
        "- texto_referencia: font=Times New Roman; size_pt=12; bold=false; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.5; left_indent_pt=0",
    ]

    cls_path = AUX_UTILIDADES_DIR / "td.cls"
    if cls_path.exists():
        raw = cls_path.read_text(encoding="utf-8", errors="replace")
        if "Times New Roman" in raw:
            snippets.extend(
                [
                    "",
                    "[TD-CLS:fontes]",
                    "O template TD define Times New Roman como fonte principal.",
                ]
            )
    return "\n".join(snippets)


def _build_agent_support_context(agent_name: str) -> str:
    parts: list[str] = []
    tasks_context = _build_tasks_context(agent_name)
    if tasks_context:
        parts.append(tasks_context)
    if agent_name == "referencias":
        parts.append(_load_reference_support_context())
    if agent_name == "tipografia":
        parts.append(_load_typography_support_context())
    return "\n\n".join(part for part in parts if part)


def _parse_instruction_profiles(raw_text: str) -> dict[str, str]:
    blocks = {name.upper(): body.strip() for name, body in _PROFILE_BLOCK_RE.findall(raw_text or "")}
    return blocks


def load_agent_instruction(agent_name: str, profile_key: str | None = None) -> str:
    path = PROMPT_FILES.get(agent_name)
    if path is None:
        raise ValueError(f"Agente de prompt desconhecido: {agent_name}")
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de prompt não encontrado: {path}")

    raw = path.read_text(encoding="utf-8").strip()
    blocks = _parse_instruction_profiles(raw)
    if not blocks:
        return raw

    key = (profile_key or "").upper()
    return blocks.get(key) or blocks.get("GENERIC") or next(iter(blocks.values()))


def _build_profile_context(profile_key: str | None) -> dict[str, str]:
    profile = get_prompt_profile(profile_key)
    return {
        "profile_description": profile.description,
        "profile_instruction": profile.instruction,
    }


def build_agent_prompt(agent_name: str, profile_key: str | None = None) -> ChatPromptTemplate:
    instruction = load_agent_instruction(agent_name, profile_key=profile_key)
    profile_ctx = _build_profile_context(profile_key)
    support_context = _build_agent_support_context(agent_name)

    return ChatPromptTemplate.from_messages(
        [
            ("system", instruction),
            (
                "human",
                (
                    "Perfil do documento: {profile_description}\n"
                    "Instrução de perfil: {profile_instruction}\n\n"
                    "Cada linha do trecho vem no formato [indice_global] (referência | tipo=...). "
                    "Se preencher paragraph_index, use exatamente o número entre colchetes [N] daquela linha; "
                    "nunca use a posição do item no lote.\n"
                    "Respeite o rótulo tipo=... para não analisar elementos fora do seu escopo.\n\n"
                    "Normas auxiliares locais:\n{support_context}\n\n"
                    "Pergunta do usuário: {question}\n\n"
                    "Trecho do documento:\n{document_excerpt}\n\n"
                    "Contrato de saída:\n{output_contract}\n"
                ),
            ),
        ]
    ).partial(
        **profile_ctx,
        support_context=support_context or "(nenhuma norma auxiliar carregada para este agente)",
        output_contract=agent_output_contract_text(),
    )


def build_coordinator_prompt(profile_key: str | None = None) -> ChatPromptTemplate:
    instruction = load_agent_instruction("coordenador", profile_key=profile_key)
    profile_ctx = _build_profile_context(profile_key)

    return ChatPromptTemplate.from_messages(
        [
            ("system", instruction),
            (
                "human",
                (
                    "Perfil do documento: {profile_description}\n"
                    "Instrução de perfil: {profile_instruction}\n\n"
                    "Pergunta do usuário: {question}\n\n"
                    "Trecho do documento:\n{document_excerpt}\n\n"
                    "Comentários dos agentes (JSON):\n{comments_json}\n\n"
                    "Responda em português, de forma direta, e cite os principais pontos."
                ),
            ),
        ]
    ).partial(**profile_ctx)
