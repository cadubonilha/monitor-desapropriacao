"""
Filtro jurídico por Regex: identifica atos declaratórios de desapropriação
(fase administrativa) em texto bruto de Diários Oficiais.

Base normativa que orienta os padrões:
  * Decreto-Lei 3.365/1941: declaração de utilidade pública (DUP), inclusive
    para instituição de servidão administrativa; urgência para imissão provisória.
  * Lei 4.132/1962: desapropriação por interesse social.
  * Lei 8.629/1993 e LC 76/1993: interesse social para fins de reforma agrária.

Estratégia (sem IA, apenas código determinístico):
  1. Limpeza do texto (hifenização de quebra de linha, espaços).
  2. "Dobra" do texto para minúsculas sem acento com MESMO comprimento, o que
     permite casar padrões no texto dobrado e recortar o trecho no original.
  3. Âncoras positivas: verbo declaratório, finalidade expropriatória, base legal.
  4. Validação contextual de cada âncora em janela local: exige objeto
     (desapropriação/servidão/imóvel) e rejeita ruído típico:
       - entidades declaradas "de utilidade pública" (associações, APAEs etc.);
       - referência a decreto anterior ("declarado ... pelo Decreto nº");
       - licitações e extratos (pregão, avaliação de imóveis, contratos);
       - atos judiciais (autos, vara, citação, sentença, precatório).
  5. Agrupamento de âncoras por número do ato, extração de número, tipo,
     matrículas, áreas, finalidades e etiquetas.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Normalização
# ---------------------------------------------------------------------------
_RE_HIFEN_QUEBRA = re.compile(r"(\w)-[ \t]*\r?\n[ \t]*(\w)")
_RE_ESPACOS = re.compile(r"\s+")
_RE_USO_PRIVADO = re.compile("[\ue000-\uf8ff]")


def limpar_texto(texto: str) -> str:
    """Desfaz hifenização de fim de linha, decodifica entidades e colapsa espaços."""
    texto = html.unescape(texto or "")
    texto = texto.replace("­", "")  # hífen condicional invisível
    # PDFs com fonte Symbol trazem "°" e "º" na área de uso privado (ex.: U+F0B0).
    texto = texto.replace("\uf0b0", "°").replace("\uf0ba", "º")
    texto = _RE_USO_PRIVADO.sub(" ", texto)
    texto = _RE_HIFEN_QUEBRA.sub(r"\1\2", texto)
    return _RE_ESPACOS.sub(" ", texto).strip()


def dobrar(texto: str) -> str:
    """
    Minúsculas sem acento preservando o comprimento: cada caractere de entrada
    gera exatamente um caractere de saída. Assim, posições de matches no texto
    dobrado valem também para o texto original.
    """
    saida = []
    for c in texto:
        base = unicodedata.normalize("NFD", c)[0]
        minus = base.lower()
        saida.append(minus if len(minus) == 1 else base)
    return "".join(saida)


# ---------------------------------------------------------------------------
# Padrões (aplicados sobre o texto dobrado)
# ---------------------------------------------------------------------------
_N = r"(?:n\.?\s*[o°º]s?\.?|n[o°º]s?\.?|n\.|numero)"

RE_DECLARACAO = re.compile(
    # "declara", "fica declarado", "ficam declaradas", "declara como de",
    # "declara a área ... de utilidade pública" (até 8 palavras entre os termos).
    # Não casa "declarou" (pretérito), típico de referência a ato anterior.
    r"\bdeclara(?:d[oa]s?|m|ndo)?\b(?:\W+\w+){0,8}?\W+(?:como\s+)?(?:de\s+)?"
    r"(?P<tipo>utilidade\s+publica|necessidade\s+publica|interesse\s+social)"
)

RE_FINS = re.compile(
    r"\b(?:para|a)\s+(?:os\s+)?(?:fins?|efeitos?)\s+de\s+"
    r"(?P<fim>desapropria\w*|expropria\w*|(?:instituicao|constituicao|implantacao)\s+de\s+servidao\w*"
    r"|servidao\w*)"
    r"|\ba\s+fim\s+de\s+(?:ser(?:em)?\s+)?(?:desapropriad\w*|expropriad\w*)"
)

RE_BASE_LEGAL = re.compile(
    r"\bdecreto[\s\-]*lei\s*(?:federal\s*)?(?:" + _N + r"\s*)?3\.?365\b"
    r"|\blei\s*(?:federal\s*)?(?:" + _N + r"\s*)?4\.?132\b"
    r"|\blei\s*(?:federal\s*)?(?:" + _N + r"\s*)?8\.?629\b"
    r"|\blei\s+complementar\s*(?:federal\s*)?(?:" + _N + r"\s*)?76\b"
)

RE_OBJETO = re.compile(
    r"\b(?:desapropria\w*|expropria\w*|servidao\w*|imissao\s+(?:provisoria\s+)?(?:na|de)\s+posse)"
)

RE_IMOVEL = re.compile(
    r"\b(?:imove(?:l|is)|areas?|terrenos?|lotes?|glebas?|matricula\w*|faixa\s+de\s+terra"
    r"|propriedades?|predios?|benfeitorias?|quadra)\b"
)

# --- Exclusões --------------------------------------------------------------
RE_EXC_ENTIDADE = re.compile(
    r"utilidade\s+publica\s+(?:municipal\s+|estadual\s+|federal\s+)?(?:a|o|as|os|da|do)\s+"
    r"(?:associacao|entidade|instituicao|fundacao|clube|liga|sociedade|igreja|centro|grupo|lar"
    r"|apae|asilo|creche|uniao|federacao|conselho|organizacao|oscip|cooperativa|instituto"
    r"|movimento|comunidade|rede|academia|banda|coral|corpo\s+de\s+bombeiros\s+voluntarios"
    r"|hospital\s+beneficente|santa\s+casa|ong)\b"
    r"|entidade\s+sem\s+fins\s+lucrativos|sem\s+fins\s+economicos"
)

RE_REFERENCIA_ANTERIOR = re.compile(
    # "declarado de utilidade pública pelo Decreto nº 123", "conforme Decreto ..."
    r"^.{0,120}?\b(?:pelo|atraves\s+do|por\s+meio\s+do|conforme(?:\s+o)?|nos\s+termos\s+do|mediante\s+o)\s+"
    r"decreto(?![\s\-]*lei)"
)

RE_EXC_LICITACAO = re.compile(
    r"\b(?:pregao|tomada\s+de\s+precos|concorrencia\s+(?:publica|eletronica|" + _N + r")"
    r"|edital\s+de\s+licitacao|aviso\s+de\s+licitacao|chamamento\s+publico|dispensa\s+de\s+licitacao"
    r"|inexigibilidade|ata\s+de\s+registro\s+de\s+precos|homologacao\s+e\s+adjudicacao"
    r"|extrato\s+(?:do|de)\s+contrato|termo\s+aditivo|ordem\s+de\s+servico|empresa\s+contratada"
    r"|laudos?\s+de\s+avaliacao\s+(?:dos|de)\s+imoveis|servicos\s+de\s+avaliacao)\b"
)

RE_EXC_JUDICIAL = re.compile(
    r"\b(?:autos\s+(?:de\s+processo\s+)?" + _N + r"|vara\s+(?:civel|da\s+fazenda|federal|unica|de\s+fazenda)"
    r"|juiz(?:o|a)?\s+de\s+direito|sentenca|cite-se|intime-se|edital\s+de\s+citacao"
    r"|mandado\s+de\s+imissao|acao\s+de\s+desapropriacao\s+" + _N + r"|processo\s+judicial"
    r"|cumprimento\s+de\s+sentenca|precatorio|requisicao\s+de\s+pequeno\s+valor|transitad\w+\s+em\s+julgado)"
)

# --- Identificação do ato ---------------------------------------------------
# Variantes de "nº" vistas em diários reais: "N 13.603", e "N2" (OCR que lê "º" como "2").
_N_NUM = r"(?:" + _N + r"|n2(?=\s*\d)|n(?=\s+\d))"
RE_NUMERO = re.compile(
    # Admite uma palavra entre o tipo e o número, como em "DECRETO RIO Nº 58838" ou "DECRETO GP Nº 12".
    r"\b(?P<tipo>decreto(?![\s\-]*lei)(?:\s+(?:municipal|estadual|executivo|governamental|legislativo"
    r"|regulamentar|numerado|rio|gp|pm\w{0,3}|gab\w{0,6}|sec\w{0,6}))?|resolucao(?:\s+autorizativa)?|portaria"
    r"|lei(?:\s+(?:municipal|ordinaria))?)"
    r"\s*(?:" + _N_NUM + r")?\s*[:\-]?\s*"
    r"(?P<num>\d{1,3}(?:\.\d{3})+|\d{1,6})(?:\s*[\/\-]\s*(?P<ano>\d{4}|\d{2})\b)?"
)

# Números de leis e decretos-leis frequentemente citados como fundamento;
# nunca representam o número do próprio ato.
NUMEROS_REFERENCIA = {"3365", "4132", "8629", "76", "8987", "10257", "14133", "6766", "9514",
                      "10406", "8666", "13465", "12651", "9785", "4504"}

RE_MATRICULA = re.compile(
    r"\bmatricula\w*\s*(?:imobiliaria\s*)?(?:sob\s+(?:o\s+)?)?(?:" + _N + r"\s*)?[:\-]?\s*"
    r"(?P<lista>(?:\d{1,3}(?:\.\d{3})+|\d{3,7})(?:\s*(?:,|e)\s*(?:\d{1,3}(?:\.\d{3})+|\d{3,7}))*)"
)
RE_NUM_SIMPLES = re.compile(r"\d{1,3}(?:\.\d{3})+|\d{3,7}")

RE_AREA = re.compile(
    r"(?P<valor>\d{1,3}(?:\.\d{3})+(?:,\d{1,4})?|\d+(?:,\d{1,4})?)\s*"
    r"(?P<un>m²|m2\b|metros\s+quadrados|ha\b|hectares?\b|alqueires?\b)"
)

TAGS = {
    "utilidade_publica": re.compile(r"utilidade\s+publica"),
    "necessidade_publica": re.compile(r"necessidade\s+publica"),
    "interesse_social": re.compile(r"interesse\s+social"),
    "servidao": re.compile(r"servidao"),
    "urgencia": re.compile(r"\burgencia\b"),
    "reforma_agraria": re.compile(r"reforma\s+agraria|\b8\.?629\b"),
    "revogacao": re.compile(
        r"\brevoga(?:-se)?\s+(?:o|a)\s+decreto|fica\w*\s+revogad[oa]s?\s+o\s+decreto"
        r"|torna\w*\s+sem\s+efeito|caducidade"
    ),
    "retificacao": re.compile(r"\bretifica\w*|\baltera\s+(?:o|a|os|as)\s+(?:art\w*|decreto|anexo)"),
    "matricula": re.compile(r"\bmatricula"),
    "coordenadas": re.compile(r"\b(?:utm|sirgas|datum|latitude|longitude|coordenadas)\b"),
}

FINALIDADES = {
    "Viário/Rodovia": re.compile(
        r"\b(?:rodovias?|estradas?|viadutos?|pontes?|trincheira|duplicacao|anel\s+viario|contorno\s+viario"
        r"|sistema\s+viario|alargamento|prolongamento|abertura\s+de\s+(?:rua|via|avenida)|binario|trevo)\b"
    ),
    "Saneamento": re.compile(
        r"\b(?:saneamento|esgot\w*|abastecimento\s+de\s+agua|estacao\s+de\s+tratamento|ete|eta"
        r"|reservatorio|adutora|emissario|elevatoria|captacao\s+de\s+agua)\b"
    ),
    "Drenagem/Hídrico": re.compile(
        r"\b(?:drenagem|macrodrenagem|galeria\s+pluvial|canal|canalizacao|barragem|piscinao|dique"
        r"|bacia\s+de\s+(?:detencao|contencao|amortecimento|retencao)|contencao\s+de\s+cheias)\b"
    ),
    "Energia": re.compile(
        r"\b(?:linhas?\s+de\s+transmissao|subestacao|energia\s+eletrica|linhas?\s+de\s+distribuicao"
        r"|usina|aerogeradores?|gasoduto|oleoduto|parque\s+eolico)\b"
    ),
    "Educação": re.compile(r"\b(?:escolas?|creches?|cmei|unidade\s+escolar|centro\s+(?:municipal\s+)?de\s+educacao)\b"),
    "Saúde": re.compile(r"\b(?:unidade\s+(?:basica\s+)?de\s+saude|ubs|hospital|posto\s+de\s+saude|upa)\b"),
    "Lazer/Ambiental": re.compile(
        r"\b(?:parque(?!\s+(?:industrial|eolico|tecnologico))|praca|area\s+verde|bosque|preservacao\s+ambiental"
        r"|unidade\s+de\s+conservacao|recreacao|lazer)\b"
    ),
    "Habitação/Regularização": re.compile(
        r"\b(?:habitacao|habitacional|moradias?|regularizacao\s+fundiaria|reurb|conjunto\s+habitacional)\b"
    ),
    "Equipamento público": re.compile(
        r"\b(?:paco\s+municipal|cemiterio|terminal|rodoviaria|aterro|centro\s+administrativo|delegacia"
        r"|quartel|ginasio|centro\s+esportivo|sede\s+(?:da|do)\s+(?:secretaria|prefeitura|camara))\b"
    ),
    "Mobilidade/Transporte": re.compile(r"\b(?:metro|brt|corredor\s+de\s+onibus|ferrovia\w*|aeroporto|aerodromo)\b"),
    "Industrial/Econômico": re.compile(r"\b(?:distrito\s+industrial|parque\s+(?:industrial|tecnologico)|polo\s+\w+)\b"),
}

# Tamanhos de janela (caracteres) usados na validação contextual.
JANELA_ANTES = 500
JANELA_DEPOIS = 700
DIST_AGRUPAMENTO = 1500
BUSCA_CABECALHO = 3000


# ---------------------------------------------------------------------------
# Estruturas de saída
# ---------------------------------------------------------------------------
@dataclass
class AtoExtraido:
    """Resultado bruto da extração, antes de receber metadados da fonte."""

    numero_ato: str | None
    tipo_ato: str
    resumo_contexto: str
    tags: list[str] = field(default_factory=list)
    finalidades: list[str] = field(default_factory=list)
    matriculas: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    score: int = 0
    confianca: str = "baixa"
    posicao: int = 0
    assinatura: str = ""  # trecho normalizado usado no hash quando não há número


@dataclass
class _Ancora:
    pos: int
    tipo: str  # "declaracao" | "fins" | "base"
    pontos: int
    tags: set = field(default_factory=set)
    cab_pos: int | None = None
    numero: str | None = None
    tipo_ato: str = "Não identificado"


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------
def chave_numero(numero: str | None) -> str:
    """Forma canônica do número para deduplicação: "4.830", "4830/2026" e "04830" viram "4830"."""
    if not numero:
        return ""
    base = re.split(r"\s*[/-]\s*(?=\d{2,4}\b)", numero.strip())[0]
    digitos = re.sub(r"\D", "", base).lstrip("0")
    return digitos or numero.strip().lower()


def _janela(texto: str, pos: int, antes: int, depois: int) -> str:
    return texto[max(0, pos - antes): pos + depois]


def _localizar_cabecalho(dob: str, orig: str, pos: int) -> tuple[int | None, str | None, str]:
    """
    Procura, para trás, o cabeçalho do ato ("DECRETO Nº 1.234/2026").
    Preferência: ocorrência em CAIXA ALTA no original (padrão de cabeçalho),
    depois decretos, depois resoluções/portarias, por último leis.
    """
    inicio = max(0, pos - BUSCA_CABECALHO)
    candidatos = []
    for m in RE_NUMERO.finditer(dob, inicio, pos + 200):
        num = m.group("num")
        if num.replace(".", "") in NUMEROS_REFERENCIA:
            continue
        tipo = m.group("tipo").split()[0]
        caixa_alta = orig[m.start(): m.start() + 3].isupper()
        if tipo == "lei" and not caixa_alta:
            continue  # "Lei nº 7.186/2006" em minúsculas é citação, não cabeçalho do ato
        prioridade = {"decreto": 3, "resolucao": 2, "portaria": 2, "lei": 1}.get(tipo, 0)
        numero = num + (f"/{m.group('ano')}" if m.group("ano") else "")
        candidatos.append((caixa_alta, prioridade, m.start(), numero, tipo))
    if not candidatos:
        return None, None, "Não identificado"
    # Antes da âncora tem precedência sobre depois dela.
    antes = [c for c in candidatos if c[2] <= pos] or candidatos
    melhor = max(antes, key=lambda c: (c[0], c[1], c[2]))
    nomes = {"decreto": "Decreto", "resolucao": "Resolução", "portaria": "Portaria", "lei": "Lei"}
    return melhor[2], melhor[3], nomes.get(melhor[4], "Não identificado")


def _resumo(orig: str, ancora: int, cab_pos: int | None, minimo: int = 300, maximo: int = 500) -> str:
    """Recorta 300 a 500 caracteres, começando no cabeçalho quando ele está próximo."""
    no_cabecalho = cab_pos is not None and 0 <= ancora - cab_pos <= 300
    ini = cab_pos if no_cabecalho else max(0, ancora - 160)
    fim = min(len(orig), ini + maximo)
    # Ajusta início (só quando não começa no cabeçalho) e fim para limites de palavra.
    if ini > 0 and not no_cabecalho:
        espaco = orig.find(" ", ini)
        ini = espaco + 1 if 0 <= espaco < ancora else ini
    if fim < len(orig):
        espaco = orig.rfind(" ", ini + minimo, fim)
        fim = espaco if espaco > 0 else fim
    trecho = orig[ini:fim].strip()
    return ("… " if ini > 0 and not no_cabecalho else "") + trecho + (" …" if fim < len(orig) else "")


def _lista_matriculas(dob: str) -> list[str]:
    saida: list[str] = []
    for m in RE_MATRICULA.finditer(dob):
        for n in RE_NUM_SIMPLES.findall(m.group("lista")):
            if n not in saida:
                saida.append(n)
    return saida[:10]


def _lista_areas(dob: str) -> list[str]:
    saida: list[str] = []
    for m in RE_AREA.finditer(dob):
        un = m.group("un")
        un = "m²" if un.startswith(("m", "metros")) else ("ha" if un.startswith("h") else "alqueire(s)")
        valor = f"{m.group('valor')} {un}"
        if valor not in saida:
            saida.append(valor)
    return saida[:5]


def _confianca(score: int) -> str:
    return "alta" if score >= 8 else ("media" if score >= 5 else "baixa")


# ---------------------------------------------------------------------------
# Validação contextual de cada âncora
# ---------------------------------------------------------------------------
def _avaliar_ancora(dob: str, pos: int, fim_match: int, tipo: str) -> _Ancora | None:
    jan = _janela(dob, pos, JANELA_ANTES, JANELA_DEPOIS)
    jan_curta = _janela(dob, pos, 300, 400)
    perto = _janela(dob, pos, 250, 250)

    declarativo = RE_DECLARACAO.search(jan)
    tem_objeto = bool(RE_OBJETO.search(jan))
    objeto_perto = bool(RE_OBJETO.search(perto))
    tem_imovel = bool(RE_IMOVEL.search(jan))

    # 1) Entidade declarada "de utilidade pública" (lei de reconhecimento de associação).
    if RE_EXC_ENTIDADE.search(_janela(dob, pos, 40, 300)) and not objeto_perto:
        return None

    # 2) Referência a decreto anterior logo após a declaração ("... pelo Decreto nº").
    if tipo == "declaracao" and RE_REFERENCIA_ANTERIOR.search(dob[fim_match: fim_match + 160]):
        return None

    # 3) Atos judiciais: rejeição incondicional na janela curta.
    if RE_EXC_JUDICIAL.search(jan_curta):
        return None

    # 4) Licitações e contratos: rejeita, salvo declaração + objeto colados à âncora.
    if RE_EXC_LICITACAO.search(jan_curta) and not (declarativo and objeto_perto):
        return None

    pontos = 0
    if tipo == "declaracao":
        if not (tem_objeto or tem_imovel):
            return None
        pontos += 3
    elif tipo == "fins":
        pontos += 3
        if not declarativo:
            pontos -= 1  # finalidade sem verbo declaratório: possível fase posterior
    else:  # base legal isolada só vale se houver declaração por perto
        if not (declarativo and tem_objeto):
            return None
        pontos += 2

    if declarativo and tipo != "declaracao":
        pontos += 2
    if RE_FINS.search(jan) and tipo != "fins":
        pontos += 2
    if RE_BASE_LEGAL.search(jan) and tipo != "base":
        pontos += 1
    pontos += int(tem_objeto) + int(tem_imovel)

    anc = _Ancora(pos=pos, tipo=tipo, pontos=pontos)
    if tipo == "fins" and not declarativo:
        anc.tags.add("sem_declaracao_explicita")
    return anc


# ---------------------------------------------------------------------------
# API pública do módulo
# ---------------------------------------------------------------------------
def extrair_atos(texto_bruto: str) -> list[AtoExtraido]:
    """
    Recebe o texto de um Diário (edição completa ou trecho) e devolve a lista
    de atos expropriatórios identificados, já deduplicados dentro do texto.
    """
    orig = limpar_texto(texto_bruto)
    if not orig:
        return []
    dob = dobrar(orig)

    ancoras: list[_Ancora] = []
    for rotulo, regex in (("declaracao", RE_DECLARACAO), ("fins", RE_FINS), ("base", RE_BASE_LEGAL)):
        for m in regex.finditer(dob):
            anc = _avaliar_ancora(dob, m.start(), m.end(), rotulo)
            if anc:
                anc.cab_pos, anc.numero, anc.tipo_ato = _localizar_cabecalho(dob, orig, m.start())
                ancoras.append(anc)
    if not ancoras:
        return []

    # Agrupamento: mesmo número de ato, ou proximidade quando não há número.
    ancoras.sort(key=lambda a: a.pos)
    grupos: list[list[_Ancora]] = []
    for anc in ancoras:
        alvo = None
        for g in grupos:
            ref = g[0]
            if anc.numero and ref.numero and chave_numero(anc.numero) == chave_numero(ref.numero):
                alvo = g
                break
            if (not anc.numero or not ref.numero or chave_numero(anc.numero) == chave_numero(ref.numero)) and \
                    anc.pos - g[-1].pos <= DIST_AGRUPAMENTO:
                alvo = g
                break
        if alvo is None:
            grupos.append([anc])
        else:
            alvo.append(anc)

    atos: list[AtoExtraido] = []
    for g in grupos:
        principal = next((a for a in g if a.tipo == "declaracao"), g[0])
        numero = next((a.numero for a in g if a.numero), None)
        tipo_ato = next((a.tipo_ato for a in g if a.numero), "Não identificado")
        cab_pos = next((a.cab_pos for a in g if a.numero), None)

        ini_corpo = cab_pos if cab_pos is not None and cab_pos <= principal.pos else max(0, principal.pos - 600)
        corpo = dob[ini_corpo: g[-1].pos + 1200]

        tags = set().union(*(a.tags for a in g))
        tags |= {nome for nome, rx in TAGS.items() if rx.search(corpo)}
        finalidades = sorted(nome for nome, rx in FINALIDADES.items() if rx.search(corpo))
        matriculas = _lista_matriculas(corpo)
        areas = _lista_areas(corpo)

        score = max(a.pontos for a in g)
        score += int(bool(numero)) + int(bool(matriculas)) + int(bool(areas)) + int("coordenadas" in tags)

        assinatura = re.sub(r"[^a-z0-9]", "", dob[principal.pos: principal.pos + 220])[:150]
        atos.append(
            AtoExtraido(
                numero_ato=numero,
                tipo_ato=tipo_ato,
                resumo_contexto=_resumo(orig, principal.pos, cab_pos),
                tags=sorted(tags),
                finalidades=finalidades,
                matriculas=matriculas,
                areas=areas,
                score=score,
                confianca=_confianca(score),
                posicao=principal.pos,
                assinatura=assinatura,
            )
        )
    return atos
