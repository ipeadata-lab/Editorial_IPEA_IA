# Dataset Ouro

## Objetivo

Criar um conjunto de avaliação humana para medir a qualidade real dos comentários do sistema editorial.

## Unidade de anotação

Cada comentário aceito por uma rodada vira uma anotação candidata com estes campos principais:

- `agent`
- `paragraph_index`
- `issue_excerpt`
- `suggested_fix`
- `model_comment`
- `label`
- `severity`
- `reviewer_note`

Também existe a seção `missed_issues`, usada para registrar problemas que o modelo não encontrou.

## Taxonomia mínima

### Comentários do modelo

- `correto`: comentário certo e útil
- `parcial`: comentário tem valor, mas está incompleto ou impreciso
- `incorreto`: comentário errado, especulativo ou inadequado

### Problemas faltantes

- `faltou`: o modelo deveria ter apontado esse problema e não apontou

### Severidade

- `alta`
- `media`
- `baixa`

## Fluxo recomendado

1. Rodar o modelo no documento.
2. Gerar o scaffold ouro a partir do JSON aceito.
3. Revisar cada comentário manualmente.
4. Registrar em `missed_issues` os problemas relevantes que passaram batido.
5. Consolidar os arquivos anotados em um conjunto de avaliação.
6. Rodar o agregador de métricas reais.

## Geração do scaffold

Exemplo:

```powershell
python -m src.editorial_docx.gold_dataset `
  "D:\github\lang_IPEA_editorial\testes\234362_TD_3125_Benefícios coletivos (53 laudas)_output.relatorio.json" `
  --output "D:\github\lang_IPEA_editorial\testes\dataset_ouro\seed_234362_td_3125.json" `
  --source-document "234362_TD_3125_Benefícios coletivos (53 laudas).docx" `
  --model-name "gpt-5.2" `
  --run-label "seed_inicial"
```

## Observações

- O scaffold não substitui revisão humana.
- As métricas ficam realmente confiáveis quando `correto/parcial/incorreto/faltou` são preenchidos por revisores.
- O ideal é manter pelo menos um documento de cada perfil editorial mais frequente.

## Consolidação de métricas

Exemplo:

```powershell
python -m src.editorial_docx.gold_metrics `
  "D:\github\lang_IPEA_editorial\testes\dataset_ouro" `
  --output "D:\github\lang_IPEA_editorial\testes\dataset_ouro\metricas_reais.json"
```

Saídas calculadas:

- `VP`: comentários corretos
- `VP_parcial`: comentários parcialmente corretos
- `FP`: comentários incorretos
- `FN`: problemas marcados em `missed_issues`
- `VN`: fica `null` neste esquema atual, porque negativos verdadeiros ainda não são observados diretamente
- `precisao`, `recall`, `f1`
- versões ponderadas usando peso para `parcial`
