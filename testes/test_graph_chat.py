from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import sys
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import editorial_docx.graph_chat as graph_chat_module
from editorial_docx.docx_utils import _build_comment_lines_for_item, _build_review_note
from editorial_docx.docx_utils import apply_comments_to_docx, extract_paragraphs_with_metadata
from editorial_docx.graph_chat import (
    _agent_scope_indexes,
    _connection_error_summary,
    _heuristic_reference_comments,
    _invoke_with_retry,
    _is_connection_error,
    _normalize_batch_comments,
    _parse_comment_reviews,
    _parse_comments,
    _review_comments_with_llm,
    _verify_batch_comments,
    run_conversation,
)
from editorial_docx.models import AgentComment, agent_short_label
from editorial_docx.prompts.prompt import AGENT_ORDER, _build_agent_support_context, load_agent_instruction
from editorial_docx.prompts.schemas import agent_output_contract_text


SAMPLE_DOCX = Path(__file__).resolve().parent / "234362_TD_3125_Benefícios coletivos (53 laudas).docx"


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


def test_parse_comments_ignores_tipografia_auto_apply_fields():
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
    assert comments[0].auto_apply is False
    assert "font=Times New Roman" in comments[0].format_spec


def test_parse_comment_reviews_accepts_valid_json():
    raw = """
    [
      {
        "paragraph_index": 8,
        "issue_excerpt": "e sugerem",
        "suggested_fix": "e sugere",
        "decision": "approve",
        "reason": "Erro objetivo de concordância."
      }
    ]
    """

    items, status = _parse_comment_reviews(raw)

    assert status == "json direto"
    assert len(items) == 1
    assert items[0]["decision"] == "approve"


def test_review_comments_with_llm_filters_rejected_comments(monkeypatch):
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Concordância",
            message="A concordância verbal está incorreta neste fragmento.",
            paragraph_index=8,
            issue_excerpt="e sugerem",
            suggested_fix="e sugere",
        ),
        AgentComment(
            agent="gramatica_ortografia",
            category="Estilo",
            message="Há repetição imediata do mesmo termo.",
            paragraph_index=21,
            issue_excerpt="tem o objetivo de",
            suggested_fix="busca",
        ),
    ]

    class FakeResponse:
        content = """
        [
          {
            "paragraph_index": 8,
            "issue_excerpt": "e sugerem",
            "suggested_fix": "e sugere",
            "decision": "approve",
            "reason": "Erro objetivo."
          },
          {
            "paragraph_index": 21,
            "issue_excerpt": "tem o objetivo de",
            "suggested_fix": "busca",
            "decision": "reject",
            "reason": "Reescrita estilística."
          }
        ]
        """

    monkeypatch.setattr(graph_chat_module, "_invoke_with_model_fallback", lambda prompt, payload, operation: FakeResponse())

    approved, status = _review_comments_with_llm(
        comments,
        agent="gramatica_ortografia",
        question="Revise",
        excerpt="[8] (...) e sugerem\n[21] (...) tem o objetivo de",
        profile_key="TD",
    )

    assert len(approved) == 1
    assert approved[0].issue_excerpt == "e sugerem"
    assert "revisor: 1 aprovados, 1 rejeitados" in status


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


def test_normalize_batch_comments_discards_grammar_comment_on_direct_quote():
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

    assert normalized == []


def test_normalize_batch_comments_discards_grammar_comment_on_reference_entry():
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

    assert normalized == []


def test_normalize_batch_comments_adds_typography_comment_for_italic_heading():
    chunks = ["Beneficiários coletivos não identificáveis"]
    refs = ["parágrafo 1 | tipo=heading | estilo=heading 3 | italico=sim"]

    normalized = _normalize_batch_comments(
        comments=[],
        agent="tipografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert len(normalized) == 1
    assert normalized[0].agent == "tipografia"
    assert normalized[0].paragraph_index == 0
    assert normalized[0].format_spec == "italic=false"
    assert "itálico" in normalized[0].message


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


def test_normalize_batch_comments_discards_grammar_comment_inside_quoted_excerpt():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Ortografia",
            message="Corrigir o trecho citado.",
            paragraph_index=0,
            issue_excerpt="“de natureza indivisível”",
            suggested_fix="“de natureza indivisivel”",
        )
    ]
    chunks = [
        "A lei define os direitos difusos como aqueles “de natureza indivisível, de que são titulares pessoas indeterminadas”."
    ]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_long_grammar_comment_in_paragraph_with_quote():
    full_sentence = (
        "A distinÃ§Ã£o estÃ¡ afirmada na Lei nÂº 8078, de 1990, que define, em seu artigo 81, "
        "os direitos difusos como aqueles \u201cde natureza indivisÃ­vel, de que sÃ£o titulares "
        "pessoas indeterminadas e ligadas por circunstÃ¢ncias de fato\u201d."
    )
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Ortografia",
            message="Corrigir o perÃ­odo.",
            paragraph_index=0,
            issue_excerpt=full_sentence,
            suggested_fix=full_sentence.replace("os direitos difusos", "os direitos coletivos"),
        )
    ]
    chunks = [full_sentence]
    refs = ["parÃ¡grafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_grammar_comment_that_only_removes_terminal_period():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Pontuação",
            message="Remover pontuação final.",
            paragraph_index=0,
            issue_excerpt="territórios centrais com presença ostensiva do tráfico.",
            suggested_fix="territórios centrais com presença ostensiva do tráfico",
        )
    ]
    chunks = ["territórios centrais com presença ostensiva do tráfico."]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_agent_order_excludes_metadata_and_structure_from_default_run():
    assert "metadados" not in AGENT_ORDER
    assert "estrutura" not in AGENT_ORDER


def test_agent_short_labels_are_compact_and_self_explanatory():
    assert agent_short_label("sinopse_abstract") == "sin"
    assert agent_short_label("gramatica_ortografia") == "gram"
    assert agent_short_label("tabelas_figuras") == "tab"
    assert agent_short_label("referencias") == "ref"


def test_normalize_batch_comments_discards_grammar_comment_that_only_swaps_demonstrative():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Concordância",
            message="Ajustar concordância.",
            paragraph_index=0,
            issue_excerpt="esse trabalho",
            suggested_fix="este trabalho",
        )
    ]
    chunks = ["esse trabalho tem o objetivo de amadurecer o debate."]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_grammar_comment_that_only_inserts_coordination_comma():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Pontuação",
            message="Ajustar pontuação.",
            paragraph_index=0,
            issue_excerpt="abordagens coletivas e territoriais",
            suggested_fix="abordagens coletivas, e territoriais",
        )
    ]
    chunks = ["as diferentes estratégias e abordagens coletivas e territoriais"]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_adds_heuristic_for_plural_agreement():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=["Os impactos recaem sobre os benefícios monetário do programa."],
        refs=["parágrafo 1 | tipo=paragraph"],
    )

    assert len(normalized) == 1
    assert normalized[0].issue_excerpt == "benefícios monetário"
    assert normalized[0].suggested_fix == "benefícios monetários"


