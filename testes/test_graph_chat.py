from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import sys
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from editorial_docx.docx_utils import _build_review_note
from editorial_docx.docx_utils import apply_comments_to_docx, extract_paragraphs_with_metadata
from editorial_docx.graph_chat import _agent_scope_indexes, _normalize_batch_comments, _parse_comments
from editorial_docx.models import AgentComment
from editorial_docx.prompts.prompt import AGENT_ORDER, _build_agent_support_context, load_agent_instruction


def test_parse_comments_accepts_json_fenced_block():
    raw = """```json
    [
      {
        "category": "gramatica_ortografia",
        "message": "Erro de crase",
        "paragraph_index": 1,
        "issue_excerpt": "a nivel",
        "suggested_fix": "em nível"
      }
    ]
    ```"""

    comments = _parse_comments(raw, agent="gramatica_ortografia")

    assert len(comments) == 1
    assert comments[0].message == "Erro de crase"
    assert comments[0].paragraph_index == 1


def test_parse_comments_accepts_wrapped_comments_key():
    raw = """
    {
      "comments": [
        {
          "category": "gramatica_ortografia",
          "message": "Ajustar concordância",
          "paragraph_index": 2
        }
      ]
    }
    """

    comments = _parse_comments(raw, agent="gramatica_ortografia")

    assert len(comments) == 1
    assert comments[0].message == "Ajustar concordância"
    assert comments[0].paragraph_index == 2


def test_parse_comments_returns_empty_list_for_empty_payload():
    assert _parse_comments("", agent="gramatica_ortografia") == []


def test_parse_comments_accepts_tipografia_auto_apply_fields():
    raw = """
    [
      {
        "category": "Tipografia",
        "message": "Ajustar corpo do texto ao padrão.",
        "paragraph_index": 0,
        "issue_excerpt": "Texto normal.",
        "suggested_fix": "Aplicar padrão de corpo do texto.",
        "auto_apply": true,
        "format_spec": "font=Times New Roman; size_pt=12; bold=false; italic=false; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.5; left_indent_pt=0"
      }
    ]
    """

    comments = _parse_comments(raw, agent="tipografia")

    assert len(comments) == 1
    assert comments[0].auto_apply is True
    assert "font=Times New Roman" in comments[0].format_spec


def test_normalize_batch_comments_maps_local_index_to_global_index():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="gramatica_ortografia",
            message="Ajustar concordância",
            paragraph_index=1,
            issue_excerpt="texto com erro",
            suggested_fix="texto corrigido",
        )
    ]
    chunks = ["zero", "um", "dois", "tres", "quatro", "texto com erro", "depois"]
    refs = [
        "parágrafo 1 | tipo=paragraph",
        "parágrafo 2 | tipo=paragraph",
        "parágrafo 3 | tipo=paragraph",
        "parágrafo 4 | tipo=paragraph",
        "parágrafo 5 | tipo=paragraph",
        "parágrafo 6 | tipo=paragraph",
        "parágrafo 7 | tipo=paragraph",
    ]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[4, 5],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].paragraph_index == 5


def test_normalize_batch_comments_accepts_objective_grammar_comment_on_direct_quote():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Pontuação",
            message="Erro de pontuação no trecho citado.",
            paragraph_index=0,
            issue_excerpt="\"trecho citado\"",
            suggested_fix="\"trecho citado,\"",
        )
    ]
    chunks = ['"trecho citado"']
    refs = ["parágrafo 1 | tipo=direct_quote"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].paragraph_index == 0


def test_normalize_batch_comments_accepts_objective_grammar_comment_on_reference_entry():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Pontuação",
            message="Erro de pontuação na referência.",
            paragraph_index=0,
            issue_excerpt="SILVA, J. Título do artigo , 2020.",
            suggested_fix="SILVA, J. Título do artigo, 2020.",
        )
    ]
    chunks = ["SILVA, J. Título do artigo , 2020."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].paragraph_index == 0


