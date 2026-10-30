"""
Coletor ESTADUAL (camada de reserva): Google News RSS restrito a domínios .gov.br.

Por que existe: os Diários Oficiais estaduais (IOEs) têm 27 portais distintos,
vários com consulta só por PDF, captcha ou bloqueio de IP de datacenter. Até
que cada raspador direto seja escrito e validado (o do PR é o próximo, a
partir do reconhecimento em scripts/reconhecimento.py), esta camada capta os
decretos estaduais pelas notícias oficiais dos próprios governos (agências
estaduais de notícias, DERs, companhias de saneamento e energia), que
costumam divulgar as declarações de utilidade pública no dia da publicação.

Limitações assumidas e sinalizadas no registro:
  * url_fonte aponta para a NOTÍCIA oficial, não para o Diário; o registro
    recebe a etiqueta "fonte_secundaria" e confiança no máximo "media".
  * Notícias sobre atos municipais também aparecem; a esfera é inferida
    pelo texto (Governo do Estado/Governador -> Estadual; Prefeitura -> Municipal).

Formato do feed: RSS 2.0 (item: title, link, pubDate RFC 822, description, source).

Uso:
    python -m pipeline.coletor_estadual                 # todas as UFs, últimos 3 dias
    python -m pipeline.coletor_estadual --ufs PR,SC --dias 7
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .common import (
    FUSO_BR,
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
from .filtro_juridico import FINALIDADES, RE_EXC_ENTIDADE, TAGS, dobrar, limpar_texto
from .geo import SIGLAS, inferir_local

log = logging.getLogger("monitor.estadual")

FONTE = "google_news_rss"
URL_RSS = "https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

# Nome do estado com acentos, para a consulta (o Google trata acentos melhor assim).
NOMES_CONSULTA = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
    "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
    "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará",
    "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins",
}

# Notícia relevante: ato declaratório + instituto expropriatório.
RE_DECLARA = re.compile(r"\b(?:declara\w*|decreto\w*|publica\w*|assina\w*)\b")
RE_INSTITUTO = re.compile(
    r"utilidade\s+publica|interesse\s+social|necessidade\s+publica|desapropria\w*|servidao\s+administrativa"
)
RE_OBJETO = re.compile(r"\b(?:area|areas|imove\w+|terreno\w*|faixa\w*|propriedade\w*|desapropria\w*|lote\w*)\b")
# Fase judicial ou indenizatória: fora do foco do projeto.
RE_EXC_NOTICIA = re.compile(
    r"\b(?:justica|juiz\w*|tribunal|stf|stj|tj[a-z]{2}|sentenca|liminar|acao\s+de\s+desapropriacao"
    r"|indenizacao\s+(?:paga|milionaria)|precatorio\w*|leilao)\b"
)
RE_ESTADUAL = re.compile(
    r"\b(?:governo\s+d[oa]\s+estado|governador\w*|governo\s+estadual|governo\s+d[oa]\s+(?:"
    + "|".join(map(re.escape, (dobrar(n) for n in NOMES_CONSULTA.values()))) + r")"
    r"|der[\s\-/]?[a-z]{2}\b|departamento\s+de\s+estradas|sanepar|copel|sabesp|cemig|copasa|casan|celesc"
    r"|embasa|compesa|cagece|caesb|corsan|cedae)\b"
)
RE_MUNICIPAL = re.compile(r"\b(?:prefeitura|prefeit[oa]|municipal|camara\s+de\s+vereadores)\b")
RE_NUMERO = re.compile(r"\bdecreto\s*(?:estadual\s*)?n[º°o]?\.?\s*([\d.]+(?:/\d{2,4})?)", re.I)


def construir_consulta(uf: str, dias: int) -> str:
    estado = NOMES_CONSULTA[uf]
    return (
        f'("utilidade pública" OR "interesse social" OR desapropriação) decreto "{estado}" '
        f"site:gov.br when:{max(dias, 1)}d"
    )


def parse_rss(xml_texto: str) -> list[dict]:
    """Lê os itens do RSS. Devolve dicts com titulo, link, data, descricao, fonte."""
    try:
        raiz = ET.fromstring(xml_texto)
    except ET.ParseError:
        return []
    itens = []
    for it in raiz.iter("item"):
        desc_html = it.findtext("description") or ""
        try:
            data = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(FUSO_BR).date()
        except (TypeError, ValueError):
            data = None
        fonte = it.find("source")
        itens.append({
            "titulo": limpar_texto(it.findtext("title") or ""),
            "link": (it.findtext("link") or "").strip(),
            "data": data,
            "descricao": limpar_texto(BeautifulSoup(desc_html, "html.parser").get_text(" ")),
            "fonte_url": fonte.get("url") if fonte is not None else "",
            "fonte_nome": (fonte.text or "").strip() if fonte is not None else "",
        })
    return itens


def avaliar_noticia(item: dict, uf_consulta: str) -> Ato | None:
    """Aplica o filtro de notícia e converte em Ato; None se não for relevante."""
    if not item["link"] or not item["data"]:
        return None
    fonte_url = (item.get("fonte_url") or "").lower()
    if fonte_url and ".gov.br" not in fonte_url:
        return None  # garantia extra além do operador site:gov.br
    texto = f"{item['titulo']}. {item['descricao']}"
    dob = dobrar(texto)
    if not (RE_DECLARA.search(dob) and RE_INSTITUTO.search(dob) and RE_OBJETO.search(dob)):
        return None
    if RE_EXC_NOTICIA.search(dob) or (RE_EXC_ENTIDADE.search(dob) and "desapropria" not in dob):
        return None

    estadual = bool(RE_ESTADUAL.search(dob))
    esfera = "Estadual" if estadual or not RE_MUNICIPAL.search(dob) else "Municipal"
    uf, municipio = inferir_local(texto, uf_padrao=uf_consulta)
    if uf not in SIGLAS:
        uf = uf_consulta
    m = RE_NUMERO.search(texto)
    numero = m.group(1).rstrip(".") if m else None

    tags = {nome for nome, rx in TAGS.items() if rx.search(dob)} | {"fonte_secundaria"}
    finalidades = sorted(nome for nome, rx in FINALIDADES.items() if rx.search(dob))
    orgao = (item.get("fonte_nome") or "").strip() or (
        f"Governo do Estado ({uf})" if esfera == "Estadual" else "Prefeitura Municipal"
    )
    score = 3 + int(bool(numero)) + int(bool(finalidades)) + int("desapropria" in dob)
    resumo = texto if len(texto) <= 500 else texto[:497].rsplit(" ", 1)[0] + " …"
    return Ato(
        id=gerar_id(FONTE, item["link"]),
        data_publicacao=item["data"].isoformat(),
        esfera=esfera,
        uf=uf,
        municipio=municipio if esfera == "Municipal" or municipio else None,
        orgao_emissor=orgao,
        numero_ato=numero,
        resumo_contexto=resumo,
        url_fonte=item["link"],
        tipo_ato="Notícia oficial",
        tags=sorted(tags),
        finalidades=finalidades,
        score=score,
        confianca="media" if score >= 5 else "baixa",
        fonte=FONTE,
    )


def executar(dias: int, ufs: list[str]) -> int:
    inicio = hoje_br() - timedelta(days=max(dias, 1))
    sessao = criar_sessao()
    limitador = Limitador(3.0)  # gentileza com o Google: uma consulta a cada 3 s
    atos: dict[str, Ato] = {}
    erros: list[str] = []
    ok = 0
    for uf in ufs:
        url = URL_RSS.format(q=quote_plus(construir_consulta(uf, dias)))
        limitador.aguardar()
        try:
            r = sessao.get(url, timeout=60)
            if r.status_code in (403, 429):
                rotacionar_user_agent(sessao)
                limitador.aguardar()
                r = sessao.get(url, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            erros.append(f"{uf}: {e}")
            log.warning("RSS %s: %s", uf, e)
            continue
        ok += 1
        itens = parse_rss(r.text)
        aceitos = 0
        for it in itens:
            if it["data"] and it["data"] < inicio:
                continue
            ato = avaliar_noticia(it, uf)
            if ato and ato.id not in atos:
                atos[ato.id] = ato
                aceitos += 1
        log.info("%s: %d notícias, %d aceitas", uf, len(itens), aceitos)

    resultado = mesclar_e_salvar(atos.values())
    estat = {
        "periodo": f"{inicio.isoformat()} a {hoje_br().isoformat()}",
        "ufs_consultadas": len(ufs),
        "consultas_ok": ok,
        "atos_extraidos": len(atos),
        "novos": resultado["novos"],
        "atualizados": resultado["atualizados"],
        "erros": erros[:20],
    }
    registrar_execucao(FONTE, estat)
    log.info("Resultado: %s", estat)
    resumo_github(
        "### Coletor estadual (notícias oficiais .gov.br)\n"
        f"* UFs: {ok}/{len(ufs)} consultadas | atos: {len(atos)} | novos: {resultado['novos']}\n"
    )
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    configurar_log()
    p = argparse.ArgumentParser(description="Coletor estadual (Google News RSS .gov.br)")
    p.add_argument("--dias", type=int, default=int(os.getenv("DIAS_RETROATIVOS_RSS", "3")))
    p.add_argument("--ufs", default=os.getenv("RSS_UFS", ""), help="vazio = todas")
    a = p.parse_args(argv)
    ufs = [u.strip().upper() for u in a.ufs.split(",") if u.strip()] or sorted(NOMES_CONSULTA)
    invalidas = [u for u in ufs if u not in NOMES_CONSULTA]
    if invalidas:
        p.error(f"UF inválida: {', '.join(invalidas)}")
    return executar(a.dias, ufs)


if __name__ == "__main__":
    sys.exit(main())
