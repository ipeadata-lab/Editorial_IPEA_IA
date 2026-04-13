# lang_IPEA_editorial

Sistema de revisão editorial para `.docx` e `.pdf`, com interface em `Streamlit`, orquestração por agentes e exportação de comentários no Word e em JSON.

## Documentação principal

O estado consolidado do projeto, com regras editoriais, comportamento atual dos agentes, decisões normativas e pontos de manutenção, está em:

- [ESTADO_ATUAL_EDITORIAL.md](D:\github\lang_IPEA_editorial\docs\ESTADO_ATUAL_EDITORIAL.md)

Esse documento deve ser tratado como a referência principal do sistema.

## Resumo do comportamento atual

- não há correções automáticas silenciosas;
- o DOCX mostra comentários com diagnóstico e correção;
- o JSON exporta todos os comentários visíveis;
- os agentes ativos na execução padrão são:
  - `sinopse_abstract`
  - `gramatica_ortografia`
  - `tabelas_figuras`
  - `referencias`
  - `tipografia`

## Instalação

```bash
python -m pip install -e .
```

## Configuração da LLM

O arquivo de exemplo está em:

- [.env.example](D:\github\lang_IPEA_editorial\.env.example)

Copie para `.env` e ajuste o provider desejado.

### OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.2
```

### Ollama

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=llama3.1:8b
OLLAMA_API_KEY=ollama
```

### Endpoint compatível com OpenAI

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://servidor-interno/v1
LLM_MODEL=nome-do-modelo
```

Arquivos centrais:
- [llm.py](D:\github\lang_IPEA_editorial\src\editorial_docx\llm.py)
- [streamlit_app.py](D:\github\lang_IPEA_editorial\streamlit_app.py)

## Execução

### Streamlit

```bash
streamlit run streamlit_app.py
```

### CLI

```bash
python -m src.editorial_docx "D:\github\lang_IPEA_editorial\testes\arquivo.docx"
```

## Saídas

- `<nome>_output.docx`
- `<nome>_output.relatorio.json`

## Arquivos centrais do projeto

- [graph_chat.py](D:\github\lang_IPEA_editorial\src\editorial_docx\graph_chat.py)
- [docx_utils.py](D:\github\lang_IPEA_editorial\src\editorial_docx\docx_utils.py)
- [document_loader.py](D:\github\lang_IPEA_editorial\src\editorial_docx\document_loader.py)
- [prompt.py](D:\github\lang_IPEA_editorial\src\editorial_docx\prompts\prompt.py)
- [test_graph_chat.py](D:\github\lang_IPEA_editorial\testes\test_graph_chat.py)

## Testes

```bash
pytest testes\test_graph_chat.py -q
pytest testes\test_llm.py -q
```