def test_normalize_batch_comments_accepts_objective_grammar_comment_on_caption():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Pontuação",
            message="Erro de pontuação na legenda.",
            paragraph_index=0,
            issue_excerpt="Tabela 2 Decomposição da renda",
            suggested_fix="Tabela 2: Decomposição da renda",
        )
    ]
    chunks = ["Tabela 2 Decomposição da renda"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].paragraph_index == 0


def test_normalize_batch_comments_discards_identical_fix():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Acentuação",
            message="Sem mudança real",
            paragraph_index=0,
            issue_excerpt="benefícios coletivos",
            suggested_fix="benefícios coletivos",
        )
    ]
    chunks = ["benefícios coletivos"]
    refs = ["parágrafo 1 | tipo=paragraph | estilo=Corpo de Texto Especial"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_extract_paragraphs_with_metadata_includes_block_type_and_style(tmp_path):
    docx_path = tmp_path / "mini.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>1 INTRODUÇÃO</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Texto normal.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
</w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)

    items = extract_paragraphs_with_metadata(docx_path)

    assert items[0].block_type == "heading"
    assert "tipo=heading" in items[0].ref_label
    assert "estilo=heading 1" in items[0].ref_label
    assert items[1].block_type == "paragraph"


def test_extract_paragraphs_with_metadata_keeps_long_body_text_as_paragraph(tmp_path):
    docx_path = tmp_path / "mini_long_body.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>3. Classificação pelo tipo de beneficiário</w:t></w:r></w:p>
    <w:p><w:r><w:t>A primeira abordagem proposta busca identificar os tipos de beneficiários da seguridade social a partir da distinção entre os beneficiários individuais e os coletivos, com desenvolvimento analítico ao longo do parágrafo.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)

    items = extract_paragraphs_with_metadata(docx_path)

    assert items[0].block_type == "heading"
    assert items[1].block_type == "paragraph"


def test_reference_prompt_support_context_loads_local_norms():
    support_context = _build_agent_support_context("referencias")

    assert "custom_2026_associacao-brasileira-de-normas-tecnicas-ipea" in support_context
    assert "[CSL:author]" in support_context
    assert "[TD-CLS:cslsetup]" in support_context
    assert "[PREAMBLE:hypersetup]" in support_context


def test_typography_prompt_support_context_loads_local_norms():
    support_context = _build_agent_support_context("tipografia")

    assert "Times New Roman" in support_context
    assert "texto_referencia" in support_context
    assert "Títulos e subtítulos" in support_context


def test_structure_prompt_support_context_loads_editorial_tasks():
    support_context = _build_agent_support_context("estrutura")

    assert "Títulos e subtítulos" in support_context
    assert "1, 1.1, 1.1.1" in support_context


def test_build_review_note_summarizes_applied_suggestion():
    item = AgentComment(
        agent="gramatica_ortografia",
        category="Concordância",
        message="Ajustar trecho",
        review_status="resolvido",
        suggested_fix="texto corrigido",
        approved_text="texto corrigido",
    )

    assert _build_review_note(item) == "Sugestão aplicada no painel assistido."


def test_build_review_note_summarizes_author_change():
    item = AgentComment(
        agent="gramatica_ortografia",
        category="Concordância",
        message="Ajustar trecho",
        review_status="resolvido",
        suggested_fix="texto corrigido",
        approved_text="texto final do autor",
    )

    assert _build_review_note(item) == "Modificado pelo autor no painel assistido."


def test_build_review_note_marks_typography_as_auto_applied():
    item = AgentComment(
        agent="tipografia",
        category="Tipografia",
        message="Aplicar padrão",
        auto_apply=True,
        format_spec="font=Times New Roman; size_pt=12",
    )

    assert _build_review_note(item) == "Ajuste tipográfico aplicado automaticamente."


