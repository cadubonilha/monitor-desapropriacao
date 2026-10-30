"""
Testes do coletor federal. Usa:
  * amostra_inlabs_real.xml: XML REAL do INLABS (Alvará da PF, 2019), que
    confirma o formato e deve ser descartado pelo filtro;
  * amostra_leitura_real.json: trecho REAL do JSON da Leitura do Jornal
    (set/2026), sem atos expropriatórios;
  * XMLs SINTÉTICOS no mesmo formato, com DUP da ANEEL e decreto do INCRA.
"""
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from pipeline import coletor_federal as cf
from pipeline import common

AQUI = Path(__file__).parent

XML_ANEEL = """﻿<xml><article id="1" name="rea1" pubName="DO1" artType="Resolução Autorizativa" pubDate="20/10/2026"
artCategory="Ministério de Minas e Energia/Agência Nacional de Energia Elétrica/Superintendência de Concessões"
pdfPage="http://pesquisa.in.gov.br/imprensa/jsp/visualiza/index.jsp?data=20/10/2026&amp;jornal=515&amp;pagina=80"
editionNumber="200" idMateria="900001"><body>
<Identifica><![CDATA[RESOLUÇÃO AUTORIZATIVA Nº 16.001, DE 14 DE OUTUBRO DE 2026]]></Identifica>
<Ementa><![CDATA[Declara de utilidade pública, para fins de instituição de servidão administrativa, as áreas de terra necessárias à passagem de linha de transmissão.]]></Ementa>
<Texto><![CDATA[<p class="identifica">RESOLUÇÃO AUTORIZATIVA Nº 16.001, DE 14 DE OUTUBRO DE 2026</p>
<p>O DIRETOR-GERAL DA AGÊNCIA NACIONAL DE ENERGIA ELÉTRICA, com fundamento no Decreto-Lei nº 3.365, de 21 de junho de 1941, resolve:</p>
<p>Art. 1º Declarar de utilidade pública, para fins de instituição de servidão administrativa, em favor da Transmissora Exemplo S.A., as áreas de terra com largura de 60 metros, necessárias à passagem da Linha de Transmissão 230 kV Exemplo, localizada no Município de Ponta Grossa, Estado do Paraná.</p>
<p class="assina">FULANO DE TAL</p>]]></Texto></body><Midias /></article></xml>"""

XML_INCRA = """<xml><article id="2" pubName="DO1" artType="Decreto" pubDate="21/10/2026"
artCategory="Atos do Poder Executivo" pdfPage="http://pesquisa.in.gov.br/x?pagina=2" editionNumber="201" idMateria="900002"><body>
<Identifica><![CDATA[DECRETO DE 20 DE OUTUBRO DE 2026]]></Identifica>
<Ementa><![CDATA[Declara de interesse social, para fins de reforma agrária, o imóvel rural denominado Fazenda Exemplo, situado no Município de Unaí, Estado de Minas Gerais.]]></Ementa>
<Texto><![CDATA[<p>O PRESIDENTE DA REPÚBLICA, no uso das atribuições, tendo em vista o disposto na Lei nº 8.629, de 25 de fevereiro de 1993, e na Lei Complementar nº 76, de 6 de julho de 1993, DECRETA:</p>
<p>Art. 1º Fica declarado de interesse social, para fins de reforma agrária, o imóvel rural denominado Fazenda Exemplo, com área de 1.234,5678 ha, situado no Município de Unaí, Estado de Minas Gerais, objeto da Matrícula nº 12.345.</p>
<p>Art. 2º O Instituto Nacional de Colonização e Reforma Agrária fica autorizado a promover a desapropriação do imóvel rural de que trata este Decreto.</p>]]></Texto></body></article></xml>"""


def _materia(xml):
    return cf.parse_xml_inlabs(xml.encode("utf-8"))


def test_xml_real_do_inlabs_e_lido_e_descartado():
    m = cf.parse_xml_inlabs((AQUI / "amostra_inlabs_real.xml").read_bytes())
    assert m.data == "2019-05-07" and m.tipo == "Alvará" and m.secao == "DO1"
    assert m.url.startswith("https://pesquisa.in.gov.br/")
    assert cf.materia_para_atos(m) == []


def test_aneel_servidao():
    atos = cf.materia_para_atos(_materia(XML_ANEEL))
    assert len(atos) == 1
    a = atos[0]
    assert a.esfera == "Federal" and a.orgao_emissor == "ANEEL"
    assert a.numero_ato == "16.001"
    assert a.uf == "PR" and a.municipio == "Ponta Grossa"
    assert "servidao" in a.tags and "Energia" in a.finalidades
    assert a.data_publicacao == "2026-10-20"


