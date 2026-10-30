"""
Testes do filtro jurídico. Os textos abaixo são SINTÉTICOS (redigidos para o
teste, sem correspondência com atos reais), reproduzindo a estrutura típica
de decretos expropriatórios e dos ruídos mais comuns em Diários Oficiais.

Rodar: python -m pytest -q
"""
from pipeline.filtro_juridico import dobrar, extrair_atos

DUP_MUNICIPAL = """
PREFEITURA MUNICIPAL DE CIDADE EXEMPLO
DECRETO Nº 1.234/2026
Declara de utilidade pública, para fins de desapro-
priação, o imóvel que especifica e dá outras providências.
O PREFEITO MUNICIPAL, no uso de suas atribuições e com fundamento no Decreto-Lei nº 3.365,
de 21 de junho de 1941, DECRETA:
Art. 1º Fica declarado de utilidade pública, para fins de desapropriação, de forma amigável
ou judicial, o imóvel objeto da matrícula nº 45.678 do 1º Registro de Imóveis, com área de
1.250,00 m², destinado à implantação de Centro Municipal de Educação Infantil (CMEI).
Art. 2º Fica declarada a urgência para fins de imissão provisória na posse.
Art. 3º Este Decreto entra em vigor na data de sua publicação, revogam-se as disposições em contrário.
"""

ENTIDADE = """
LEI Nº 4.567/2026
Declara de utilidade pública a Associação de Moradores do Bairro Exemplo, entidade sem fins
lucrativos, e dá outras providências. Art. 1º Fica declarada de utilidade pública municipal a
Associação de Moradores do Bairro Exemplo, com sede nesta cidade.
"""

LICITACAO = """
AVISO DE LICITAÇÃO PREGÃO ELETRÔNICO Nº 55/2026. Objeto: contratação de empresa para prestação
de serviços de avaliação de imóveis para fins de desapropriação, conforme termo de referência.
Abertura das propostas em 10/11/2026.
"""

JUDICIAL = """
EDITAL DE CITAÇÃO. Autos nº 0001234-56.2026.8.16.0001. Ação de Desapropriação. A Vara da Fazenda
Pública cita os réus incertos para contestarem a ação de desapropriação do imóvel declarado de
utilidade pública pelo Decreto nº 900/2025, no prazo legal. Intime-se.
"""

SERVIDAO = """
DECRETO Nº 77, DE 3 DE OUTUBRO DE 2026. Declara de utilidade pública, para fins de instituição de
servidão administrativa, a faixa de terra necessária à passagem de rede coletora de esgoto
sanitário, com área de 0,85 ha, situada nas coordenadas UTM SIRGAS 2000 indicadas no anexo.
"""

INTERESSE_SOCIAL = """
DECRETO MUNICIPAL Nº 2.001/2026. Declara de interesse social, para fins de desapropriação, nos
termos da Lei nº 4.132, de 10 de setembro de 1962, a área de terras urbanas destinada a programa
habitacional e regularização fundiária, constituída pelos lotes 1 a 10 da quadra 5.
"""

DOIS_ATOS = DUP_MUNICIPAL + "\n" + "Portaria de férias de servidores. " * 120 + "\n" + SERVIDAO

REFERENCIA_ANTERIOR = """
TERMO DE ACORDO. O imóvel declarado de utilidade pública pelo Decreto nº 1.100/2024 teve o valor
da indenização ajustado entre as partes, nos termos do processo administrativo.
"""


def test_dobrar_preserva_comprimento():
    t = "Desapropriação, Servidão, nº 1.234, área m² ÇÃO"
    assert len(dobrar(t)) == len(t)
    assert "desapropriacao" in dobrar(t)


def test_dup_municipal_completo():
    atos = extrair_atos(DUP_MUNICIPAL)
    assert len(atos) == 1
    a = atos[0]
    assert a.numero_ato == "1.234/2026"
    assert a.tipo_ato == "Decreto"
    assert "45.678" in a.matriculas
    assert any("1.250,00" in x for x in a.areas)
    assert {"utilidade_publica", "urgencia", "matricula"} <= set(a.tags)
    assert "revogacao" not in a.tags  # "revogam-se as disposições" é cláusula de estilo
    assert "Educação" in a.finalidades
    assert a.confianca == "alta"
    assert 300 <= len(a.resumo_contexto) <= 510


def test_rejeita_entidade_de_utilidade_publica():
    assert extrair_atos(ENTIDADE) == []


