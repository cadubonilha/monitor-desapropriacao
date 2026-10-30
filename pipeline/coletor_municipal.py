"""
Coletor MUNICIPAL: API pública do Querido Diário (Open Knowledge Brasil).

Fluxo:
  1. Executa consultas de busca textual (sintaxe "simple query string") no
     endpoint /gazettes para o período retroativo configurado.
  2. Agrupa os resultados por edição de diário (um diário pode trazer vários atos).
  3. Para cada edição, baixa o texto integral (txt_url) e aplica o filtro
     jurídico sobre o texto completo. Se o texto não estiver disponível, usa
     os trechos (excerpts) devolvidos pela própria API.
  4. Converte em registros padronizados (Ato) e mescla em data/atos.json.

Uso local:
    python -m pipeline.coletor_municipal                 # últimos 5 dias
    python -m pipeline.coletor_municipal --dias 60       # carga retroativa
    python -m pipeline.coletor_municipal --ufs PR,SC     # restringe UFs
    python -m pipeline.coletor_municipal --sem-texto     # só excerpts (mais rápido)
    python -m pipeline.coletor_municipal --modo publicacao --dias 90  # carga histórica

Variáveis de ambiente (todas opcionais):
    DIAS_RETROATIVOS   janela de busca em dias (padrão 5)
    QD_API_BASE        URL base alternativa da API
    QD_UFS             UFs aceitas, separadas por vírgula (vazio = todas)
    QD_TERRITORIOS     códigos IBGE de municípios, separados por vírgula
    QD_MAX_TEXTOS      teto de textos integrais baixados por execução (padrão 200)
    QD_MAX_POR_CONSULTA teto de diários por consulta (padrão 1000)

Observação técnica: o Querido Diário indexa os diários com defasagem em
relação à publicação. Por isso o modo padrão filtra pela data de INDEXAÇÃO
(scraped_since), e a deduplicação por hash impede repetição entre execuções.
Parâmetros conferidos no código-fonte da API (okfn-brasil/querido-diario-api).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta

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
from .filtro_juridico import AtoExtraido, chave_numero, extrair_atos

log = logging.getLogger("monitor.municipal")

FONTE = "querido_diario"

# Verificado ao vivo em 30/10/2026: queridodiario.ok.org.br/api redireciona para
# api.queridodiario.org.br (domínio novo, sem "ok"); o antigo api.queridodiario.ok.org.br
# falhou no TLS. Os demais ficam como reserva.
BASES_API = [
    b for b in (
        os.getenv("QD_API_BASE"),
        "https://api.queridodiario.org.br",
        "https://queridodiario.ok.org.br/api",
        "https://api.queridodiario.ok.org.br",
    ) if b
]
# Hosts alternativos para o texto integral (.txt); o host original vem em txt_url.
HOSTS_TXT_ALTERNATIVOS = [("data.queridodiario.ok.org.br", "data.queridodiario.org.br")]

# Consultas em "simple query string" do OpenSearch (verificado no código-fonte
# da API, versão 0.19): o operador padrão é OU; "+" força E, "|" é OU, "*" é
# prefixo e aspas buscam a frase exata. Cada frase tem variante sem acento
# porque o OCR de diários escaneados frequentemente perde a acentuação.
# As consultas são amplas de propósito; a precisão vem do filtro jurídico local.
_UP = '("utilidade pública" | "utilidade publica")'
_IS = '("interesse social")'
_NP = '("necessidade pública" | "necessidade publica")'
_DESAP = '(desapropria* | expropria*)'
CONSULTAS = [
    f"{_UP} + {_DESAP}",
    f"{_IS} + {_DESAP}",
    f"{_NP} + {_DESAP}",
    '("servidão administrativa" | "servidao administrativa") + decreto',
    '("3.365" | "3365") + decret*',
]

TAMANHO_PAGINA = 100
TAMANHO_TRECHO = 1000   # caracteres por excerpt
TRECHOS_POR_DIARIO = 5
LIMITE_TXT_BYTES = 25 * 1024 * 1024  # diários de capitais podem ser grandes

# Modo de janela temporal:
#   "indexacao"  (padrão) filtra por scraped_since: pega também diários antigos
#                que o Querido Diário só indexou agora (defasagem comum).
#   "publicacao" filtra por published_since/published_until (carga histórica).
MODOS = {"indexacao", "publicacao"}


# ---------------------------------------------------------------------------
# Cliente da API
# ---------------------------------------------------------------------------
class ClienteQueridoDiario:
    """Encapsula paginação, fallback de endereço e rotação de User-Agent."""

    def __init__(self, sessao: requests.Session) -> None:
        self.sessao = sessao
        self.limitador = Limitador(1.1)  # referência pública: 60 req/min
        self.base: str | None = None
        self.erros: list[str] = []

    def _requisitar(self, params: dict) -> dict | None:
        """Tenta a base ativa; em falha, percorre os endereços alternativos."""
        bases = ([self.base] if self.base else []) + [b for b in BASES_API if b != self.base]
        for base in bases:
            for tentativa in range(2):  # segunda tentativa só após 403 (troca de UA)
                self.limitador.aguardar()
                try:
                    r = self.sessao.get(f"{base}/gazettes", params=params, timeout=60)
                except requests.RequestException as e:
                    log.warning("Falha de rede em %s: %s", base, e)
                    break
                if r.status_code == 200:
                    try:
                        dados = r.json()
                    except ValueError:
                        log.warning("Resposta não JSON em %s", base)
                        break
                    self.base = base
                    return dados
                if r.status_code == 403 and tentativa == 0:
                    rotacionar_user_agent(self.sessao)
                    continue
                log.warning("HTTP %s em %s", r.status_code, base)
                break
        return None

    def buscar(self, consulta: str, inicio: date, fim: date, modo: str,
               territorios: list[str], limite: int):
        """Gera os diários (dicts da API) que satisfazem a consulta, paginando por offset."""
        offset = 0
        while offset < limite:
            params = {
                "querystring": consulta,
                "excerpt_size": TAMANHO_TRECHO,
                "number_of_excerpts": TRECHOS_POR_DIARIO,
                "size": TAMANHO_PAGINA,
                "offset": offset,
                "sort_by": "descending_date",
            }
            if modo == "indexacao":
                params["scraped_since"] = f"{inicio.isoformat()}T00:00:00"
            else:
                params["published_since"] = inicio.isoformat()
                params["published_until"] = fim.isoformat()
            if territorios:
                params["territory_ids"] = territorios
            dados = self._requisitar(params)
            if dados is None:
                self.erros.append(f"consulta falhou: {consulta} (offset {offset})")
                return
            diarios = dados.get("gazettes") or []
            total = int(dados.get("total_gazettes") or 0)
            log.info("  offset %d: %d de %d diários", offset, len(diarios), total)
            pagina = len(diarios)
            if modo == "publicacao":
                # Defesa contra mudança silenciosa de parâmetros na API.
                diarios = [d for d in diarios
                           if inicio.isoformat() <= (d.get("date") or "")[:10] <= fim.isoformat()]
            yield from diarios
            offset += pagina
            if not pagina or offset >= total:
                return


# ---------------------------------------------------------------------------
# Texto integral
# ---------------------------------------------------------------------------
def baixar_texto(sessao: requests.Session, url: str) -> str | None:
    """Baixa o .txt do diário com teto de tamanho; devolve None em falha."""
    try:
        with sessao.get(url, timeout=90, stream=True) as r:
            if r.status_code != 200:
                log.debug("TXT indisponível (%s): %s", r.status_code, url)
                return None
            partes, total = [], 0
            for bloco in r.iter_content(chunk_size=262_144):
                partes.append(bloco)
                total += len(bloco)
                if total > LIMITE_TXT_BYTES:
                    log.info("TXT truncado em %d MB: %s", LIMITE_TXT_BYTES // 2**20, url)
                    break
            return b"".join(partes).decode("utf-8", errors="replace")
    except requests.RequestException as e:
        log.debug("Erro ao baixar TXT %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Conversão para o esquema do projeto
# ---------------------------------------------------------------------------
def _chave_diario(d: dict) -> str:
    return d.get("url") or f"{d.get('territory_id')}|{d.get('date')}|{d.get('edition')}"


def converter(diario: dict, extraido: AtoExtraido) -> Ato:
    municipio = (diario.get("territory_name") or "").strip() or None
    uf = (diario.get("state_code") or "").strip().upper()
    data_pub = (diario.get("date") or "")[:10]
    url = diario.get("url") or diario.get("txt_url") or ""

    # Hash: fonte + território + data + número do ato. Sem número, usa a
    # assinatura textual normalizada do trecho declaratório.
    ident = gerar_id(
        FONTE,
        diario.get("territory_id"),
        data_pub,
        chave_numero(extraido.numero_ato) or extraido.assinatura,
    )
    return Ato(
        id=ident,
        data_publicacao=data_pub,
        esfera="Municipal",
        uf=uf,
        municipio=municipio,
        orgao_emissor=f"Prefeitura Municipal de {municipio}" if municipio else "Prefeitura Municipal",
        numero_ato=extraido.numero_ato,
        resumo_contexto=extraido.resumo_contexto,
        url_fonte=url,
        tipo_ato=extraido.tipo_ato,
        tags=extraido.tags,
        finalidades=extraido.finalidades,
        matriculas=extraido.matriculas,
        areas=extraido.areas,
        score=extraido.score,
        confianca=extraido.confianca,
        fonte=FONTE,
        url_texto=diario.get("txt_url"),
        edicao=str(diario.get("edition") or "") or None,
    )


def processar_diario(sessao: requests.Session, diario: dict, trechos: list[str], usar_texto: bool,
                     contador_txt: list[int], max_textos: int) -> list[Ato]:
    """Aplica o filtro jurídico ao texto integral (preferencial) ou aos trechos."""
    extraidos: list[AtoExtraido] = []
    txt_url = diario.get("txt_url")
    if usar_texto and txt_url and contador_txt[0] < max_textos:
        contador_txt[0] += 1
        texto = baixar_texto(sessao, txt_url)
        for antigo, novo in HOSTS_TXT_ALTERNATIVOS:
            if texto is None and antigo in txt_url:
                texto = baixar_texto(sessao, txt_url.replace(antigo, novo))
        if texto:
            extraidos = extrair_atos(texto)
            if extraidos:
                return [converter(diario, e) for e in extraidos]
            # Texto integral baixado, mas sem ato válido: é ruído. Não usar trechos,
            # pois eles são subconjunto do mesmo texto.
            return []
    for t in trechos:
        extraidos.extend(extrair_atos(t))
    # Trechos são fragmentos do mesmo diário: um fragmento sem número costuma ser
    # pedaço de um ato numerado já capturado em outro trecho. Descarta-os nesse caso.
    if any(e.numero_ato for e in extraidos):
        extraidos = [e for e in extraidos if e.numero_ato]
    atos = [converter(diario, e) for e in extraidos]
    # Deduplica atos repetidos entre trechos do mesmo diário.
    unicos: dict[str, Ato] = {}
    for a in atos:
        if a.id not in unicos or a.score > unicos[a.id].score:
            unicos[a.id] = a
    return list(unicos.values())


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def executar(dias: int, fim: date | None, ufs: set[str], territorios: list[str], usar_texto: bool,
             modo: str = "indexacao") -> int:
    fim = fim or hoje_br()
    inicio = fim - timedelta(days=max(dias, 1) - 1)
    max_textos = int(os.getenv("QD_MAX_TEXTOS", "200"))
    max_por_consulta = int(os.getenv("QD_MAX_POR_CONSULTA", "1000"))

    if modo not in MODOS:
        raise ValueError(f"modo inválido: {modo}")
    log.info("Querido Diário: %s a %s (modo %s) | UFs: %s | texto integral: %s",
             inicio, fim, modo, ",".join(sorted(ufs)) or "todas", usar_texto)

    sessao = criar_sessao()
    cliente = ClienteQueridoDiario(sessao)

    # 1) Busca e agrupamento por edição de diário.
    diarios: dict[str, dict] = {}
    trechos: dict[str, list[str]] = {}
    consultas_ok = 0
    for consulta in CONSULTAS:
        log.info("Consulta: %s", consulta)
        erros_antes = len(cliente.erros)
        for d in cliente.buscar(consulta, inicio, fim, modo, territorios, max_por_consulta):
            if ufs and (d.get("state_code") or "").upper() not in ufs:
                continue
            chave = _chave_diario(d)
            diarios.setdefault(chave, d)
            for t in d.get("excerpts") or []:
                if t and t not in trechos.setdefault(chave, []):
                    trechos[chave].append(t)
        consultas_ok += int(len(cliente.erros) == erros_antes)

    log.info("Diários candidatos: %d", len(diarios))

    # 2) Filtro jurídico por diário.
    atos: list[Ato] = []
    contador_txt = [0]
    for i, (chave, d) in enumerate(diarios.items(), 1):
        encontrados = processar_diario(sessao, d, trechos.get(chave, []), usar_texto, contador_txt, max_textos)
        if encontrados:
            log.info("[%d/%d] %s/%s %s: %d ato(s)", i, len(diarios), d.get("territory_name"),
                     d.get("state_code"), d.get("date"), len(encontrados))
        atos.extend(encontrados)
        if usar_texto:
            time.sleep(0.3)  # gentileza com o armazenamento de arquivos

    # 3) Persistência.
    resultado = mesclar_e_salvar(atos)
    estat = {
        "periodo": f"{inicio.isoformat()} a {fim.isoformat()}",
        "modo": modo,
        "consultas_ok": consultas_ok,
        "consultas_total": len(CONSULTAS),
        "diarios_candidatos": len(diarios),
        "textos_integrais_baixados": contador_txt[0],
        "atos_extraidos": len(atos),
        "novos": resultado["novos"],
        "atualizados": resultado["atualizados"],
        "erros": cliente.erros[:20],
        "base_api": cliente.base,
    }
    registrar_execucao(FONTE, estat)
    log.info("Resultado: %s", estat)

    resumo_github(
        "### Coletor municipal (Querido Diário)\n"
        f"* Período: {estat['periodo']}\n"
        f"* Consultas bem-sucedidas: {consultas_ok}/{len(CONSULTAS)}\n"
        f"* Diários candidatos: {len(diarios)} | textos integrais: {contador_txt[0]}\n"
        f"* Atos extraídos: {len(atos)} | novos: {resultado['novos']} | total no acervo: {resultado['total']}\n"
    )

    # Falha total da fonte: código 2 para o GitHub Actions sinalizar o erro.
    if consultas_ok == 0:
        log.error("Nenhuma consulta ao Querido Diário teve sucesso.")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    configurar_log()
    p = argparse.ArgumentParser(description="Coletor municipal (Querido Diário)")
    p.add_argument("--dias", type=int, default=int(os.getenv("DIAS_RETROATIVOS", "5")))
    p.add_argument("--ate", type=date.fromisoformat, default=None, help="data final YYYY-MM-DD")
    p.add_argument("--ufs", default=os.getenv("QD_UFS", ""), help="ex.: PR,SC,SP")
    p.add_argument("--territorios", default=os.getenv("QD_TERRITORIOS", ""), help="códigos IBGE")
    p.add_argument("--sem-texto", action="store_true", help="não baixar o texto integral")
    p.add_argument("--modo", choices=sorted(MODOS), default=os.getenv("QD_MODO", "indexacao"),
                   help="indexacao (diário, padrão) ou publicacao (carga histórica)")
    a = p.parse_args(argv)
    ufs = {u.strip().upper() for u in a.ufs.split(",") if u.strip()}
    territorios = [t.strip() for t in a.territorios.split(",") if t.strip()]
    return executar(a.dias, a.ate, ufs, territorios, usar_texto=not a.sem_texto, modo=a.modo)


if __name__ == "__main__":
    sys.exit(main())