def test_incra_decreto_sem_numero():
    atos = cf.materia_para_atos(_materia(XML_INCRA))
    assert len(atos) == 1
    a = atos[0]
    assert a.orgao_emissor == "Presidência da República"
    assert a.numero_ato == "s/nº, de 20 de outubro de 2026"
    assert a.uf == "MG" and a.municipio == "Unaí"
    assert {"interesse_social", "reforma_agraria"} <= set(a.tags)
    assert "12.345" in a.matriculas


def test_leitura_json_real_extraido():
    params = json.loads((AQUI / "amostra_leitura_real.json").read_text(encoding="utf-8"))
    html = f'<html><script id="params" type="application/json">{json.dumps(params)}</script></html>'
    assert cf.extrair_params(html)["jsonArray"][0]["pubName"] == "DO3"


# --- ponta a ponta com rede simulada ----------------------------------------
class Resp:
    def __init__(self, status=200, texto="", conteudo=b""):
        self.status_code, self.text, self.content = status, texto, conteudo or texto.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cf.requests.HTTPError(str(self.status_code))


class SessaoLeitura:
    def __init__(self):
        self.headers = {}
        params = json.loads((AQUI / "amostra_leitura_real.json").read_text(encoding="utf-8"))
        params["jsonArray"].append({
            "pubName": "DO1", "urlTitle": "resolucao-autorizativa-n-16.001-123", "title":
            "RESOLUÇÃO AUTORIZATIVA Nº 16.001, DE 14 DE OUTUBRO DE 2026", "pubDate": "20/10/2026",
            "content": "Declara de utilidade pública, para fins de instituição de servidão administrativa...",
            "editionNumber": "200", "artType": "Resolução Autorizativa",
            "hierarchyStr": "Ministério de Minas e Energia/Agência Nacional de Energia Elétrica"})
        self.pagina = f'<script id="params" type="application/json">{json.dumps(params)}</script>'
        corpo = cf.html_para_texto(XML_ANEEL.split("<![CDATA[")[3].split("]]>")[0])
        self.materia = f'<html><div class="texto-dou"><p>{corpo}</p></div></html>'

    def get(self, url, params=None, timeout=None, headers=None):
        if url == cf.URL_LEITURA:
            return Resp(texto=self.pagina) if params["data"] == "20-10-2026" else Resp(texto=self.pagina.replace("16.001", "0"))
        if url.startswith(cf.URL_MATERIA):
            return Resp(texto=self.materia)
        return Resp(404)


class SessaoInlabs:
    def __init__(self):
        self.headers = {}
        self.cookies = {}

    def post(self, url, data=None, headers=None, timeout=None):
        self.cookies["inlabs_session_cookie"] = "abc"
        return Resp(200)

    def get(self, url, headers=None, timeout=None):
        if "2026-10-20-DO1.zip" in url:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("a.xml", XML_ANEEL)
                z.writestr("b.xml", (AQUI / "amostra_inlabs_real.xml").read_bytes())
            return Resp(conteudo=buf.getvalue())
        if "2026-10-21-DO1.zip" in url:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("c.xml", XML_INCRA)
            return Resp(conteudo=buf.getvalue())
        return Resp(404)


@pytest.fixture()
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "mesclar_e_salvar", lambda a: common.mesclar_e_salvar(a, tmp_path / "atos.json"))
    monkeypatch.setattr(cf, "registrar_execucao", lambda f, e: common.registrar_execucao(f, e, tmp_path / "meta.json"))
    monkeypatch.setattr(cf.time, "sleep", lambda s: None)
    monkeypatch.setattr(common.Limitador, "aguardar", lambda self: None)
    return tmp_path


def test_modo_inlabs_ponta_a_ponta(ambiente, monkeypatch):
    monkeypatch.setenv("INLABS_EMAIL", "x@y.z")
    monkeypatch.setenv("INLABS_SENHA", "s")
    monkeypatch.setattr(cf, "criar_sessao", SessaoInlabs)
    assert cf.executar(3, date(2026, 10, 21), "auto") == 0
    atos = json.loads((ambiente / "atos.json").read_text(encoding="utf-8"))
    assert sorted(a["orgao_emissor"] for a in atos) == ["ANEEL", "Presidência da República"]
    meta = json.loads((ambiente / "meta.json").read_text(encoding="utf-8"))
    assert meta["fontes"]["dou"]["modo"] == "inlabs"


def test_modo_leitura_ponta_a_ponta_e_dedup(ambiente, monkeypatch):
    monkeypatch.delenv("INLABS_EMAIL", raising=False)
    monkeypatch.setattr(cf, "criar_sessao", SessaoLeitura)
    assert cf.executar(1, date(2026, 10, 20), "auto") == 0
    atos = json.loads((ambiente / "atos.json").read_text(encoding="utf-8"))
    assert len(atos) == 1 and atos[0]["numero_ato"] == "16.001"
    assert atos[0]["url_fonte"].startswith(cf.URL_MATERIA)
    cf.executar(1, date(2026, 10, 20), "auto")
    assert len(json.loads((ambiente / "atos.json").read_text(encoding="utf-8"))) == 1
