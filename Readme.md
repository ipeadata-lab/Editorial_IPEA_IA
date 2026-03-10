# lang_IPEA_editorial

Arquitetura baseada em `Streamlit + LangGraph` para conversar com um documento `.docx` e gerar comentários editoriais de múltiplos agentes.

## O que o app faz

- recebe upload de `.docx` ou `.pdf`;
- detecta sumário/seções para orientar os tópicos;
- seleciona apenas trechos relevantes por pergunta (janela de contexto menor);
- extrai o texto e executa agentes especializados com `LangGraph`;
- responde às perguntas do usuário sobre o documento;
- mostra os comentários gerados por agente;
- exporta um novo `.docx` com comentários inseridos (quando a entrada for DOCX) (sem alterar o texto original).

## Agentes (prompts)

Prompts em `src/editorial_docx/prompts/`:

- `metadados.md`
- `sinopse_abstract.md`
- `estrutura.md`
- `tabelas_figuras.md`
- `referencias.md`
- `conformidade_estilos.md`
- `coordenador.md`

## Configuração

Edite `.env`:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

## Execução

```bash
python -m pip install -e .
streamlit run streamlit_app.py
```

## Estrutura principal

- `streamlit_app.py`: interface de chat e visualização dos comentários.
- `src/editorial_docx/graph_chat.py`: grafo dos agentes e coordenação da resposta.
- `src/editorial_docx/docx_utils.py`: leitura do DOCX e aplicação dos comentários.
- `src/editorial_docx/llm.py`: inicialização do modelo OpenAI via `.env`.
- `src/editorial_docx/models.py`: modelos de dados de comentários e resposta.

## Execução

```
python -m pip install -e .
streamlit run streamlit_app.py
```

## Execução em linha de comando (lote)
Para processar um arquivo diretamente (sem abrir o Streamlit):

```
PYTHONPATH=src python -m editorial_docx "testes/arquivo.docx"
```

## Saídas padrão:

- <nome>_output.docx (para entrada DOCX);

- <nome>_output.relatorio.json (comentários em JSON).

## Estrutura principal
- streamlit_app.py: interface de chat e visualização dos comentários.

- src/editorial_docx/graph_chat.py: grafo dos agentes e coordenação da resposta.

- src/editorial_docx/docx_utils.py: leitura do DOCX e aplicação dos comentários.

- src/editorial_docx/llm.py: inicialização do modelo OpenAI via .env.

- src/editorial_docx/models.py: modelos de dados de comentários e resposta.

Para entrada PDF, o app gera relatório de comentários em JSON com referência de página/bloco.