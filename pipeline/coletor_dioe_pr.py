"""
Coletor ESTADUAL direto: Diário Oficial Executivo do Paraná (DIOE/PR).

Estrutura verificada ao vivo em 30/10/2026 no portal novo https://dioe.pr.gov.br
(o antigo www.documentos.dioe.pr.gov.br falha no TLS):

  GET /busca/busca/buscar/query/{pagina}/di:AAAA-MM-DD/df:AAAA-MM-DD/?1=1&q=<termos>

  * pagina começa em 0; 10 resultados por página; ordenação por data decrescente.
  * Resposta: JSON do Elasticsearch. Cada hit é UMA PÁGINA de uma edição:
      _id          "<diario_id>_<pagina>"
      diario       "EX" (Executivo), além de outros cadernos
      suplemento   "Edição 12257"
      _source      {conteudo (texto integral da página), data, pagina, paginas,
                    diario_id, tipo_edicao, pdf_id}
  * PDF da página: /portal/edicoes/download/<diario_id>/<pagina>

Observação: o texto vem de PDF em duas colunas e pode intercalar atos
vizinhos; o número do ato é identificado pelo cabeçalho mais próximo e deve
ser conferido no PDF (o Dashboard exibe o link da página exata).

Uso:
    python -m pipeline.coletor_dioe_pr                # últimos 5 dias
    python -m pipeline.coletor_dioe_pr --dias 60
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import date, timedelta
from urllib.parse import quote

import requests

from .common import (
    Ato,
    Limitador,
    configurar_log,
    criar_sessao,
    gerar_id,
    hoje_br,
    mesclar_e_salvar,
    registrar_execucao,
    resumo_github,
    rotacionar_user_agent,
)
from .filtro_juridico import chave_numero, extrair_atos, limpar_texto
from .geo import inferir_local

log = logging.getLogger("monitor.dioe_pr")

FONTE = "dioe_pr"
BASE = os.getenv("DIOE_PR_BASE", "https://dioe.pr.gov.br")
CADERNOS = set(filter(None, os.getenv("DIOE_PR_CADERNOS", "EX").split(",")))  # EX = Executivo
MAX_PAGINAS = int(os.getenv("DIOE_PR_MAX_PAGINAS", "30"))  # 30 x 10 = 300 páginas por consulta

# Termos no formato aceito pela busca do portal (frase entre aspas + termo).
CONSULTAS = [
    '"utilidade pública" desapropriação',
    '"interesse social" desapropriação',
    '"servidão administrativa"',
    '"3.365" decreto',
]

RE_EDICAO = re.compile(r"(\d[\d.]*)")


def url_busca(consulta: str, pagina: int, inicio: date, fim: date) -> str:
    return (f"{BASE}/busca/busca/buscar/query/{pagina}/di:{inicio.isoformat()}/df:{fim.isoformat()}"
            f"/?1=1&q={quote(consulta)}")


def buscar(sessao: requests.Session, limitador: Limitador, consulta: str, inicio: date, fim: date,
           erros: list[str]):
    """Gera os hits (páginas de diário) de uma consulta, paginando até o fim."""
    for pagina in range(MAX_PAGINAS):
        url = url_busca(consulta, pagina, inicio, fim)
        limitador.aguardar()
        try:
            r = sessao.get(url, timeout=90)
            if r.status_code == 403:
                rotacionar_user_agent(sessao)
                limitador.aguardar()
                r = sessao.get(url, timeout=90)
            r.raise_for_status()
            dados = r.json()
        except (requests.RequestException, ValueError) as e:
            erros.append(f"{consulta} p{pagina}: {e}")
            log.warning("DIOE/PR %s página %d: %s", consulta, pagina, e)
            return
        hits = (dados.get("hits") or {}).get("hits") or []
        total = int((dados.get("hits") or {}).get("total") or 0)
        if pagina == 0:
            log.info("Consulta %s: %d páginas de diário", consulta, total)
        yield from hits
        if not hits or (pagina + 1) * 10 >= total:
            return


def hit_para_atos(hit: dict) -> list[Ato]:
    """Aplica o filtro jurídico ao texto da página do diário."""
    src = hit.get("_source") or {}
    texto = limpar_texto(src.get("conteudo") or "")
    data_pub = (src.get("data") or "")[:10]
    if not texto or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_pub):
        return []
    diario_id, pag = src.get("diario_id"), src.get("pagina")
    url = f"{BASE}/portal/edicoes/download/{diario_id}/{pag}" if diario_id and pag else f"{BASE}/"
    m_ed = RE_EDICAO.search(str(hit.get("suplemento") or ""))
    edicao = m_ed.group(1) if m_ed else None

    atos = []
    for e in extrair_atos(texto):
        # Município: procura no corpo do ato (a partir da âncora), não na página inteira.
        _, municipio = inferir_local(texto[max(0, e.posicao - 200): e.posicao + 2500], uf_padrao="PR")
        atos.append(Ato(
            id=gerar_id(FONTE, data_pub, chave_numero(e.numero_ato) or e.assinatura),
            data_publicacao=data_pub,
            esfera="Estadual",
            uf="PR",
            municipio=municipio,
            orgao_emissor="Governo do Estado do Paraná",
            numero_ato=e.numero_ato,
            resumo_contexto=e.resumo_contexto,
            url_fonte=url,
            tipo_ato=e.tipo_ato,
            tags=e.tags,
            finalidades=e.finalidades,
            matriculas=e.matriculas,
            areas=e.areas,
            score=e.score,
            confianca=e.confianca,
            fonte=FONTE,
            edicao=edicao,
        ))
    return atos


def executar(dias: int, fim: date | None) -> int:
    fim = fim or hoje_br()
    inicio = fim - timedelta(days=max(dias, 1) - 1)
    sessao = criar_sessao()
    limitador = Limitador(1.5)
    erros: list[str] = []
    vistos: dict[str, dict] = {}
    consultas_ok = 0

    for consulta in CONSULTAS:
        antes = len(erros)
        for hit in buscar(sessao, limitador, consulta, inicio, fim, erros):
            if CADERNOS and hit.get("diario") not in CADERNOS:
                continue
            vistos.setdefault(hit.get("_id") or str(len(vistos)), hit)
        consultas_ok += int(len(erros) == antes)

    atos: dict[str, Ato] = {}
    for hit in vistos.values():
        for a in hit_para_atos(hit):
            if a.id not in atos or a.score > atos[a.id].score:
                atos[a.id] = a
    for a in atos.values():
        log.info("  %s | %s %s | %s", a.data_publicacao, a.tipo_ato, a.numero_ato, a.municipio or "-")

    resultado = mesclar_e_salvar(atos.values())
    estat = {
        "periodo": f"{inicio.isoformat()} a {fim.isoformat()}",
        "consultas_ok": consultas_ok,
        "consultas_total": len(CONSULTAS),
        "paginas_analisadas": len(vistos),
        "atos_extraidos": len(atos),
        "novos": resultado["novos"],
        "atualizados": resultado["atualizados"],
        "erros": erros[:20],
    }
    registrar_execucao(FONTE, estat)
    log.info("Resultado: %s", estat)
    resumo_github(
        "### Coletor estadual direto (DIOE/PR)\n"
        f"* Período: {estat['periodo']} | consultas ok: {consultas_ok}/{len(CONSULTAS)}\n"
        f"* Páginas analisadas: {len(vistos)} | atos: {len(atos)} | novos: {resultado['novos']}\n"
    )
    return 0 if consultas_ok else 2


def main(argv: list[str] | None = None) -> int:
    configurar_log()
    p = argparse.ArgumentParser(description="Coletor DIOE/PR (Diário Oficial Executivo do Paraná)")
    p.add_argument("--dias", type=int, default=int(os.getenv("DIAS_RETROATIVOS_DIOE", "5")))
    p.add_argument("--ate", type=date.fromisoformat, default=None)
    a = p.parse_args(argv)
    return executar(a.dias, a.ate)


if __name__ == "__main__":
    sys.exit(main())
