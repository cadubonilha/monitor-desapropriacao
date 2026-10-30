"""
Coletor FEDERAL: Diário Oficial da União, Seção 1 (e edição extra 1E).

Dois modos, escolhidos automaticamente:

  1. INLABS (preferencial, texto integral em XML). Exige cadastro gratuito em
     https://inlabs.in.gov.br e os secrets INLABS_EMAIL e INLABS_SENHA.
     Protocolo conferido no script oficial (github.com/Imprensa-Nacional/inlabs):
       POST /logar.php (email, password) -> cookie inlabs_session_cookie
       GET  /index.php?p=AAAA-MM-DD&dl=AAAA-MM-DD-DO1.zip  (cabeçalho origem)
     Cada ZIP contém um XML por matéria: <article artType pubDate artCategory
     pdfPage idMateria ...><body><Identifica/><Ementa/><Texto/></body>.
     O INLABS guarda apenas os últimos 4 meses.

  2. Leitura do Jornal (sem cadastro). A página
     https://www.in.gov.br/leiturajornal?data=DD-MM-AAAA&secao=do1 traz embutido
     um <script id="params"> com o JSON "jsonArray" de todas as matérias do dia
     (title, content truncado, artType, hierarchyStr, urlTitle). As candidatas
     têm o texto integral baixado em https://www.in.gov.br/web/dou/-/<urlTitle>.

Uso:
    python -m pipeline.coletor_federal                  # últimos 3 dias
    python -m pipeline.coletor_federal --dias 30
    python -m pipeline.coletor_federal --modo leitura   # força o modo sem cadastro
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

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
from .filtro_juridico import chave_numero, dobrar, extrair_atos, limpar_texto
from .geo import inferir_local

log = logging.getLogger("monitor.federal")

FONTE = "dou"
URL_INLABS = "https://inlabs.in.gov.br"
URL_LEITURA = "https://www.in.gov.br/leiturajornal"
URL_MATERIA = "https://www.in.gov.br/web/dou/-/"
SECOES_INLABS = ["DO1", "DO1E"]

# Pré-filtro barato aplicado a título + ementa/prévia (texto dobrado).
RE_PREFILTRO = re.compile(
    r"desapropria|expropria|servidao\s+administrativa|utilidade\s+publica|interesse\s+social"
    r"|necessidade\s+publica|imissao\s+(?:provisoria\s+)?na\s+posse"
)

# Órgãos emissores recorrentes em DUPs federais (busca no caminho hierárquico).
ORGAOS = [
    ("agencia nacional de energia eletrica", "ANEEL"),
    ("departamento nacional de infraestrutura de transportes", "DNIT"),
    ("instituto nacional de colonizacao e reforma agraria", "INCRA"),
    ("agencia nacional do petroleo", "ANP"),
    ("agencia nacional de transportes terrestres", "ANTT"),
    ("agencia nacional de aguas", "ANA"),
    ("agencia nacional de aviacao civil", "ANAC"),
    ("agencia nacional de transportes aquaviarios", "ANTAQ"),
    ("departamento nacional de obras contra as secas", "DNOCS"),
    ("companhia de desenvolvimento dos vales do sao francisco", "CODEVASF"),
    ("atos do poder executivo", "Presidência da República"),
    ("presidencia da republica", "Presidência da República"),
]

RE_NUM_IDENTIFICA = re.compile(r"\bn[º°o]\.?\s*([\d.]+(?:\s*[/-]\s*\d{2,4})?)", re.I)
RE_DATA_IDENTIFICA = re.compile(r"\bde\s+(\d{1,2}º?\s+de\s+[a-zç]+\s+de\s+\d{4})", re.I)


@dataclass
class Materia:
    """Matéria do DOU normalizada, independente do modo de coleta."""

    data: str              # YYYY-MM-DD
    secao: str
    tipo: str              # artType
    hierarquia: str        # artCategory / hierarchyStr
    identifica: str        # título do ato
    texto: str             # texto integral (ou prévia, no pior caso)
    url: str
    edicao: str | None = None
    id_materia: str | None = None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def html_para_texto(html: str) -> str:
    """Converte o HTML do corpo da matéria em texto corrido, um parágrafo por linha."""
    if not html:
        return ""
    sopa = BeautifulSoup(html, "html.parser")
    return limpar_texto(sopa.get_text("\n"))


def orgao_emissor(hierarquia: str) -> str:
    dob = dobrar(hierarquia or "")
    for chave, sigla in ORGAOS:
        if chave in dob:
            return sigla
    partes = [p.strip() for p in (hierarquia or "").split("/") if p.strip()]
    return partes[-1] if partes else "União"


def numero_do_titulo(identifica: str) -> str | None:
    """Número do ato a partir do título; atos sem número viram 's/nº, de <data>'."""
    m = RE_NUM_IDENTIFICA.search(identifica or "")
    if m:
        return re.sub(r"\s+", "", m.group(1)).rstrip(".")
    d = RE_DATA_IDENTIFICA.search(identifica or "")
    return f"s/nº, de {d.group(1).lower()}" if d else None


def data_br_para_iso(s: str) -> str:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date().isoformat()


def datas_da_janela(dias: int, fim: date) -> list[date]:
    """Todos os dias da janela; dias sem edição apenas não retornam matérias (404 / lista vazia)."""
    todas = [fim - timedelta(days=i) for i in range(max(dias, 1))]
    return sorted(todas)


# ---------------------------------------------------------------------------
# Modo 1: INLABS
# ---------------------------------------------------------------------------
class ClienteInlabs:
    def __init__(self, sessao: requests.Session, email: str, senha: str) -> None:
        self.sessao, self.email, self.senha = sessao, email, senha
        self.logado = False

    def login(self) -> bool:
        r = self.sessao.post(
            f"{URL_INLABS}/logar.php",
            data={"email": self.email, "password": self.senha},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=60,
        )
        self.logado = bool(self.sessao.cookies.get("inlabs_session_cookie"))
        if not self.logado:
            log.error("INLABS: login sem cookie de sessão (HTTP %s). Verifique as credenciais.", r.status_code)
        return self.logado

    def baixar_zip(self, dia: date, secao: str) -> bytes | None:
        """Devolve o ZIP ou None (404 = sem edição no dia; não é erro)."""
        iso = dia.isoformat()
        url = f"{URL_INLABS}/index.php?p={iso}&dl={iso}-{secao}.zip"
        cabecalho = {
            "Cookie": f"inlabs_session_cookie={self.sessao.cookies.get('inlabs_session_cookie')}",
            "origem": "736372697074",  # valor exigido pelo serviço (hex de "script")
        }
        for tentativa in range(2):
            r = self.sessao.get(url, headers=cabecalho, timeout=180)
            if r.status_code == 404:
                return None
            if r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content)):
                return r.content
            # Sessão expirada costuma devolver 200 com HTML de login: refaz login uma vez.
            log.info("INLABS: resposta inválida para %s (HTTP %s); relogando.", url, r.status_code)
            if tentativa == 0 and not self.login():
                break
        return None


def parse_xml_inlabs(conteudo: bytes) -> Materia | None:
    """Converte um XML de matéria do INLABS em Materia."""
    texto_xml = conteudo.decode("utf-8-sig", errors="replace")
    try:
        raiz = ET.fromstring(texto_xml)
    except ET.ParseError:
        return None
    art = raiz if raiz.tag == "article" else raiz.find(".//article")
    if art is None:
        return None
    corpo = art.find("body")

    def campo(nome: str) -> str:
        el = corpo.find(nome) if corpo is not None else None
        return (el.text or "").strip() if el is not None else ""

    identifica = limpar_texto(campo("Identifica"))
    ementa = limpar_texto(campo("Ementa"))
    texto = html_para_texto(campo("Texto"))
    # Garante que o título e a ementa estejam no texto analisado.
    if identifica and identifica[:40] not in texto:
        texto = f"{identifica}\n{ementa}\n{texto}"
    try:
        data = data_br_para_iso(art.get("pubDate", ""))
    except ValueError:
        return None
    return Materia(
        data=data,
        secao=art.get("pubName", ""),
        tipo=art.get("artType", ""),
        hierarquia=art.get("artCategory", ""),
        identifica=identifica,
        texto=texto,
        url=(art.get("pdfPage") or "").replace("http://", "https://", 1),
        edicao=art.get("editionNumber"),
        id_materia=art.get("idMateria") or art.get("id"),
    )


def coletar_inlabs(sessao: requests.Session, datas: list[date], erros: list[str]):
    cliente = ClienteInlabs(sessao, os.environ["INLABS_EMAIL"], os.environ["INLABS_SENHA"])
    if not cliente.login():
        erros.append("INLABS: falha de login")
        return
    for dia in datas:
        for secao in SECOES_INLABS:
            pacote = cliente.baixar_zip(dia, secao)
            if not pacote:
                continue
            with zipfile.ZipFile(io.BytesIO(pacote)) as z:
                nomes = [n for n in z.namelist() if n.lower().endswith(".xml")]
                log.info("INLABS %s %s: %d matérias", dia, secao, len(nomes))
                for nome in nomes:
                    m = parse_xml_inlabs(z.read(nome))
                    if m:
                        yield m
            time.sleep(1)


# ---------------------------------------------------------------------------
# Modo 2: Leitura do Jornal (sem cadastro)
# ---------------------------------------------------------------------------
def extrair_params(html: str) -> dict:
    sopa = BeautifulSoup(html, "html.parser")
    bloco = sopa.find("script", id="params")
    if not bloco or not bloco.string:
        raise ValueError("bloco <script id='params'> ausente (layout do in.gov.br mudou?)")
    return json.loads(bloco.string)


def texto_da_materia(html: str) -> str:
    """Texto integral da página da matéria, com seletores em ordem de preferência."""
    sopa = BeautifulSoup(html, "html.parser")
    for seletor in ("div.texto-dou", "div.materia", "article", "main"):
        no = sopa.select_one(seletor)
        if no and len(no.get_text(strip=True)) > 200:
            return limpar_texto(no.get_text("\n"))
    return ""


def coletar_leitura(sessao: requests.Session, datas: list[date], erros: list[str]):
    limitador = Limitador(2.0)
    for dia in datas:
        secoes = ["do1"]
        idx = 0
        while idx < len(secoes):
            secao = secoes[idx]
            idx += 1
            limitador.aguardar()
            try:
                r = sessao.get(URL_LEITURA, params={"data": dia.strftime("%d-%m-%Y"), "secao": secao}, timeout=90)
                if r.status_code == 403:
                    rotacionar_user_agent(sessao)
                    r = sessao.get(URL_LEITURA, params={"data": dia.strftime("%d-%m-%Y"), "secao": secao}, timeout=90)
                r.raise_for_status()
                params = extrair_params(r.text)
            except (requests.RequestException, ValueError) as e:
                erros.append(f"leitura {dia} {secao}: {e}")
                log.warning("Leitura do Jornal %s %s: %s", dia, secao, e)
                continue
            # Edição extra do dia anunciada em typeNormDay (ex.: {"DO1E": true}).
            if secao == "do1" and (params.get("typeNormDay") or {}).get("DO1E"):
                secoes.append("do1e")
            itens = params.get("jsonArray") or []
            log.info("Leitura %s %s: %d matérias", dia, secao, len(itens))
            for item in itens:
                previa = f"{item.get('title', '')} {item.get('content', '')}"
                if not RE_PREFILTRO.search(dobrar(previa)):
                    continue
                url = URL_MATERIA + (item.get("urlTitle") or "")
                texto = ""
                limitador.aguardar()
                try:
                    rm = sessao.get(url, timeout=90)
                    if rm.status_code == 200:
                        texto = texto_da_materia(rm.text)
                except requests.RequestException as e:
                    log.debug("Matéria indisponível %s: %s", url, e)
                if not texto:
                    texto = limpar_texto(previa)  # prévia truncada: melhor que nada
                try:
                    data = data_br_para_iso(item.get("pubDate") or dia.strftime("%d/%m/%Y"))
                except ValueError:
                    data = dia.isoformat()
                yield Materia(
                    data=data,
                    secao=item.get("pubName") or secao.upper(),
                    tipo=item.get("artType") or "",
                    hierarquia=item.get("hierarchyStr") or "",
                    identifica=limpar_texto(item.get("title") or ""),
                    texto=texto,
                    url=url,
                    edicao=str(item.get("editionNumber") or "") or None,
                )


# ---------------------------------------------------------------------------
# Conversão
# ---------------------------------------------------------------------------
def materia_para_atos(m: Materia) -> list[Ato]:
    """Aplica o filtro jurídico à matéria; uma matéria do DOU é, em regra, um único ato."""
    if not RE_PREFILTRO.search(dobrar(f"{m.identifica} {m.texto[:4000]}")):
        return []
    extraidos = extrair_atos(m.texto)
    if not extraidos:
        return []
    # Matéria = ato único: consolida no grupo de maior pontuação, unindo os metadados.
    principal = max(extraidos, key=lambda e: e.score)
    for e in extraidos:
        if e is principal:
            continue
        for campo in ("tags", "finalidades", "matriculas", "areas"):
            setattr(principal, campo, sorted(set(getattr(principal, campo)) | set(getattr(e, campo))))

    orgao = orgao_emissor(m.hierarquia)
    numero = numero_do_titulo(m.identifica) or principal.numero_ato
    uf, municipio = inferir_local(m.texto)
    return [
        Ato(
            id=gerar_id(FONTE, m.data, orgao, chave_numero(numero) or principal.assinatura),
            data_publicacao=m.data,
            esfera="Federal",
            uf=uf,
            municipio=municipio,
            orgao_emissor=orgao,
            numero_ato=numero,
            resumo_contexto=principal.resumo_contexto,
            url_fonte=m.url,
            tipo_ato=m.tipo or principal.tipo_ato,
            tags=principal.tags,
            finalidades=principal.finalidades,
            matriculas=principal.matriculas,
            areas=principal.areas,
            score=principal.score,
            confianca=principal.confianca,
            fonte=FONTE,
            edicao=m.edicao,
        )
    ]


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def executar(dias: int, fim: date | None, modo: str) -> int:
    fim = fim or hoje_br()
    datas = datas_da_janela(dias, fim)
    tem_credencial = bool(os.getenv("INLABS_EMAIL") and os.getenv("INLABS_SENHA"))
    if modo == "auto":
        modo = "inlabs" if tem_credencial else "leitura"
    if modo == "inlabs" and not tem_credencial:
        log.error("Modo inlabs exige INLABS_EMAIL e INLABS_SENHA.")
        return 2

    log.info("DOU: %s a %s | modo %s", datas[0], datas[-1], modo)
    sessao = criar_sessao()
    erros: list[str] = []
    gerador = coletar_inlabs if modo == "inlabs" else coletar_leitura

    n_materias = 0
    atos: list[Ato] = []
    for m in gerador(sessao, datas, erros):
        n_materias += 1
        novos = materia_para_atos(m)
        for a in novos:
            log.info("  %s | %s %s | %s/%s", a.data_publicacao, a.tipo_ato, a.numero_ato, a.orgao_emissor, a.uf)
        atos.extend(novos)

    # Fallback automático: INLABS falhou por completo, tenta a Leitura do Jornal.
    if modo == "inlabs" and n_materias == 0 and erros:
        log.warning("INLABS sem matérias; tentando a Leitura do Jornal.")
        modo = "inlabs->leitura"
        for m in coletar_leitura(sessao, datas, erros):
            n_materias += 1
            atos.extend(materia_para_atos(m))

    resultado = mesclar_e_salvar(atos)
    estat = {
        "periodo": f"{datas[0].isoformat()} a {datas[-1].isoformat()}",
        "modo": modo,
        "materias_analisadas": n_materias,
        "atos_extraidos": len(atos),
        "novos": resultado["novos"],
        "atualizados": resultado["atualizados"],
        "erros": erros[:20],
    }
    registrar_execucao(FONTE, estat)
    log.info("Resultado: %s", estat)
    resumo_github(
        "### Coletor federal (DOU Seção 1)\n"
        f"* Período: {estat['periodo']} | modo: {modo}\n"
        f"* Matérias analisadas: {n_materias} | atos: {len(atos)} | novos: {resultado['novos']}\n"
        + (f"* Erros: {len(erros)} (ver data/meta.json)\n" if erros else "")
    )
    # Falha total: nenhuma matéria lida e houve erro.
    return 2 if n_materias == 0 and erros else 0


def main(argv: list[str] | None = None) -> int:
    configurar_log()
    p = argparse.ArgumentParser(description="Coletor federal (DOU Seção 1)")
    p.add_argument("--dias", type=int, default=int(os.getenv("DIAS_RETROATIVOS_DOU", "3")))
    p.add_argument("--ate", type=date.fromisoformat, default=None)
    p.add_argument("--modo", choices=["auto", "inlabs", "leitura"], default=os.getenv("DOU_MODO", "auto"))
    a = p.parse_args(argv)
    return executar(a.dias, a.ate, a.modo)


if __name__ == "__main__":
    sys.exit(main())
