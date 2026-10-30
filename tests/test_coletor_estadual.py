"""Testes do coletor estadual com feed RSS SINTÉTICO no formato do Google News."""
import json
from datetime import date


from pipeline import coletor_estadual as ce
from pipeline import common

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>Governo do Paraná declara de utilidade pública áreas para duplicação da PR-445 - Agência Estadual de Notícias</title>
<link>https://news.google.com/rss/articles/AAA1</link><pubDate>Tue, 27 Oct 2026 13:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Decreto nº 11.222/2026 declara de utilidade pública, para fins de desapropriação, áreas no Município de Londrina, Estado do Paraná&lt;/a&gt;</description>
<source url="https://www.aen.pr.gov.br">Agência Estadual de Notícias</source></item>
<item><title>Justiça mantém ação de desapropriação de área em Curitiba</title>
<link>https://news.google.com/rss/articles/AAA2</link><pubDate>Tue, 27 Oct 2026 14:00:00 GMT</pubDate>
<description>Tribunal decide sobre indenização</description><source url="https://www.tjpr.jus.br">TJPR</source></item>
<item><title>Prefeitura de Exemplo declara utilidade pública de terreno para nova escola</title>
<link>https://news.google.com/rss/articles/AAA3</link><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate>
<description>Decreto prevê desapropriação de imóvel</description><source url="https://www.exemplo.pr.gov.br">Prefeitura de Exemplo</source></item>
<item><title>Câmara declara de utilidade pública a Associação Beneficente Exemplo</title>
<link>https://news.google.com/rss/articles/AAA4</link><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate>
<description>Lei reconhece entidade sem fins lucrativos</description><source url="https://www.cm.pr.gov.br">Câmara</source></item>
<item><title>Blog comenta desapropriação de área</title>
<link>https://news.google.com/rss/articles/AAA5</link><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate>
<description>decreto declara área</description><source url="https://www.blog.com.br">Blog</source></item>
</channel></rss>"""


def test_filtro_de_noticias():
    itens = ce.parse_rss(RSS)
    assert len(itens) == 5
    atos = [ce.avaliar_noticia(i, "PR") for i in itens]
    validos = [a for a in atos if a]
    assert len(validos) == 2
    est, mun = validos
    assert est.esfera == "Estadual" and est.uf == "PR" and est.numero_ato == "11.222/2026"
    assert "fonte_secundaria" in est.tags and "Viário/Rodovia" in est.finalidades
    assert est.data_publicacao == "2026-10-27"
    assert mun.esfera == "Municipal" and "Educação" in mun.finalidades


def test_consulta_restrita_a_gov_br():
    q = ce.construir_consulta("PR", 3)
    assert "site:gov.br" in q and '"Paraná"' in q and "when:3d" in q


class Resp:
    status_code = 200
    text = RSS

    def raise_for_status(self):
        pass


class Sessao:
    headers = {}

    def get(self, url, timeout=None):
        return Resp()


def test_ponta_a_ponta(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "mesclar_e_salvar", lambda a: common.mesclar_e_salvar(a, tmp_path / "atos.json"))
    monkeypatch.setattr(ce, "registrar_execucao", lambda f, e: common.registrar_execucao(f, e, tmp_path / "meta.json"))
    monkeypatch.setattr(ce, "criar_sessao", Sessao)
    monkeypatch.setattr(ce, "hoje_br", lambda: date(2026, 10, 28))
    monkeypatch.setattr(common.Limitador, "aguardar", lambda self: None)
    assert ce.executar(3, ["PR", "SC"]) == 0
    atos = json.loads((tmp_path / "atos.json").read_text(encoding="utf-8"))
    assert len(atos) == 2  # mesmo link em duas UFs não duplica