def test_normalize_batch_comments_adds_heuristic_for_compound_subject_agreement():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=["Ainda hoje é a trajetória profissional e a comprovação individual que assenta o acesso ao direito previdenciário."],
        refs=["parágrafo 1 | tipo=paragraph"],
    )

    assert len(normalized) == 1
    assert normalized[0].issue_excerpt == "que assenta o acesso"
    assert normalized[0].suggested_fix == "que assentam o acesso"


def test_normalize_batch_comments_adds_heuristic_for_exercicio_sugere():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=[
            "O exercício realizado sustenta a possibilidade de pensar em dimensões distintas de categorização dos benefícios coletivos e sugerem possibilidades promissoras de desenvolvimento de indicadores."
        ],
        refs=["parágrafo 1 | tipo=paragraph"],
    )

    assert any(item.issue_excerpt == "e sugerem" and item.suggested_fix == "e sugere" for item in normalized)


def test_normalize_batch_comments_discards_plural_copula_for_singular_head():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Concordância",
            message="Ajustar a concordância.",
            paragraph_index=0,
            issue_excerpt="o conhecimento sobre os impactos coletivos das ofertas ainda é pouco sistematizado.",
            suggested_fix="o conhecimento sobre os impactos coletivos das ofertas ainda são pouco sistematizados.",
        )
    ]
    chunks = [comments[0].issue_excerpt]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_possessive_article_style_swap():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Concordância",
            message="Ajustar a concordância.",
            paragraph_index=0,
            issue_excerpt="as suas ofertas e os seus resultados sociais,",
            suggested_fix="as suas ofertas e seus resultados sociais,",
        )
    ]
    chunks = [comments[0].issue_excerpt]
    refs = ["parágrafo 1 | tipo=paragraph"]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_table_comment_when_caption_already_has_identifier():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Identificação",
            message="Falta identificador para a tabela.",
            paragraph_index=0,
            issue_excerpt="Tabela 2: Decomposição do índice de Gini",
            suggested_fix="Adicionar identificador 'Tabela 2' antes do título.",
        )
    ]
    chunks = ["Tabela 2: Decomposição do índice de Gini"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert all(item.message != "Falta identificador para a tabela." for item in normalized)
    assert any(item.message == "Na legenda, o identificador deve ficar na primeira linha e o título descritivo na linha abaixo." for item in normalized)


def test_normalize_batch_comments_discards_table_comment_with_empty_issue_excerpt():
    comments = [
        AgentComment(
            agent="tabelas_figuras",
            category="Título",
            message="Falta título para a tabela.",
            paragraph_index=0,
            issue_excerpt="",
            suggested_fix="Adicionar título descritivo da tabela.",
        )
    ]
    chunks = ["Tabela 3"]
    refs = ["parágrafo 1 | tipo=caption"]

    normalized = _normalize_batch_comments(
        comments,
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_synopsis_comment_about_uppercase_on_fragment():
    comments = [
        AgentComment(
            agent="sinopse_abstract",
            category="Textual Issue",
            message="A sinopse apresenta uma frase que não inicia com letra maiúscula.",
            paragraph_index=0,
            issue_excerpt="sugerem possibilidades promissoras de desenvolvimento",
            suggested_fix="Iniciar a frase com letra maiúscula.",
        )
    ]
    chunks = ["Os resultados sugerem possibilidades promissoras de desenvolvimento de indicadores."]
    refs = ["parágrafo 1 | tipo=abstract_body | align=justify"]

    normalized = _normalize_batch_comments(
        comments,
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_synopsis_word_limit_false_positive():
    comments = [
        AgentComment(
            agent="sinopse_abstract",
            category="Extensão",
            message="A sinopse excede o limite de 250 palavras.",
            paragraph_index=0,
            issue_excerpt="Texto curto com bem menos palavras do que o limite editorial.",
            suggested_fix="Reduzir a sinopse para no máximo 250 palavras.",
        )
    ]
    chunks = ["Texto curto com bem menos palavras do que o limite editorial."]
    refs = ["parágrafo 1 | tipo=abstract_body | align=justify"]

    normalized = _normalize_batch_comments(
        comments,
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_verify_batch_comments_reports_reason_for_synopsis_word_limit_false_positive():
    comments = [
        AgentComment(
            agent="sinopse_abstract",
            category="Extensão",
            message="A sinopse excede o limite de 250 palavras.",
            paragraph_index=0,
            issue_excerpt="Texto curto com bem menos palavras do que o limite editorial.",
            suggested_fix="Reduzir a sinopse para no máximo 250 palavras.",
        )
    ]

    accepted, decisions = _verify_batch_comments(
        comments,
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=["Texto curto com bem menos palavras do que o limite editorial."],
        refs=["parágrafo 1 | tipo=abstract_body | align=justify"],
    )

    assert accepted == []
    assert len(decisions) == 1
    assert decisions[0].accepted is False
    assert decisions[0].reason == "alegação de limite de palavras não confirmada"


def test_normalize_batch_comments_discards_keywords_repetition_false_positive():
    comments = [
        AgentComment(
            agent="sinopse_abstract",
            category="Palavras-chave",
            message="As palavras-chave repetem o termo 'seguridade social'.",
            paragraph_index=0,
            issue_excerpt="seguridade social; benefícios coletivos; indicadores sociais; saúde; assistência social; previdência social; proteção social.",
            suggested_fix="Remover a repetição de 'seguridade social'.",
        )
    ]
    chunks = [comments[0].issue_excerpt]
    refs = ["parágrafo 1 | tipo=keywords_content"]

    normalized = _normalize_batch_comments(
        comments,
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_verify_batch_comments_rejects_duplicate_against_existing_comments():
    existing = AgentComment(
        agent="gramatica_ortografia",
        category="Concordância",
        message="A concordância está incorreta neste fragmento.",
        paragraph_index=0,
        issue_excerpt="benefícios monetário",
        suggested_fix="benefícios monetários",
    )
    duplicate = AgentComment(
        agent="gramatica_ortografia",
        category="Concordância",
        message="A concordância está incorreta neste fragmento.",
        paragraph_index=0,
        issue_excerpt="benefícios monetário",
        suggested_fix="benefícios monetários",
    )

    accepted, decisions = _verify_batch_comments(
        [duplicate],
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=["Os benefícios monetário do programa são relevantes."],
        refs=["parágrafo 1 | tipo=paragraph"],
        existing_comments=[existing],
        batch_index=2,
    )

    assert accepted == []
    assert len(decisions) >= 1
    assert all(decision.accepted is False for decision in decisions)
    assert any(decision.reason == "comentário duplicado" and decision.source == "llm" for decision in decisions)
    assert all(decision.batch_index == 2 for decision in decisions)


def test_normalize_batch_comments_adds_synopsis_word_limit_heuristic_when_text_exceeds_limit():
    long_synopsis = " ".join(f"palavra{i}" for i in range(251))

    normalized = _normalize_batch_comments(
        comments=[],
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=[long_synopsis],
        refs=["parágrafo 1 | tipo=abstract_body | align=justify"],
    )

    assert any(item.category == "Extensão" and "250 palavras" in item.message for item in normalized)


def test_verify_batch_comments_marks_heuristic_as_accepted_source():
    accepted, decisions = _verify_batch_comments(
        comments=[],
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=[" ".join(f"palavra{i}" for i in range(251))],
        refs=["parágrafo 1 | tipo=abstract_body | align=justify"],
    )

    assert len(accepted) == 1
    assert decisions[0].accepted is True
    assert decisions[0].source == "heuristic"


def test_normalize_batch_comments_discards_grammar_redundancy_rewrite_comment():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Estilo",
            message="Há repetição imediata do mesmo termo, gerando construção incorreta por duplicação local.",
            paragraph_index=0,
            issue_excerpt="Com o objetivo de contribuir com essa discussão, esse trabalho tem o objetivo de",
            suggested_fix="Com o objetivo de contribuir com essa discussão, este trabalho busca",
        )
    ]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=["Com o objetivo de contribuir com essa discussão, esse trabalho tem o objetivo de classificar os benefícios coletivos."],
        refs=["parágrafo 1 | tipo=paragraph"],
    )

    assert normalized == []


def test_verify_batch_comments_reports_reason_for_grammar_regency_rewrite_comment():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Regência",
            message="O verbo é transitivo direto e não exige a preposição.",
            paragraph_index=0,
            issue_excerpt="implica no reconhecimento",
            suggested_fix="implica o reconhecimento",
        )
    ]

    accepted, decisions = _verify_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=["A medida implica no reconhecimento de direitos sociais."],
        refs=["parágrafo 1 | tipo=paragraph"],
    )

    assert accepted == []
    assert len(decisions) == 1
    assert decisions[0].reason == "comentário gramatical de reescrita ou regência discutível"