def test_rejeita_licitacao():
    assert extrair_atos(LICITACAO) == []


def test_rejeita_ato_judicial():
    assert extrair_atos(JUDICIAL) == []


def test_rejeita_referencia_a_decreto_anterior():
    assert extrair_atos(REFERENCIA_ANTERIOR) == []


def test_servidao_administrativa():
    atos = extrair_atos(SERVIDAO)
    assert len(atos) == 1
    a = atos[0]
    assert a.numero_ato == "77"
    assert {"servidao", "coordenadas"} <= set(a.tags)
    assert "Saneamento" in a.finalidades
    assert any("ha" in x for x in a.areas)


def test_interesse_social_nao_confunde_numero_da_lei():
    atos = extrair_atos(INTERESSE_SOCIAL)
    assert len(atos) == 1
    assert atos[0].numero_ato == "2.001/2026"
    assert "interesse_social" in atos[0].tags
    assert "Habitação/Regularização" in atos[0].finalidades


def test_dois_atos_no_mesmo_diario():
    atos = extrair_atos(DOIS_ATOS)
    numeros = sorted(a.numero_ato for a in atos)
    assert numeros == ["1.234/2026", "77"]


REVOGACAO = """
DECRETO N° 3.010, DE 15 DE OUTUBRO DE 2026. Revoga o Decreto nº 2.500/2024, que declarou de utilidade
pública, para fins de desapropriação, o imóvel da matrícula 9.876. Art. 1º Fica revogado o Decreto
nº 2.500/2024, que declara de utilidade pública, para fins de desapropriação, a área destinada à praça.
"""

DUP_COM_LICITACAO_DISTANTE = (
    "AVISO DE LICITAÇÃO PREGÃO Nº 12/2026 objeto aquisição de material de expediente. "
    + "Texto administrativo diverso sobre nomeações e férias. " * 15
    + DUP_MUNICIPAL
)


def test_revogacao_recebe_etiqueta():
    atos = extrair_atos(REVOGACAO)
    assert len(atos) == 1
    assert atos[0].numero_ato == "3.010"
    assert "revogacao" in atos[0].tags


def test_dup_nao_e_contaminado_por_licitacao_distante():
    atos = extrair_atos(DUP_COM_LICITACAO_DISTANTE)
    assert [a.numero_ato for a in atos] == ["1.234/2026"]
    assert atos[0].resumo_contexto.startswith("DECRETO Nº 1.234/2026")


# --- Regressões extraídas de diários REAIS (Querido Diário, set-out/2026) ----
def test_numero_com_palavra_intermediaria():
    t = ("DECRETO RIO Nº 58838 DE 26 DE OUTUBRO DE 2026 Declara de utilidade pública, para fins de "
         "desapropriação por hasta pública, o imóvel que menciona.")
    assert extrair_atos(t)[0].numero_ato == "58838"


def test_numero_com_simbolo_de_fonte_privada():
    t = ("DECRETO N 13.603, DE 06 DE OUTUBRO DE 2026. Declara de UTILIDADE PÚBLICA, para fins de "
         "instituição de Servidão Administrativa, a faixa de terra destinada à rede de drenagem pluvial.")
    assert extrair_atos(t)[0].numero_ato == "13.603"


def test_numero_com_erro_de_ocr_n2():
    t = ('DECRETO N2 4.780, DE 19 DE OUTUBRO DE 2.026 Dispõe sobre: "Declara de utilidade pública, para '
         'fins de desapropriação amigável ou judicial, o imóvel que especifica".')
    assert extrair_atos(t)[0].numero_ato == "4.780"


def test_lei_citada_em_minusculas_nao_vira_numero():
    t = ("Conforme a Lei nº 7.186/2006, fica declarado de utilidade pública, para fins de desapropriação, "
         "o imóvel da matrícula 1.234.")
    assert extrair_atos(t)[0].numero_ato is None


def test_chave_numero():
    from pipeline.filtro_juridico import chave_numero
    assert chave_numero("4.830") == chave_numero("4830/2026") == chave_numero("04830") == "4830"
    assert chave_numero("s/nº, de 20 de outubro de 2026") != ""


def test_rejeita_conteudo_programatico_de_concurso():
    t = ("Desapropriação. Conceito, fundamentos e requisitos. O procedimento e o processo desapropriatório. "
         "A declaração da utilidade pública, a imissão provisória e a justa indenização.")
    assert extrair_atos(t) == []
