# Monitor Nacional de Desapropriação

Pipeline gratuito e automatizado (GitHub Actions) que vasculha Diários Oficiais
diariamente em busca de atos declaratórios de desapropriação na fase
administrativa: declaração de utilidade pública, necessidade pública e
interesse social (Decreto-Lei 3.365/1941 e Lei 4.132/1962), inclusive para
instituição de servidão administrativa e reforma agrária (Lei 8.629/1993).

Regra de arquitetura: nenhuma chamada a modelo de IA no ciclo diário. Todo o
processamento é Python puro, Requests, BeautifulSoup e Regex.

## Estrutura

```
.github/workflows/coleta_diaria.yml    coleta 2x ao dia (06:23 e 20:47 de Brasília)
.github/workflows/reconhecimento.yml   execução manual: grava HTML real dos portais em debug/
pipeline/common.py                     esquema do registro, hash, HTTP resiliente, mesclagem do JSON
pipeline/filtro_juridico.py            Regex jurídico: âncoras, exclusões, número, matrícula, área, tags
pipeline/geo.py                        inferência de UF e município pelo texto do ato
pipeline/coletor_municipal.py          Querido Diário (API pública)
pipeline/coletor_federal.py            DOU Seção 1 (INLABS ou Leitura do Jornal)
pipeline/coletor_estadual.py           reserva estadual: notícias oficiais .gov.br (Google News RSS)
scripts/reconhecimento.py              diagnóstico de portais para novos raspadores
scripts/gerar_index.py                 gera index.html (GitHub Pages) a partir de dashboard.html
dashboard.html / index.html            Dashboard (fonte única / versão para GitHub Pages)
tests/                                 22 testes com amostras reais e textos sintéticos
data/atos.json                         acervo consolidado
data/meta.json                         status da última execução de cada fonte
data/exemplo.json                      dados FICTÍCIOS para demonstração do Dashboard
```

## Implantação (uma única vez)

1. Crie um repositório **público** no GitHub (Actions gratuito e leitura Raw sem token).
2. Envie todos os arquivos desta pasta para a branch `main`.
3. *Settings > Actions > General > Workflow permissions*: marque **Read and write permissions**.
4. Recomendado: cadastre-se gratuitamente em https://inlabs.in.gov.br e crie em
   *Settings > Secrets and variables > Actions > Secrets* os segredos
   `INLABS_EMAIL` e `INLABS_SENHA`. Sem eles, o coletor federal usa a Leitura
   do Jornal do in.gov.br (funciona, mas depende de HTML do portal).
5. *Settings > Pages*: Source **Deploy from a branch**, branch `main`, pasta `/ (root)`.
   O Dashboard ficará em `https://<usuario>.github.io/<repositorio>/` e lerá
   `data/atos.json` automaticamente.
6. *Actions > Coleta diaria de decretos > Run workflow*: primeira carga com
   `dias = 60` e `modo_municipal = publicacao`.
7. *Actions > Reconhecimento de portais > Run workflow*: gera `debug/` com o
   HTML real dos portais do PR (insumo para o raspador direto do DIOE).
8. Opcional, em *Variables*: `QD_UFS` e `RSS_UFS` (ex.: `PR,SC,SP`) para
   restringir as UFs nas execuções agendadas.

## Fontes e grau de verificação

| Fonte | Esfera | Como foi validada |
|---|---|---|
| Querido Diário `/gazettes` | Municipal | parâmetros conferidos no código-fonte da API (okfn-brasil/querido-diario-api, v0.19) |
| INLABS (XML) | Federal | protocolo do script oficial da Imprensa Nacional; formato testado com XML real |
| Leitura do Jornal in.gov.br | Federal | estrutura `#params`/`jsonArray` testada com JSON real de set/2026 |
| Google News RSS `.gov.br` | Estadual (reserva) | formato RSS 2.0 padrão; registros marcados como `fonte_secundaria` |
| DIOE/PR direto | Estadual | **pendente**: depende do `debug/` gerado pelo reconhecimento |

Nenhuma das fontes foi chamada ao vivo durante o desenvolvimento (rede
bloqueada no ambiente de desenvolvimento). A primeira execução no Actions é o
teste real: confira `data/meta.json` e o resumo de cada execução.

## Dashboard

* No GitHub Pages: lê `data/atos.json` do próprio repositório; aceita
  `?dados=<URL Raw>` e o botão "Trocar fonte de dados".
* Como página publicada no Claude: endereços externos são bloqueados; use
  "Enviar arquivo" com o `atos.json`. Abre com dados de exemplo fictícios.
* Após editar `dashboard.html`, rode `python scripts/gerar_index.py`.

## Execução local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python -m pipeline.coletor_municipal --dias 7 --ufs PR
python -m pipeline.coletor_federal --dias 3
python -m pipeline.coletor_estadual --ufs PR --dias 7
```

## Esquema do registro (data/atos.json)

Obrigatórios: `id`, `data_publicacao`, `esfera`, `uf`, `municipio`,
`orgao_emissor`, `numero_ato`, `resumo_contexto`, `url_fonte`.

Enriquecimentos: `tipo_ato`, `tags` (utilidade_publica, interesse_social,
necessidade_publica, servidao, urgencia, reforma_agraria, revogacao,
retificacao, matricula, coordenadas, fonte_secundaria, sem_declaracao_explicita),
`finalidades`, `matriculas`, `areas`, `score`, `confianca` (alta, media, baixa),
`fonte`, `url_texto`, `edicao`, `coletado_em`.

Atos federais sem número recebem `numero_ato` no formato `s/nº, de <data>`;
UF `BR` indica âmbito nacional ou UF não identificada no texto.

## Limitações conhecidas

* Cobertura municipal limitada aos municípios indexados pelo Querido Diário.
* Diários escaneados com OCR ruim podem escapar do Regex.
* A camada estadual atual depende de notícias oficiais: aponta para a notícia,
  não para o Diário, e não cobre atos sem divulgação.
* Agendamentos do GitHub Actions podem atrasar em horários de pico.