def test_normalize_batch_comments_discards_grammar_diacritic_removal_on_single_word():
    comments = [
        AgentComment(
            agent="gramatica_ortografia",
            category="Ortografia",
            message="Há acento indevido na palavra.",
            paragraph_index=0,
            issue_excerpt="Beneficiômetro",
            suggested_fix="Beneficiometro",
        )
    ]

    normalized = _normalize_batch_comments(
        comments,
        agent="gramatica_ortografia",
        batch_indexes=[0],
        chunks=["Beneficiômetro"],
        refs=["parágrafo 1 | tipo=paragraph"],
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_comment_that_rewrites_whole_entry():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="O título da referência não está em conformidade.",
            paragraph_index=0,
            issue_excerpt="Delgado, G., & Cardoso Jr, J. C.. (2000). Principais resultados da pesquisa domiciliar sobre a previdência rural na região sul do Brasil (Projeto Avaliação Socioeconômica da Previdência Social Rural). Rio de Janeiro: IPEA, 2023. (Texto para Discussão n. 734.",
            suggested_fix="DELGADO, G.; CARDOSO JR, J. C.. Principais resultados da pesquisa domiciliar sobre a previdência rural na região sul do Brasil (Projeto Avaliação Socioeconômica da Previdência Social Rural). Rio de Janeiro: IPEA, 2023. (Texto para Discussão n. 734).",
        )
    ]
    chunks = [comments[0].issue_excerpt]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert all("DELGADO, G.; CARDOSO JR" not in item.suggested_fix for item in normalized)
    assert any(item.issue_excerpt.endswith("2023") and item.suggested_fix.endswith("2000") for item in normalized)


def test_normalize_batch_comments_discards_reference_comment_that_only_asks_for_missing_information():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="Falta de informações sobre o periódico na referência.",
            paragraph_index=0,
            issue_excerpt="Rego, W. L. (2008). Aspectos teóricos das políticas de cidadania: uma aproximação ao Bolsa Família. Lua Nova: Revista de Cultura e Política, (73), 147-185.",
            suggested_fix="Adicionar informações sobre o periódico, como volume e número, se disponíveis.",
        )
    ]
    chunks = [comments[0].issue_excerpt]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_incomplete_format_without_local_evidence():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="Referência com formato híbrido e incompleto: após o título há apenas a série e o ano, sem local/editora no padrão esperado.",
            paragraph_index=0,
            issue_excerpt="IPEA, Texto para Discussão (TD) 2941, 2023.",
            suggested_fix="Reestruturar o trecho final para incluir local, editora e tratar a série de forma consistente, usando os dados disponíveis na fonte.",
        )
    ]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=["SILVA, João. Título do trabalho. IPEA, Texto para Discussão (TD) 2941, 2023."],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert normalized == []


def test_verify_batch_comments_reports_reason_for_reference_incomplete_format_speculation():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="Referência de artigo sem o local do periódico, elemento previsto na estrutura indicada.",
            paragraph_index=0,
            issue_excerpt="Saúde e Sociedade, v. 29, n. 3, p. e190151, 2020.",
            suggested_fix="Inserir o local do periódico após o título do periódico, se essa informação constar na fonte consultada.",
        )
    ]

    accepted, decisions = _verify_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=["BARROS, P. B. de Azevedo. Saúde e Sociedade, v. 29, n. 3, p. e190151, 2020."],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert accepted == []
    assert len(decisions) == 1
    assert decisions[0].reason == "completude bibliográfica sem evidência local"