def test_normalize_batch_comments_discards_table_source_suggestion_inside_caption():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Fonte",
            message="Adicionar a fonte dos dados utilizados na Tabela 2, conforme o padrão editorial.",
            paragraph_index=0,
            issue_excerpt="Tabela 2: Decomposição do índice de Gini da renda total familiar per capita por fontes de renda – Brasil (2017-2018)",
            suggested_fix="Tabela 2: Decomposição do índice de Gini da renda total familiar per capita por fontes de renda – Brasil (2017-2018). Fonte: ...",
        )
    ]
    chunks = ["Tabela 2: Decomposição do índice de Gini da renda total familiar per capita por fontes de renda – Brasil (2017-2018)"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_accepts_safe_typography_auto_apply():
    comments = [
        AgentComment(
            agent="tipografia",
            category="Tipografia",
            message="Ajustar corpo do texto ao padrão.",
            paragraph_index=0,
            issue_excerpt="Texto normal.",
            suggested_fix="Aplicar padrão de corpo do texto.",
            auto_apply=True,
            format_spec="font=Times New Roman; size_pt=12; bold=false; italic=false; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.5; left_indent_pt=0",
        )
    ]
    chunks = ["Texto normal."]
    refs = ["parÃ¡grafo 1 | tipo=paragraph | estilo=Corpo Especial"]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tipografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].auto_apply is True


def test_normalize_batch_comments_accepts_safe_structure_auto_apply():
    comments = [
        AgentComment(
            agent="estrutura",
            category="Estrutura",
            message="Normalizar título para o padrão editorial.",
            paragraph_index=0,
            issue_excerpt="1 Introdução",
            suggested_fix="1. INTRODUÇÃO",
            auto_apply=True,
        )
    ]
    chunks = ["1 Introdução"]
    refs = ["parágrafo 1 | tipo=heading"]

    normalized = _normalize_batch_comments(
        comments,
        agent="estrutura",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].auto_apply is True


