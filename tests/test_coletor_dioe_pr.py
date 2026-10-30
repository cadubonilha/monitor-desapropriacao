"""
Testes do coletor DIOE/PR. O conteúdo das páginas reproduz trechos REAIS da
edição nº 12.254 do Diário Oficial Executivo do Paraná (22/10/2026), obtidos
na API de busca do portal em 30/10/2026, no mesmo formato de resposta.
"""
import json
from datetime import date

from pipeline import coletor_dioe_pr as cp
from pipeline import common

PAGINA_4 = (
    "4 5º FEIRA |22/OUT/2026 - EDIÇÃO N° 12.254 PODER EXECUTIVO ESTADUAL Decretos Autônomos Poder Executivo "
    "DECRETO Nº 15.460 Atos do Chefe do Poder Executivo Declara de utilidade pública para fins de desapropriação "
    "o imóvel que especifica. O GOVERNADOR DO ESTADO DO PARANÁ, no uso das atribuições que lhe confere o inciso V "
    "do art. 10 da Constituição Estadual, na alínea “h” do art. 5 do Decreto-Lei nº 3.365, de 21 de junho de 1941, "
    "e tendo em vista o contido no protocolo nº 25.329.011-0, DECRETA: Art. 1° Declara de utilidade pública, para "
    "fins de desapropriação, o imóvel com área total de 54.450,00 m² (cinquenta e quatro mil, quatrocentos e "
    "cinquenta metros quadrados), objeto da Matrícula nº 28.082, do 1º Ofício de Registro de Imóveis de Campo "
    "Mourão, no Município de Campo Mourão, para instalação e funcionamento de serviços públicos estaduais."
)

PAGINA_7 = (
    "7 5º FEIRA |22/OUT/2026 - EDIÇÃO N° 12.254 PODER EXECUTIVO ESTADUAL Protocolo 143693/2026 DECRETO Nº 15.467 "
    "Declara de utilidade pública para fins de desapropriação administrativa pela Companhia de Saneamento do "
    "Paraná a área de terra que especifica. O GOVERNADOR DO ESTADO DO PARANÁ, no uso das atribuições que lhe são "
    "conferidas pelos incisos V e VI do art. 87 Constituição Estadual, DECRETA: Art. 1º Declara de utilidade "
    "pública para fins de desapropriação administrativa pela Companhia de Saneamento do Paraná - SANEPAR, a área "
    "de terra abaixo descrita. III - Município/Comarca: CURIÚVA /PR. IV - Certidão de Registro: Matrícula nº "
    "19.446 - 1º Ofício. Art. 2º A área destina-se à implantação do Reservatório de Água - RAP Bom Retiro. "
    "VI - Área Atingida: 202,10 m². Art. 5º A SANEPAR poderá invocar em juízo a urgência a que se refere o art. "
    "15 do Decreto-Lei nº 3.365, de 1.941."
)

PAGINA_ENTIDADE = (
    "Concede o Título de Utilidade Pública à Casa de Apoio Feminina Recomeçar, com sede no Município de Foz do "
    "Iguaçu. Art. 1º Concede o Título de Utilidade Pública à Casa de Apoio Feminina Recomeçar - CAFER."
)


def hit(_id, pagina, conteudo, diario="EX"):
    did = int(_id.split("_")[0])
    return {"_id": _id, "diario": diario, "suplemento": "Edição 12254",
            "_source": {"conteudo": conteudo, "data": "2026-10-22", "pagina": pagina, "paginas": 87,
                        "diario_id": did, "tipo_edicao": 3}}


RESPOSTA = {"hits": {"total": 4, "hits": [
    hit("17396_7", 7, PAGINA_7), hit("17396_4", 4, PAGINA_4),
    hit("17396_9", 9, PAGINA_ENTIDADE), hit("17400_2", 2, PAGINA_4, diario="CO"),
]}}


def test_url_de_busca_no_formato_do_portal():
    u = cp.url_busca('"utilidade pública" desapropriação', 0, date(2026, 10, 20), date(2026, 10, 22))
    assert u.startswith("https://dioe.pr.gov.br/busca/busca/buscar/query/0/di:2026-10-20/df:2026-10-22/?1=1&q=")


def test_decreto_campo_mourao():
    atos = cp.hit_para_atos(hit("17396_4", 4, PAGINA_4))
    assert len(atos) == 1
    a = atos[0]
    assert a.numero_ato == "15.460" and a.esfera == "Estadual" and a.uf == "PR"
    assert a.municipio == "Campo Mourão" and "28.082" in a.matriculas
    assert a.url_fonte == "https://dioe.pr.gov.br/portal/edicoes/download/17396/4"
    assert a.edicao == "12254"


def test_decreto_sanepar_com_urgencia():
    a = cp.hit_para_atos(hit("17396_7", 7, PAGINA_7))[0]
    assert a.numero_ato == "15.467"
    assert {"urgencia", "matricula"} <= set(a.tags) and "19.446" in a.matriculas


class Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return RESPOSTA


class Sessao:
    headers = {}

    def get(self, url, timeout=None):
        return Resp()


def test_ponta_a_ponta(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "mesclar_e_salvar", lambda a: common.mesclar_e_salvar(a, tmp_path / "atos.json"))
    monkeypatch.setattr(cp, "registrar_execucao", lambda f, e: common.registrar_execucao(f, e, tmp_path / "meta.json"))
    monkeypatch.setattr(cp, "criar_sessao", Sessao)
    monkeypatch.setattr(common.Limitador, "aguardar", lambda self: None)
    assert cp.executar(3, date(2026, 10, 22)) == 0
    atos = json.loads((tmp_path / "atos.json").read_text(encoding="utf-8"))
    # Entidade descartada; caderno "CO" ignorado; mesmos hits em 4 consultas não duplicam.
    assert sorted(a["numero_ato"] for a in atos) == ["15.460", "15.467"]