def test_normalize_batch_comments_discards_reference_title_comment_when_issue_is_page_range():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="A formatação do título do livro está inconsistente.",
            paragraph_index=0,
            issue_excerpt="Democracia hoje: novos desafios para a teoria democrática contemporânea. Brasília: Ed. da UnB, 2021. pp.246-82.",
            suggested_fix="Democracia hoje: novos desafios para a teoria democrática contemporânea. Brasília: Ed. da UnB, 2021. pp. 246-282.",
        )
    ]
    chunks = [comments[0].issue_excerpt]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_adds_reference_heuristic_for_glued_entries():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0],
        chunks=[
            "DESLANDES, Suely. Humanização dos cuidados em saúde. Rio de Janeiro: Fiocruz, 2006.DURKHEIM, E. Da divisão do trabalho social. São Paulo: Martins Fontes, 1999."
        ],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert any(item.issue_excerpt == "2006.D" and item.suggested_fix == "2006. D" for item in normalized)


def test_normalize_batch_comments_adds_reference_heuristic_for_page_spacing():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0],
        chunks=[
            "SOUZA, Marcelo L. O território: sobre espaço, poder, autonomia e desenvolvimento. In: CASTRO, Iná E., et. al (orgs.), Geografia: conceitos e temas. 2. ed. Rio de Janeiro: Bertrand, 1999, p.77-116."
        ],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert any(item.issue_excerpt.startswith("p.77") and item.suggested_fix.startswith("p. 77") for item in normalized)


def test_normalize_batch_comments_adds_reference_heuristic_for_missing_terminal_period():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0],
        chunks=[
            "SOUZA, Marcelo L. O território: sobre espaço, poder, autonomia e desenvolvimento. Rio de Janeiro: Bertrand, 1999"
        ],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert any(item.message == "A referência termina sem ponto final." for item in normalized)


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


def test_extract_paragraphs_with_metadata_includes_inherited_italic_metadata(tmp_path):
    docx_path = tmp_path / "mini_italic_heading.docx"
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
      <w:pPr><w:pStyle w:val="Heading3"/></w:pPr>
      <w:r><w:t>Subtítulo em itálico</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:rPr><w:i/></w:rPr>
  </w:style>