def test_normalize_batch_comments_rejects_unsafe_structure_auto_apply():
    comments = [
        AgentComment(
            agent="estrutura",
            category="Estrutura",
            message="Inserir numeração ausente.",
            paragraph_index=0,
            issue_excerpt="Introdução",
            suggested_fix="1. INTRODUÇÃO",
            auto_apply=True,
        )
    ]
    chunks = ["Introdução"]
    refs = ["parágrafo 1 | tipo=heading"]

    normalized = _normalize_batch_comments(
        comments,
        agent="estrutura",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_accepts_safe_reference_auto_apply():
    comments = [
        AgentComment(
            agent="referencias",
            category="Referências",
            message="Normalizar pontuação da referência.",
            paragraph_index=0,
            issue_excerpt="SILVA, J. Título do artigo , v. 2, n. 1, 2020.",
            suggested_fix="SILVA, J. Título do artigo, v. 2, n. 1, 2020.",
            auto_apply=True,
        )
    ]
    chunks = ["SILVA, J. Título do artigo , v. 2, n. 1, 2020."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].auto_apply is True


def test_normalize_batch_comments_rejects_unsafe_reference_auto_apply():
    comments = [
        AgentComment(
            agent="referencias",
            category="Referências",
            message="Completar DOI.",
            paragraph_index=0,
            issue_excerpt="SILVA, J. Título do artigo. 2020.",
            suggested_fix="SILVA, J. Título do artigo. 2020. DOI: 10.0000/xyz.",
            auto_apply=True,
        )
    ]
    chunks = ["SILVA, J. Título do artigo. 2020."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_metadados_outside_front_matter():
    comments = [
        AgentComment(
            agent="metadados",
            category="Dados editoriais",
            message="A cidade deve ser Brasília/DF.",
            paragraph_index=28,
            issue_excerpt="Cidade não fornecida.",
            suggested_fix="Preencher a cidade como Brasília/DF.",
        )
    ]
    chunks = ["x"] * 40
    refs = [f"parágrafo {i+1} | tipo=paragraph" for i in range(40)]

    normalized = _normalize_batch_comments(
        comments,
        agent="metadados",
        batch_indexes=[28],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_structure_numbering_claim_in_body_paragraph():
    comments = [
        AgentComment(
            agent="estrutura",
            category="numeração e hierarquia de seções",
            message="A seção 5 não está numerada, mas deveria ser numerada como 5.",
            paragraph_index=0,
            issue_excerpt="o tema será tratado na seção 5 desse texto",
            suggested_fix="Numerar a seção 5 como 5.",
        )
    ]
    chunks = ["o tema será tratado na seção 5 desse texto"]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="estrutura",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_accepts_safe_table_caption_auto_apply():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Tabela",
            message="Normalizar identificador da tabela.",
            paragraph_index=0,
            issue_excerpt="Tabela 2",
            suggested_fix="TABELA 2",
            auto_apply=True,
        )
    ]
    chunks = ["Tabela 2"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].auto_apply is True


def test_normalize_batch_comments_rejects_unsafe_table_caption_auto_apply():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Tabela",
            message="Incluir fonte ausente.",
            paragraph_index=0,
            issue_excerpt="TABELA 2",
            suggested_fix="TABELA 2\nFonte: IBGE.",
            auto_apply=True,
        )
    ]
    chunks = ["TABELA 2"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_table_source_claim_when_neighbor_source_exists():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Tabela/Figura",
            message="Identificador do gráfico está correto, mas a fonte não está presente.",
            paragraph_index=0,
            issue_excerpt="Gráfico 6: Efeito simulado",
            suggested_fix="Incluir a fonte dos dados abaixo do gráfico.",
        )
    ]
    chunks = ["Gráfico 6: Efeito simulado", "Fonte: Portal Beneficiômetro da Seguridade Social"]
    refs = ["parágrafo 1 | tipo=caption", "parágrafo 2 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0, 1],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_table_level_claim_from_table_cell():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Tabela",
            message="Falta subtítulo descritivo para a tabela.",
            paragraph_index=0,
            issue_excerpt="6,7",
            suggested_fix="Adicionar um subtítulo descritivo após o identificador.",
        )
    ]
    chunks = ["6,7"]
    refs = ["parágrafo 1 | tipo=table_cell"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_source_claim_when_not_anchored_in_caption():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Tabela",
            message="Falta fonte para a tabela.",
            paragraph_index=0,
            issue_excerpt="Texto analítico sobre a tabela",
            suggested_fix="Adicionar uma linha com a fonte dos dados abaixo da tabela.",
        )
    ]
    chunks = ["Texto analítico sobre a tabela"]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_wrong_style_for_caption():
    comments = [
        AgentComment(
            agent="conformidade_estilos",
            category="style",
            message="Uso de estilo inadequado para a função editorial.",
            paragraph_index=0,
            issue_excerpt="Gráfico 6: Efeito simulado",
            suggested_fix="TEXTO_REFERENCIA",
        )
    ]
    chunks = ["Gráfico 6: Efeito simulado"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="conformidade_estilos",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_title_style_for_internal_phrase_in_paragraph():
    comments = [
        AgentComment(
            agent="conformidade_estilos",
            category="Style Issue",
            message="O título 'Beneficiários individuais' deve ser formatado como TÍTULO_2.",
            paragraph_index=0,
            issue_excerpt="Beneficiários individuais",
            suggested_fix="TÍTULO_2",
        )
    ]
    chunks = [
        "A primeira abordagem proposta busca identificar os tipos de beneficiários da seguridade social a partir da distinção entre os beneficiários individuais e os coletivos."
    ]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="conformidade_estilos",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_grammar_style_rewrite():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Clareza",
            message="A frase pode ser simplificada.",
            paragraph_index=0,
            issue_excerpt="a previdência social deve ser analisada a partir da perspectiva de seus resultados sociais",
            suggested_fix="a previdência social deve ser analisada sob a perspectiva de seus resultados sociais",
        )
    ]
    chunks = ["a previdência social deve ser analisada a partir da perspectiva de seus resultados sociais"]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_apply_comments_to_docx_auto_applies_typography_formatting(tmp_path):
    docx_path = tmp_path / "mini_tipografia.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>Texto normal.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="tipografia",
                category="Tipografia",
                message="Ajustar corpo do texto ao padrão.",
                paragraph_index=0,
                issue_excerpt="Texto normal.",
                suggested_fix="Aplicar padrão de corpo do texto.",
                auto_apply=True,
                format_spec="font=Times New Roman; size_pt=12; bold=false; italic=false; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.5; left_indent_pt=0",
            )
        ],
    )

    out_path = tmp_path / "resultado.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")

    assert "Times New Roman" in document_xml
    assert 'w:jc w:val="both"' in document_xml


