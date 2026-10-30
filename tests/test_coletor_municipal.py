"""
Teste ponta a ponta do coletor municipal com a API do Querido Diário SIMULADA
(sem rede). Valida paginação, agrupamento por diário, uso do texto integral,
esquema do JSON, deduplicação entre execuções e meta.json.
"""
import json
from datetime import date

import pytest

from pipeline import coletor_municipal as cm
from pipeline import common
from tests.test_filtro_juridico import DUP_MUNICIPAL, LICITACAO

CAMPOS_OBRIGATORIOS = {"id", "data_publicacao", "esfera", "uf", "municipio", "orgao_emissor",
                       "numero_ato", "resumo_contexto", "url_fonte"}


class RespostaFalsa:
    def __init__(self, status=200, dados=None, texto=""):
        self.status_code = status
        self._dados = dados
        self._texto = texto.encode("utf-8")

    def json(self):
        return self._dados

    def iter_content(self, chunk_size=1):
        yield self._texto

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


DIARIO_OK = {
    "territory_id": "4106902", "territory_name": "Cidade Exemplo", "state_code": "PR",
    "date": "2026-10-20", "edition": "1500", "is_extra_edition": False,
    "url": "https://exemplo.invalid/diario-ok.pdf", "txt_url": "https://exemplo.invalid/diario-ok.txt",
    "excerpts": ["Declara de utilidade pública, para fins de desapropriação, o imóvel ..."],
}
DIARIO_RUIDO = {
    "territory_id": "3550308", "territory_name": "Outra Cidade", "state_code": "SP",
    "date": "2026-10-21", "edition": "88", "is_extra_edition": False,
    "url": "https://exemplo.invalid/diario-ruido.pdf", "txt_url": "https://exemplo.invalid/diario-ruido.txt",
    "excerpts": [LICITACAO],
}


class SessaoFalsa:
    def __init__(self):
        self.headers = {}
        self.chamadas_api = 0

    def get(self, url, params=None, timeout=None, stream=False):
        if url.endswith("/gazettes"):
            self.chamadas_api += 1
            offset = int(params.get("offset", 0))
            pagina = [DIARIO_OK, DIARIO_RUIDO] if offset == 0 else []
            return RespostaFalsa(dados={"total_gazettes": 2, "gazettes": pagina})
        if url.endswith("diario-ok.txt"):
            return RespostaFalsa(texto="Cabeçalho do diário. " + DUP_MUNICIPAL)
        if url.endswith("diario-ruido.txt"):
            return RespostaFalsa(texto=LICITACAO)
        return RespostaFalsa(status=404)


@pytest.fixture()
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ARQ_ATOS", tmp_path / "atos.json")
    monkeypatch.setattr(common, "ARQ_META", tmp_path / "meta.json")
    monkeypatch.setattr(cm, "criar_sessao", lambda: SessaoFalsa())
    monkeypatch.setattr(cm.time, "sleep", lambda s: None)
    monkeypatch.setattr(common.Limitador, "aguardar", lambda self: None)
    # mesclar_e_salvar/registrar_execucao usam os caminhos padrão do módulo common
    monkeypatch.setattr(cm, "mesclar_e_salvar", lambda atos: common.mesclar_e_salvar(atos, tmp_path / "atos.json"))
    monkeypatch.setattr(cm, "registrar_execucao", lambda f, e: common.registrar_execucao(f, e, tmp_path / "meta.json"))
    return tmp_path


def test_execucao_completa_e_idempotente(ambiente):
    codigo = cm.executar(dias=10, fim=date(2026, 10, 25), ufs=set(), territorios=[], usar_texto=True)
    assert codigo == 0

    atos = json.loads((ambiente / "atos.json").read_text(encoding="utf-8"))
    assert len(atos) == 1, "licitação deve ser descartada; DUP deve entrar"
    a = atos[0]
    assert CAMPOS_OBRIGATORIOS <= set(a)
    assert a["esfera"] == "Municipal" and a["uf"] == "PR" and a["municipio"] == "Cidade Exemplo"
    assert a["numero_ato"] == "1.234/2026"
    assert a["data_publicacao"] == "2026-10-20"
    assert a["url_fonte"].endswith(".pdf")

    meta = json.loads((ambiente / "meta.json").read_text(encoding="utf-8"))
    assert meta["fontes"]["querido_diario"]["novos"] == 1

    # Segunda execução: nada novo (deduplicação por hash).
    cm.executar(dias=10, fim=date(2026, 10, 25), ufs=set(), territorios=[], usar_texto=True)
    assert len(json.loads((ambiente / "atos.json").read_text(encoding="utf-8"))) == 1
    meta = json.loads((ambiente / "meta.json").read_text(encoding="utf-8"))
    assert meta["fontes"]["querido_diario"]["novos"] == 0


def test_filtro_por_uf(ambiente):
    cm.executar(dias=10, fim=date(2026, 10, 25), ufs={"SP"}, territorios=[], usar_texto=True)
    atos = json.loads((ambiente / "atos.json").read_text(encoding="utf-8"))
    assert atos == []