</w:styles>"""

    with ZipFile(docx_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)

    items = extract_paragraphs_with_metadata(docx_path)

    assert items[0].block_type == "heading"
    assert items[0].is_italic is True
    assert "italico=sim" in items[0].ref_label


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


def test_extract_paragraphs_with_metadata_recognizes_scientific_front_matter_and_references():
    items = extract_paragraphs_with_metadata(SAMPLE_DOCX)

    assert items[1].block_type == "title"
    assert items[2].block_type == "author_line"
    assert items[7].block_type == "abstract_heading"
    assert items[8].block_type == "abstract_body"
    assert items[9].block_type == "keywords_label"
    assert items[10].block_type == "keywords_content"
    assert items[11].block_type == "jel_code"
    assert items[12].block_type == "abstract_heading"
    assert items[14].block_type == "keywords_label"
    assert items[15].block_type == "keywords_content"
    assert items[16].block_type == "jel_code"
    assert items[338].block_type == "reference_entry"
    assert items[349].block_type == "reference_entry"
    assert items[389].block_type == "reference_entry"


def test_reference_prompt_support_context_loads_local_norms():
    support_context = _build_agent_support_context("referencias")

    assert "custom_2026_associacao-brasileira-de-normas-tecnicas-ipea" in support_context
    assert "[CSL:author]" in support_context
    assert "[TD-CLS:cslsetup]" in support_context
    assert "[PREAMBLE:hypersetup]" in support_context


def test_reference_prompt_instruction_mentions_explicit_local_diagnosis():
    instruction = load_agent_instruction("referencias", "td")

    assert "o que está faltando" in instruction
    assert "onde está o erro" in instruction
    assert "Referência incompleta" in instruction
    assert "Há duplicação de local e editora" in instruction
    assert "A referência online informa a URL" in instruction
    assert "Inserir \\`Acesso em:\\` com a data de consulta após a URL." in instruction


def test_heuristic_reference_comments_flags_missing_access_date():
    comments = _heuristic_reference_comments(
        batch_indexes=[0],
        chunks=[
            "GONDIM, Grácia M.; MONKEN, Maurício. Território e territorialização. In: GONDIM, Grácia M. M.; CHRISTÓFARO, Maria A. C.; MIYASHIRO, Gladys (org.). Técnico de vigilância em saúde: contexto e identidade. Rio de Janeiro: EPSJV, 2017. p. 21-44. Disponível em: https://www.epsjv.fiocruz.br/sites/default/files/livro1.pdf."
        ],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert any(item.message == "A referência online informa a URL, mas não traz `Acesso em:` ao final." for item in comments)
    assert any(item.suggested_fix == "Inserir `Acesso em:` com a data de consulta após a URL." for item in comments)


def test_heuristic_reference_comments_flags_duplicated_place_and_publisher():
    comments = _heuristic_reference_comments(
        batch_indexes=[0],
        chunks=["GONDIM, G. M. de M.; MONKEN, M. Territorialização em saúde. Rio de Janeiro: Rio de Janeiro, 2009."],
        refs=["parágrafo 1 | tipo=reference_entry"],
    )

    assert any(item.message == "Há duplicação de local e editora no trecho final da referência." for item in comments)
    assert any(item.issue_excerpt == "Rio de Janeiro: Rio de Janeiro, 2009" for item in comments)


def test_typography_prompt_support_context_loads_local_norms():
    support_context = _build_agent_support_context("tipografia")

    assert "Times New Roman" in support_context
    assert "texto_referencia" in support_context
    assert "Títulos e subtítulos" in support_context
    assert "A família de fonte é apenas referência de template" in support_context
    assert "titulo_3: case=mixed; size_pt=12; bold=true; italic=false" in support_context


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


def test_build_comment_lines_for_item_prioritizes_suggested_fix():
    item = AgentComment(
        agent="gramatica_ortografia",
        category="ConcordÃ¢ncia",
        message="Ajustar concordÃ¢ncia verbal.",
        suggested_fix="vÃªm sendo",
    )

    lines = _build_comment_lines_for_item(item, ordinal=1)

    assert lines == ["Correção: vÃªm sendo"]


def test_build_comment_lines_for_item_falls_back_to_message_when_no_fix():
    item = AgentComment(
        agent="referencias",
        category="InconsistÃªncia",
        message="HÃ¡ uma inconsistÃªncia a verificar.",
        suggested_fix="",
    )

    lines = _build_comment_lines_for_item(item, ordinal=1)

    assert lines == ["HÃ¡ uma inconsistÃªncia a verificar."]


def test_agent_output_contract_requests_diagnosis_and_fix():
    contract = agent_output_contract_text()

    assert "use `message` para explicar de forma natural e objetiva o que está errado ou faltando no trecho" in contract
    assert "Use `suggested_fix` para trazer a correção exata do fragmento" in contract
    assert "nunca mencione hipóteses descartadas" in contract


def test_is_connection_error_detects_dns_resolution_failure():
    exc = RuntimeError("Connection error")
    exc.__cause__ = OSError("[Errno 11001] getaddrinfo failed")

    assert _is_connection_error(exc) is True
    assert _connection_error_summary(exc) == "falha de DNS/conectividade (`getaddrinfo failed`)"


def test_invoke_with_retry_retries_connection_failure_and_then_succeeds(monkeypatch):
    attempts = []

    class FakeRunnable:
        def invoke(self, payload):
            attempts.append(payload)
            if len(attempts) < 3:
                raise RuntimeError("Connection error")
            return {"ok": True}

    monkeypatch.setattr(graph_chat_module, "get_llm_retry_config", lambda: {"max_retries": 3, "backoff_seconds": 0.0})

    result = _invoke_with_retry(FakeRunnable(), {"question": "q"}, operation="teste")

    assert result == {"ok": True}
    assert len(attempts) == 3


def test_invoke_with_model_fallback_uses_secondary_provider_after_openai_failure(monkeypatch):
    calls = []

    class FakePrompt:
        def __or__(self, model):
            return model

    monkeypatch.setattr(
        graph_chat_module,
        "get_chat_models",
        lambda: [
            ({"provider": "openai", "model": "gpt-5.2"}, "primary"),
            ({"provider": "openai_compatible", "model": "modelo-interno"}, "secondary"),
        ],
    )

    def fake_invoke_with_retry(runnable, payload, operation):
        calls.append(operation)
        if "openai:gpt-5.2" in operation:
            raise graph_chat_module.LLMConnectionFailure("teste", 3, RuntimeError("Connection error"))
        return {"ok": "fallback"}

    monkeypatch.setattr(graph_chat_module, "_invoke_with_retry", fake_invoke_with_retry)

    result = graph_chat_module._invoke_with_model_fallback(prompt=FakePrompt(), payload={"q": "1"}, operation="agente")

    assert result == {"ok": "fallback"}
    assert any("openai:gpt-5.2" in call for call in calls)
    assert any("openai_compatible:modelo-interno" in call for call in calls)


def test_run_conversation_returns_partial_result_when_connection_fails(monkeypatch):
    class FakeAgentApp:
        def stream(self, initial_state, stream_mode="updates"):
            yield {
                "sinopse_abstract": {
                    "comments": initial_state["comments"],
                    "batch_status": "falha de conexão da LLM após retries: falha de DNS/conectividade (`getaddrinfo failed`)",
                }
            }

    monkeypatch.setattr(graph_chat_module, "_build_graph", lambda agent_order, include_coordinator=False: FakeAgentApp())

    result = run_conversation(
        paragraphs=["SINOPSE", "Texto de teste."],
        refs=["parágrafo 1 | tipo=abstract_heading", "parágrafo 2 | tipo=abstract_body | align=justify"],
        sections=[],
        question="Revise",
        selected_agents=["sinopse_abstract", "gramatica_ortografia"],
    )

    assert result.comments == []
    assert "Resumo parcial" in result.answer
    assert "sinopse_abstract" in result.answer
    assert "falha de conexão da LLM" in result.answer


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

    assert all(item.message != "Adicionar a fonte dos dados utilizados na Tabela 2, conforme o padrão editorial." for item in normalized)
    assert any(item.message == "Na legenda, o identificador deve ficar na primeira linha e o título descritivo na linha abaixo." for item in normalized)


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
    assert normalized[0].auto_apply is False


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
    assert normalized[0].auto_apply is False


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

    assert len(normalized) == 1
    assert normalized[0].auto_apply is False


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
    assert normalized[0].auto_apply is False


def test_normalize_batch_comments_discards_speculative_reference_doi_completion():
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


def test_normalize_batch_comments_discards_table_caption_auto_apply_even_when_marked_safe():
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

    assert normalized == []


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

    assert all(item.message != "Identificador do gráfico está correto, mas a fonte não está presente." for item in normalized)
    assert all(item.message != "O bloco está sem uma linha de fonte ou elaboração logo abaixo da legenda." for item in normalized)
    assert any(item.message == "Na legenda, o identificador deve ficar na primeira linha e o título descritivo na linha abaixo." for item in normalized)


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


def test_normalize_batch_comments_adds_table_caption_split_heuristic():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="tabelas_figuras",
        batch_indexes=[0],
        chunks=["Tabela 2: Decomposição do índice de Gini da renda total familiar per capita"],
        refs=["parágrafo 1 | tipo=caption"],
    )

    assert any(item.message == "Na legenda, o identificador deve ficar na primeira linha e o título descritivo na linha abaixo." for item in normalized)
    assert any(item.suggested_fix == "Separar em duas linhas: `TABELA 2` na primeira linha e `Decomposição do índice de Gini da renda total familiar per capita` na linha abaixo." for item in normalized)


def test_normalize_batch_comments_adds_table_missing_source_heuristic_when_block_has_no_source_line():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="tabelas_figuras",
        batch_indexes=[0, 1, 2],
        chunks=["Tabela 2: Decomposição do índice de Gini", "Tipo de rendimento", "Coeficiente de concentração"],
        refs=["parágrafo 1 | tipo=caption", "parágrafo 2 | tipo=table_cell", "parágrafo 3 | tipo=table_cell"],
    )

    assert any(item.message == "O bloco está sem uma linha de fonte ou elaboração logo abaixo da legenda." for item in normalized)
    assert any(item.suggested_fix == "Adicionar uma linha própria com `Fonte:` ou `Elaboração:` abaixo do bloco." for item in normalized)


def test_normalize_batch_comments_does_not_add_table_missing_source_heuristic_when_source_line_exists():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="tabelas_figuras",
        batch_indexes=[0, 1],
        chunks=["Gráfico 6: Efeito simulado", "Fonte: Portal Beneficiômetro da Seguridade Social"],
        refs=["parágrafo 1 | tipo=caption", "parágrafo 2 | tipo=paragraph"],
    )

    assert all(item.message != "O bloco está sem uma linha de fonte ou elaboração logo abaixo da legenda." for item in normalized)


def test_normalize_batch_comments_adds_synopsis_alignment_heuristic_for_non_justified_abstract():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=["This abstract discusses the collective benefits of social security."],
        refs=["parágrafo 1 | tipo=abstract_body | align=left"],
    )

    assert any(item.message == "O abstract deve estar justificado, mas este parágrafo está com outro alinhamento." for item in normalized)
    assert any(item.suggested_fix == "Justificar o parágrafo do abstract." for item in normalized)


def test_normalize_batch_comments_skips_synopsis_alignment_heuristic_when_abstract_is_justified():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="sinopse_abstract",
        batch_indexes=[0],
        chunks=["This abstract discusses the collective benefits of social security."],
        refs=["parágrafo 1 | tipo=abstract_body | align=justify"],
    )

    assert all(item.message != "O abstract deve estar justificado, mas este parágrafo está com outro alinhamento." for item in normalized)


def test_normalize_batch_comments_adds_reference_global_comment_for_uncited_entry():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[1, 2, 3],
        chunks=[
            "Segundo Silva (2020), a política produziu efeitos coletivos relevantes.",
            "Referências",
            "SILVA, João. Política social no Brasil. Rio de Janeiro: Editora X, 2020.",
            "SOUZA, Maria. Benefícios coletivos. São Paulo: Editora Y, 2021.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=reference_heading",
            "parágrafo 3 | tipo=reference_entry",
            "parágrafo 4 | tipo=reference_entry",
        ],
    )

    assert any(item.message == "Há referências na lista que não foram localizadas nas citações do corpo do texto." and item.paragraph_index == 1 for item in normalized)
    assert any("SOUZA (2021)" in item.suggested_fix for item in normalized)


def test_normalize_batch_comments_adds_reference_global_comment_for_citation_missing_from_reference_list():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0, 1, 2],
        chunks=[
            "Segundo Silva (2020), a política produziu efeitos coletivos relevantes.",
            "Referências",
            "SOUZA, Maria. Benefícios coletivos. São Paulo: Editora Y, 2021.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=reference_heading",
            "parágrafo 3 | tipo=reference_entry",
        ],
    )

    assert any("Há citações no corpo do texto sem correspondência clara na lista de referências." == item.message and item.paragraph_index == 1 for item in normalized)
    assert any("silva (2020)" in item.suggested_fix.casefold() for item in normalized)
    assert any(item.category == "citation_match" and item.paragraph_index == 0 for item in normalized)
    assert any(item.issue_excerpt == "Silva (2020)" for item in normalized)


def test_normalize_batch_comments_does_not_repeat_reference_global_comment_outside_heading_batch():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0],
        chunks=[
            "Segundo Silva (2020), a política produziu efeitos coletivos relevantes.",
            "Referências",
            "SOUZA, Maria. Benefícios coletivos. São Paulo: Editora Y, 2021.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=reference_heading",
            "parágrafo 3 | tipo=reference_entry",
        ],
    )

    assert all(item.paragraph_index != 1 for item in normalized)
    assert any(item.category == "citation_match" and item.paragraph_index == 0 for item in normalized)


def test_agent_scope_indexes_for_references_includes_body_citation_paragraphs():
    indexes = _agent_scope_indexes(
        "referencias",
        chunks=[
            "Segundo Silva (2020), a política produziu efeitos coletivos relevantes.",
            "Texto sem citação.",
            "Referências",
            "SILVA, João. Política social no Brasil. Rio de Janeiro: Editora X, 2020.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=paragraph",
            "parágrafo 3 | tipo=reference_heading",
            "parágrafo 4 | tipo=reference_entry",
        ],
        sections=[],
    )

    assert 0 in indexes
    assert 2 in indexes
    assert 3 in indexes


def test_normalize_batch_comments_ignores_law_year_as_bibliographic_citation():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0, 1, 2],
        chunks=[
            "Nos termos da Lei nº 7.347, de 1985, a tutela coletiva possui disciplina própria.",
            "Referências",
            "SILVA, João. Política social no Brasil. Rio de Janeiro: Editora X, 2020.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=reference_heading",
            "parágrafo 3 | tipo=reference_entry",
        ],
    )

    assert all(item.category != "citation_match" for item in normalized)


def test_normalize_batch_comments_matches_first_reference_when_two_entries_are_glued():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[0, 1, 2],
        chunks=[
            "Segundo Deslandes (2006), a humanização dos cuidados exige revisão crítica.",
            "Referências",
            "DESLANDES, Suely. Humanização: revisitando o conceito a partir das contribuições da sociologia médica. In: DESLANDES, S. F. et al. Humanização dos cuidados em saúde: conceitos, dilemas e práticas. Rio de Janeiro: Fiocruz, p. 33-47, 2006. DURKHEIM, E. Da divisão do trabalho social. São Paulo: Martins Fontes, 1999.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=reference_heading",
            "parágrafo 3 | tipo=reference_entry",
        ],
    )

    assert all(
        not (
            item.category == "citation_match"
            and item.issue_excerpt == "Deslandes (2006)"
        )
        for item in normalized
    )


def test_normalize_batch_comments_adds_reference_citation_format_comment_for_missing_space():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="referencias",
        batch_indexes=[1],
        chunks=[
            "Texto de abertura.",
            "Segundo Mation(2025), a política produziu efeitos distributivos.",
            "Referências",
            "MATION, Lucas. Política distributiva. Brasília: Ipea, 2025.",
        ],
        refs=[
            "parágrafo 1 | tipo=paragraph",
            "parágrafo 2 | tipo=paragraph",
            "parágrafo 3 | tipo=reference_heading",
            "parágrafo 4 | tipo=reference_entry",
        ],
    )

    assert any(item.category == "citation_format" for item in normalized)
    assert any(item.issue_excerpt == "Mation(2025)" for item in normalized)
    assert any(item.suggested_fix == "Mation (2025)" for item in normalized)


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


def test_apply_comments_to_docx_does_not_auto_apply_typography_formatting(tmp_path):
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

    assert "Times New Roman" not in document_xml
    assert 'w:jc w:val="both"' not in document_xml


def test_apply_comments_to_docx_does_not_auto_apply_structure_heading_normalization(tmp_path):
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

    assert "1 Introdução" in document_xml


def test_apply_comments_to_docx_does_not_auto_apply_reference_normalization(tmp_path):
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

    assert "Título do artigo , v. 2" in document_xml


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
    assert "Achados consolidados neste trecho:" not in text
    assert "1. [est]" not in text
    assert "2. [gram]" not in text
    assert "Parágrafo com dois ajustes." in text
    assert "Parágrafo com dois ajustes!" in text
    assert "Trecho:" not in text


def test_agent_order_excludes_conformidade_estilos():
    assert "conformidade_estilos" not in AGENT_ORDER
    assert "metadados" not in AGENT_ORDER
    assert "sinopse_abstract" in AGENT_ORDER
    assert "estrutura" not in AGENT_ORDER


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


def test_normalize_batch_comments_accepts_typography_capitalization_instruction():
    comments = [
        AgentComment(
            agent="tipografia",
            category="heading",
            message="Título deve estar em negrito e em caixa alta.",
            paragraph_index=0,
            issue_excerpt="Introdução",
            suggested_fix="Aplicar negrito e caixa alta.",
            auto_apply=True,
            format_spec="size_pt=12; bold=true; case=upper",
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

    assert len(normalized) == 1
    assert normalized[0].format_spec == "size_pt=12; bold=true; case=upper"


def test_normalize_batch_comments_discards_font_only_typography_instruction():
    comments = [
        AgentComment(
            agent="tipografia",
            category="heading",
            message="Ajustar a fonte do título.",
            paragraph_index=0,
            issue_excerpt="Introdução",
            suggested_fix="Aplicar a fonte do template.",
            auto_apply=True,
            format_spec="font=Times New Roman",
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


def test_normalize_batch_comments_discards_references_emphasis_guess():
    comments = [
        AgentComment(
            agent="referencias",
            category="Inconsistência de título",
            message="O título do artigo deve estar em itálico.",
            paragraph_index=0,
            issue_excerpt="SHEI, AMIE. Brazil’s conditional cash transfer program associated with declines in infant mortality rates. Health Affairs, v. 32, n. 7, p. 1274-1281, 2013.",
            suggested_fix="SHEI, AMIE. *Brazil’s conditional cash transfer program associated with declines in infant mortality rates*. Health Affairs, v. 32, n. 7, p. 1274-1281, 2013.",
        )
    ]
    chunks = [
        "SHEI, AMIE. Brazil’s conditional cash transfer program associated with declines in infant mortality rates. Health Affairs, v. 32, n. 7, p. 1274-1281, 2013."
    ]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_in_comment_when_in_already_exists():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message='Uso incorreto de "In:". O correto é "In:" seguido do título da obra.',
            paragraph_index=0,
            issue_excerpt="In: Jaccoud, L (org). Coordenação e relações intergovernamentais",
            suggested_fix="In: JACCOUD, L. (Org.). Coordenação e relações intergovernamentais",
        )
    ]
    chunks = [
        "Jaccoud, Luciana. Coordenação e territórios no Suas: o caso do Paif. In: Jaccoud, L (org). Coordenação e relações intergovernamentais nas políticas sociais brasileiras. Brasília, IPEA, 2020."
    ]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_volume_guess_when_v_is_absent():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message='Falta de espaço entre "v." e o número do volume.',
            paragraph_index=0,
            issue_excerpt="Bulletin of the World Health Organization, 89, 496-503.",
            suggested_fix="Bulletin of the World Health Organization, v. 89, 496-503.",
        )
    ]
    chunks = ["Bulletin of the World Health Organization, 89, 496-503."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_point_after_n_false_positive():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message='Falta de ponto após "n." na referência.',
            paragraph_index=0,
            issue_excerpt="Texto para Discussão n. 2919",
            suggested_fix="Texto para Discussão n. 2919.",
        )
    ]
    chunks = ["Jaccoud, Luciana. Seguridade social: por uma análise macrossetorial. Rio de Janeiro: IPEA, Texto para Discussão n. 2919, 2023."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_n_degree_guess():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message='Uso incorreto de "n." para numeração de textos.',
            paragraph_index=0,
            issue_excerpt="Texto para Discussão n. 2919",
            suggested_fix="Texto para Discussão n° 2919",
        )
    ]
    chunks = ["Jaccoud, Luciana. Seguridade social: por uma análise macrossetorial. Rio de Janeiro: IPEA, Texto para Discussão n. 2919, 2023."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_colon_spacing_false_positive():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message='Falta de espaço após ":" na descrição do documento.',
            paragraph_index=0,
            issue_excerpt="A Theory of Justice. Cambridge: Harvard University Press, 1971",
            suggested_fix="A Theory of Justice. Cambridge: Harvard University Press, 1971.",
        )
    ]
    chunks = ["Rawls, J. A Theory of Justice. Cambridge: Harvard University Press, 1971"]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert all(item.message != 'Falta de espaço após ":" na descrição do documento.' for item in normalized)
    assert any(item.message == "A referência termina sem ponto final." for item in normalized)


def test_normalize_batch_comments_discards_reference_texto_para_discussao_punctuation_guess():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="Falta de pontuação entre o título e a editora.",
            paragraph_index=0,
            issue_excerpt="IPEA, Texto para Discussão (TD) 2941, 2023.",
            suggested_fix="IPEA: Texto para Discussão (TD) 2941, 2023.",
        )
    ]
    chunks = ["ANSILIERO, Gabriela et al. Beneficiômetro da seguridade social: um panorama da previdência social brasileira a partir de indicadores clássicos. IPEA, Texto para Discussão (TD) 2941, 2023."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_point_after_number_false_positive():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="Inserir ponto final após o número.",
            paragraph_index=0,
            issue_excerpt="n. 11,",
            suggested_fix="n. 11.",
        )
    ]
    chunks = ["ZAVASCKI, Teori Albino. Defesa de direitos coletivos e defesa coletiva de direitos. Revista da Faculdade de Direito da UFRGS, n. 11,"]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_reference_false_positive_about_terminal_period():
    comments = [
        AgentComment(
            agent="referencias",
            category="inconsistency",
            message="Falta pontuação final na referência.",
            paragraph_index=0,
            issue_excerpt="Rio de Janeiro: Bertrand, 1999.",
            suggested_fix="Rio de Janeiro: Bertrand, 1999.",
        )
    ]
    chunks = ["SOUZA, Marcelo L. O território. Rio de Janeiro: Bertrand, 1999."]
    refs = ["parágrafo 1 | tipo=reference_entry"]

    normalized = _normalize_batch_comments(
        comments,
        agent="referencias",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_normalize_batch_comments_discards_references_year_change_guess():
    comments = [
        AgentComment(
            agent="referencias",
            category="Inconsistência de ano",
            message="O ano deve ser corrigido.",
            paragraph_index=0,
            issue_excerpt="DELGADO, G.; CARDOSO JR., J. C.. Principais resultados da pesquisa domiciliar sobre a previdência rural na região sul do Brasil. Rio de Janeiro: Ipea, 2023.",
            suggested_fix="DELGADO, G.; CARDOSO JR., J. C.. Principais resultados da pesquisa domiciliar sobre a previdência rural na região sul do Brasil. Rio de Janeiro: Ipea, 2000.",
        )
    ]
    chunks = [
        "DELGADO, G.; CARDOSO JR., J. C.. Principais resultados da pesquisa domiciliar sobre a previdência rural na região sul do Brasil. Rio de Janeiro: Ipea, 2023."
    ]
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


def test_agent_scope_indexes_starts_estrutura_from_intro_and_prefers_short_titles():
    chunks = [
        "SINOPSE",
        "1 INTRODUÇÃO",
        "Texto corrido da introdução.",
        "2 Classificação dos benefícios coletivos",
        "Texto.",
        "3 Considerações finais",
        "Texto final.",
        "4 Título estrutural excessivamente longo para o filtro",
    ]
    refs = [
        "parágrafo 1 | tipo=heading | estilo=Heading 1",
        "parágrafo 2 | tipo=heading | estilo=Heading 1",
        "parágrafo 3 | tipo=paragraph",
        "parágrafo 4 | tipo=heading | estilo=Heading 1",
        "parágrafo 5 | tipo=paragraph",
        "parágrafo 6 | tipo=heading | estilo=Heading 1",
        "parágrafo 7 | tipo=paragraph",
        "parágrafo 8 | tipo=heading | estilo=Heading 1",
    ]

    picked = _agent_scope_indexes("estrutura", chunks, refs, sections=[])

    assert picked == [1, 3, 5, 7]


def test_agent_scope_indexes_accepts_implicit_short_heading_without_formatting():
    chunks = [
        "SINOPSE",
        "1 Introdução",
        "Texto de abertura do trabalho com desenvolvimento suficiente para corpo do texto.",
        "Resultados",
        "Este parágrafo descreve os resultados do estudo com conteúdo corrido e mais de seis palavras.",
    ]
    refs = [
        "parágrafo 1 | tipo=heading",
        "parágrafo 2 | tipo=heading",
        "parágrafo 3 | tipo=paragraph",
        "parágrafo 4 | tipo=paragraph",
        "parágrafo 5 | tipo=paragraph",
    ]

    picked = _agent_scope_indexes("estrutura", chunks, refs, sections=[])

    assert picked == [1, 3]


def test_normalize_batch_comments_adds_structure_numbering_for_consideracoes_finais_when_intro_is_numbered():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="estrutura",
        batch_indexes=[0, 1, 2, 3, 4, 5],
        chunks=[
            "Introdução",
            "Classificação dos benefícios coletivos",
            "Classificação pela oferta dos benefícios",
            "Classificação por resultados das políticas públicas",
            "Considerações finais",
            "Referências",
        ],
        refs=[
            "parágrafo 1 | tipo=heading | estilo=heading 1 | numerado=sim",
            "parágrafo 2 | tipo=heading | estilo=heading 1",
            "parágrafo 3 | tipo=heading | estilo=heading 1",
            "parágrafo 4 | tipo=heading | estilo=heading 1",
            "parágrafo 5 | tipo=heading | estilo=heading 1",
            "parágrafo 6 | tipo=reference_heading | estilo=heading 1",
        ],
    )

    assert any(item.issue_excerpt == "Considerações finais" and item.suggested_fix == "5. Considerações finais" for item in normalized)
    assert any(item.issue_excerpt == "Referências" and item.suggested_fix == "6. Referências" for item in normalized)


def test_normalize_batch_comments_does_not_add_structure_numbering_when_intro_is_not_numbered():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="estrutura",
        batch_indexes=[0, 1],
        chunks=["Introdução", "Considerações finais"],
        refs=[
            "parágrafo 1 | tipo=heading | estilo=heading 1",
            "parágrafo 2 | tipo=heading | estilo=heading 1",
        ],
    )

    assert normalized == []


def test_normalize_batch_comments_adds_structure_numbering_when_top_level_headings_are_mixed():
    normalized = _normalize_batch_comments(
        comments=[],
        agent="estrutura",
        batch_indexes=[0, 1, 2, 3],
        chunks=[
            "Introdução",
            "Classificação dos benefícios coletivos",
            "Considerações finais",
            "Referências",
        ],
        refs=[
            "parágrafo 1 | tipo=heading | estilo=heading 1",
            "parágrafo 2 | tipo=heading | estilo=heading 1",
            "parágrafo 3 | tipo=heading | estilo=heading 1 | numerado=sim",
            "parágrafo 4 | tipo=reference_heading | estilo=heading 1 | numerado=sim",
        ],
    )

    assert any(item.issue_excerpt == "Introdução" and item.suggested_fix == "1. Introdução" for item in normalized)
    assert any(item.issue_excerpt == "Classificação dos benefícios coletivos" and item.suggested_fix == "2. Classificação dos benefícios coletivos" for item in normalized)


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


def test_normalize_batch_comments_discards_structure_comment_with_paragraph_reference():
    comments = [
        AgentComment(
            agent="estrutura",
            category="Hierarquia quebrada",
            message="A seção 'Referências' (parágrafo 328) está em nível incompatível.",
            paragraph_index=0,
            issue_excerpt="Referências",
            suggested_fix="Alterar a numeração da seção, por exemplo, '2 Referências'.",
        )
    ]
    chunks = ["Referências"]
    refs = ["parágrafo 1 | tipo=heading"]

    normalized = _normalize_batch_comments(
        comments,
        agent="estrutura",
        batch_indexes=[0],
        chunks=chunks,
        refs=refs,
    )

    assert normalized == []


def test_reference_td_prompt_restricts_missing_field_and_cross_reference_inference():
    instruction = load_agent_instruction("referencias", "td")

    assert "Não cobrar volume, número, editora, local, data, DOI" in instruction
    assert "Não usar comparação com \"as demais referências\"" in instruction


def test_grammar_td_prompt_restricts_style_and_optional_comma_claims():
    instruction = load_agent_instruction("gramatica_ortografia", "td")

    assert "não comentar redundância, repetição vocabular, concisão" in instruction
    assert "não comentar regência, preposição ou colocação pronominal quando a construção admitir variação culta plausível" in instruction
    assert "não pedir vírgula facultativa" in instruction


def test_sinopse_td_prompt_restricts_keywords_and_jel_count_claims():
    instruction = load_agent_instruction("sinopse_abstract", profile_key="TD")

    assert "não cobrar quantidade máxima de palavras-chave" in instruction
    assert "não estiver explicitamente declarada no perfil TD" in instruction


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

    assert "Times New Roman" not in document_xml
    assert "<w:comment " in comments_xml