def test_apply_comments_to_docx_auto_applies_safe_structure_heading_normalization(tmp_path):
    docx_path = tmp_path / "mini_estrutura.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>1 Introdução</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="estrutura",
                category="Estrutura",
                message="Normalizar título para o padrão editorial.",
                paragraph_index=0,
                issue_excerpt="1 Introdução",
                suggested_fix="1. INTRODUÇÃO",
                auto_apply=True,
            )
        ],
    )

    out_path = tmp_path / "resultado_estrutura.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")

    assert "1. INTRODUÇÃO" in document_xml


def test_apply_comments_to_docx_auto_applies_safe_reference_normalization(tmp_path):
    docx_path = tmp_path / "mini_referencia.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>SILVA, J. Título do artigo , v. 2, n. 1, 2020.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="referencias",
                category="Referências",
                message="Normalizar pontuação da referência.",
                paragraph_index=0,
                issue_excerpt="SILVA, J. Título do artigo , v. 2, n. 1, 2020.",
                suggested_fix="SILVA, J. Título do artigo, v. 2, n. 1, 2020.",
                auto_apply=True,
            )
        ],
    )

    out_path = tmp_path / "resultado_referencia.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")

    assert "Título do artigo, v. 2" in document_xml


def test_apply_comments_to_docx_consolidates_multiple_comments_on_same_paragraph(tmp_path):
    docx_path = tmp_path / "mini_comentarios.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>Parágrafo com dois ajustes.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="estrutura",
                category="Estrutura",
                message="Normalizar título.",
                paragraph_index=0,
                issue_excerpt="Parágrafo com dois ajustes.",
                suggested_fix="Parágrafo com dois ajustes.",
            ),
            AgentComment(
                agent="gramatica_ortografia",
                category="Pontuação",
                message="Revisar pontuação final.",
                paragraph_index=0,
                issue_excerpt="Parágrafo com dois ajustes.",
                suggested_fix="Parágrafo com dois ajustes!",
            ),
        ],
    )

    out_path = tmp_path / "resultado_comentarios.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        comments_xml = zf.read("word/comments.xml")

    root = etree.fromstring(comments_xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    comments = root.findall(".//w:comment", ns)
    text = "".join(node.text or "" for node in comments[0].findall(".//w:t", ns))

    assert len(comments) == 1
    assert "Achados consolidados neste trecho:" in text
    assert "1. [estrutura/Estrutura]" in text
    assert "2. [gramatica_ortografia/Pontuação]" in text
    assert "Trecho:" not in text


def test_agent_order_excludes_conformidade_estilos():
    assert "conformidade_estilos" not in AGENT_ORDER


def test_normalize_batch_comments_discards_structure_section_claim_for_illustration_caption():
    comments = [
        AgentComment(
            agent="estrutura",
            category="numeraÃ§Ã£o e hierarquia de seÃ§Ãµes",
            message="GrÃ¡fico 6 deve ser numerado como seÃ§Ã£o 6.",
            paragraph_index=0,
            issue_excerpt="GrÃ¡fico 6: Efeito simulado",
            suggested_fix="Numerar a seÃ§Ã£o como 6.",
        )
    ]
    chunks = ["GrÃ¡fico 6: Efeito simulado"]
    refs = ["parÃ¡grafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="estrutura",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_typography_for_generic_body_paragraph():
    comments = [
        AgentComment(
            agent="tipografia",
            category="Tipografia",
            message="Ajustar espaÃ§amento do corpo do texto.",
            paragraph_index=0,
            issue_excerpt="Texto normal.",
            suggested_fix="Aplicar padrÃ£o de corpo do texto.",
            auto_apply=True,
            format_spec="space_after_pt=6; line_spacing=1.5",
        )
    ]
    chunks = ["Texto normal."]
    refs = ["parÃ¡grafo 1 | tipo=paragraph | estilo=Normal"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tipografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_agent_scope_indexes_limits_tipografia_to_structured_blocks():
    chunks = ["1 INTRODUÃ‡ÃƒO", "Texto A", "GrÃ¡fico 1: Resultado", "Fonte: Base", "SILVA, J. TÃ­tulo. 2020."]
    refs = [
        "parÃ¡grafo 1 | tipo=heading | estilo=Heading 1",
        "parÃ¡grafo 2 | tipo=paragraph | estilo=Normal",
        "parÃ¡grafo 3 | tipo=caption | estilo=Legenda",
        "parÃ¡grafo 4 | tipo=paragraph | estilo=Normal",
        "parÃ¡grafo 5 | tipo=reference_entry | estilo=ReferÃªncia",
    ]

    picked = _agent_scope_indexes("tipografia", chunks, refs, sections=[])

    assert picked == [0, 2, 4]


def test_normalize_batch_comments_discards_typography_capitalization_instruction():
    comments = [
        AgentComment(
            agent="tipografia",
            category="heading",
            message="Título deve estar em negrito e em caixa alta.",
            paragraph_index=0,
            issue_excerpt="Introdução",
            suggested_fix="Aplicar negrito e caixa alta.",
            auto_apply=True,
            format_spec="font=Times New Roman; size_pt=12; bold=true",
        )
    ]
    chunks = ["Introdução"]
    refs = ["parágrafo 1 | tipo=heading | estilo=Heading 1"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tipografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_references_missing_field_guess():
    comments = [
        AgentComment(
            agent="referencias",
            category="Inconsistência de paginação",
            message="A referência não apresenta a paginação do artigo.",
            paragraph_index=0,
            issue_excerpt="SILVA, J. Título do artigo. Revista X, 2020.",
            suggested_fix="Adicionar a paginação do artigo.",
        )
    ]
    chunks = ["SILVA, J. Título do artigo. Revista X, 2020."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_references_title_case_claim_on_all_caps_entry():
    comments = [
        AgentComment(
            agent="referencias",
            category="Inconsistência de título",
            message="A entrada apresenta o título em caixa alta, o que não está em conformidade.",
            paragraph_index=0,
            issue_excerpt="MATTOS, Bruna et al. EDUCAÇÃO EM SAÚDE: COMO ANDA ESSA PRÁTICA?",
            suggested_fix="Alterar para caixa baixa.",
        )
    ]
    chunks = ["MATTOS, Bruna et al. EDUCAÇÃO EM SAÚDE: COMO ANDA ESSA PRÁTICA?"]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_agent_scope_indexes_limits_estrutura_to_headings():
    chunks = ["Resumo em texto corrido.", "Classificação dos benefícios coletivos", "O texto menciona a seção 2."]
    refs = [
        "parágrafo 1 | tipo=paragraph",
        "parágrafo 2 | tipo=heading | estilo=Heading 1",
        "parágrafo 3 | tipo=paragraph",
    ]

    picked = _agent_scope_indexes("estrutura", chunks, refs, sections=[])

    assert picked == [1]


def test_sinopse_abstract_td_prompt_requires_jel_after_pt_and_en_blocks():
    instruction = load_agent_instruction("sinopse_abstract", profile_key="TD")

    assert "após Palavras-chave" in instruction
    assert "após Keywords/Abstract" in instruction
    assert "ausência em uma das versões" in instruction
    assert "texto justificado" in instruction


def test_normalize_batch_comments_discards_structure_title_mention_inside_paragraph():
    comments = [
        AgentComment(
            agent="estrutura",
            category="Numeração de seções",
            message="A seção 'Classificação dos benefícios coletivos' deve ser numerada como 2.",
            paragraph_index=0,
            issue_excerpt="Classificação dos benefícios coletivos",
            suggested_fix="Numerar a seção como 2 Classificação dos benefícios coletivos.",
        )
    ]
    chunks = [
        "Este trabalho apresenta uma proposta de classificação. A seção Classificação dos benefícios coletivos será retomada mais adiante."
    ]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="estrutura",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_apply_comments_to_docx_anchors_comment_to_issue_excerpt_and_highlights_it(tmp_path):
    docx_path = tmp_path / "mini_anchor.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>Os atendimentos em grupos se justificam ainda pelas evidências.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="gramatica_ortografia",
                category="ConcordÃ¢ncia",
                message="Ajustar concordÃ¢ncia.",
                paragraph_index=0,
                issue_excerpt="pelas evidências",
                suggested_fix="pelas evidências corretas",
            )
        ],
    )

    out_path = tmp_path / "resultado_anchor.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        document_xml = zf.read("word/document.xml")

    root = etree.fromstring(document_xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    highlights = root.findall(".//w:highlight", ns)
    texts = [node.text or "" for node in root.findall(".//w:p//w:t", ns)]

    assert any(node.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") == "yellow" for node in highlights)
    assert "pelas evidências" in "".join(texts)
    assert len(texts) >= 2


def test_apply_comments_to_docx_splits_comments_when_excerpts_are_far_apart(tmp_path):
    docx_path = tmp_path / "mini_split_comments.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>O primeiro problema está aqui e o segundo problema está mais adiante.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="gramatica_ortografia",
                category="Erro 1",
                message="Ajustar o primeiro trecho.",
                paragraph_index=0,
                issue_excerpt="primeiro problema",
                suggested_fix="primeiro ajuste",
            ),
            AgentComment(
                agent="gramatica_ortografia",
                category="Erro 2",
                message="Ajustar o segundo trecho.",
                paragraph_index=0,
                issue_excerpt="segundo problema",
                suggested_fix="segundo ajuste",
            ),
        ],
    )

    out_path = tmp_path / "resultado_split.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        comments_xml = zf.read("word/comments.xml")

    root = etree.fromstring(comments_xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    comments = root.findall(".//w:comment", ns)

    assert len(comments) == 2


def test_apply_comments_to_docx_applies_auto_fix_silently_without_comment(tmp_path):
    docx_path = tmp_path / "mini_silent_auto.docx"
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>Texto normal.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", root_rels.replace("officeDocument", "comments"))
        zf.writestr("word/styles.xml", styles)

    output_bytes = apply_comments_to_docx(
        docx_path,
        [
            AgentComment(
                agent="tipografia",
                category="Tipografia",
                message="Ajustar corpo do texto ao padrão.",
                paragraph_index=0,
                issue_excerpt="Texto normal.",
                suggested_fix="Aplicar padrão de corpo do texto.",
                auto_apply=True,
                format_spec="font=Times New Roman; size_pt=12; bold=false; italic=false; align=justify; space_before_pt=0; space_after_pt=6; line_spacing=1.5; left_indent_pt=0",
            )
        ],
    )

    out_path = tmp_path / "resultado_silent_auto.docx"
    out_path.write_bytes(output_bytes)

    with ZipFile(out_path, "r") as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
        comments_xml = zf.read("word/comments.xml").decode("utf-8")

    assert "Times New Roman" in document_xml
    assert "<w:comment " not in comments_xml
