"""
Utilitários compartilhados por todos os coletores (municipal, federal, estadual).

Responsabilidades:
  * Esquema único do registro (classe Ato) exigido pelo Dashboard.
  * Geração de ID estável (hash) para deduplicação entre execuções.
  * Sessão HTTP resiliente: retentativas com backoff, rotação de User-Agent e timeout.
  * Mesclagem incremental do arquivo consolidado data/atos.json.
  * Registro de metadados da execução em data/meta.json (lido pelo Dashboard).

Nenhuma função deste módulo chama serviços pagos ou modelos de IA.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Caminhos e constantes
# ---------------------------------------------------------------------------
RAIZ = Path(__file__).resolve().parent.parent
DIR_DADOS = Path(os.getenv("DIR_DADOS", RAIZ / "data"))
ARQ_ATOS = DIR_DADOS / "atos.json"
ARQ_META = DIR_DADOS / "meta.json"

FUSO_BR = timezone(timedelta(hours=-3))  # Horário de Brasília (sem horário de verão)

ESFERAS_VALIDAS = {"Federal", "Estadual", "Municipal"}

# Lista curta de User-Agents reais de navegadores. A rotação reduz bloqueios
# simples por assinatura em portais estaduais (será reaproveitada nos Passos 2 e 3).
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
]

log = logging.getLogger("monitor")


def configurar_log(nivel: str | None = None) -> None:
    """Configura o logging padrão (nível controlado pela variável LOG_LEVEL)."""
    logging.basicConfig(
        level=(nivel or os.getenv("LOG_LEVEL", "INFO")).upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def agora_iso() -> str:
    """Data e hora atuais no fuso de Brasília, em ISO 8601."""
    return datetime.now(FUSO_BR).replace(microsecond=0).isoformat()


def hoje_br() -> date:
    """Data de hoje no fuso de Brasília (o runner do GitHub roda em UTC)."""
    return datetime.now(FUSO_BR).date()


# ---------------------------------------------------------------------------
# Esquema do registro
# ---------------------------------------------------------------------------
@dataclass
class Ato:
    """
    Registro padronizado de um ato expropriatório.

    Os nove primeiros campos são o contrato obrigatório do projeto. Os demais
    são enriquecimentos opcionais extraídos por Regex, úteis para filtros do
    Dashboard; consumidores que não os conheçam podem simplesmente ignorá-los.
    """

    id: str
    data_publicacao: str  # YYYY-MM-DD
    esfera: str  # Federal | Estadual | Municipal
    uf: str
    municipio: str | None
    orgao_emissor: str
    numero_ato: str | None
    resumo_contexto: str
    url_fonte: str
    # Enriquecimentos ------------------------------------------------------
    tipo_ato: str = "Não identificado"
    tags: list[str] = field(default_factory=list)
    finalidades: list[str] = field(default_factory=list)
    matriculas: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    score: int = 0
    confianca: str = "baixa"
    fonte: str = ""
    url_texto: str | None = None
    edicao: str | None = None
    coletado_em: str = field(default_factory=agora_iso)

    def validar(self) -> None:
        """Garante a integridade mínima do registro antes de gravar."""
        if self.esfera not in ESFERAS_VALIDAS:
            raise ValueError(f"esfera inválida: {self.esfera}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.data_publicacao or ""):
            raise ValueError(f"data_publicacao inválida: {self.data_publicacao}")
        if not self.url_fonte:
            raise ValueError("url_fonte vazia")
        if not self.uf or len(self.uf) != 2:
            raise ValueError(f"uf inválida: {self.uf}")


def gerar_id(*partes: object) -> str:
    """Hash SHA-1 (16 hex) estável a partir das partes informadas."""
    base = "|".join("" if p is None else str(p).strip().lower() for p in partes)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def criar_sessao(total_retentativas: int = 4, backoff: float = 2.0) -> requests.Session:
    """
    Sessão HTTP com retentativa automática em 429 e 5xx (backoff exponencial,
    respeitando o cabeçalho Retry-After quando o servidor o envia).
    """
    sessao = requests.Session()
    retry = Retry(
        total=total_retentativas,
        connect=total_retentativas,
        read=total_retentativas,
        status=total_retentativas,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    sessao.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
        }
    )
    return sessao


def rotacionar_user_agent(sessao: requests.Session) -> None:
    """Troca o User-Agent da sessão (usar após respostas 403)."""
    sessao.headers["User-Agent"] = random.choice(USER_AGENTS)


class Limitador:
    """Garante intervalo mínimo entre chamadas (ex.: 60 req/min = 1,0 s)."""

    def __init__(self, intervalo_s: float) -> None:
        self.intervalo = intervalo_s
        self._ultimo = 0.0

    def aguardar(self) -> None:
        espera = self.intervalo - (time.monotonic() - self._ultimo)
        if espera > 0:
            time.sleep(espera)
        self._ultimo = time.monotonic()


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------
def _ler_json(caminho: Path, padrao):
    if not caminho.exists():
        return padrao
    try:
        with caminho.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        log.error("Arquivo corrompido, preservando cópia: %s", caminho)
        caminho.rename(caminho.with_suffix(".corrompido.json"))
        return padrao


def _gravar_json_atomico(caminho: Path, dados) -> None:
    """Grava em arquivo temporário e renomeia (evita JSON truncado se o job cair)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(caminho)


def carregar_atos(caminho: Path | None = None) -> list[dict]:
    return _ler_json(caminho or ARQ_ATOS, [])


def mesclar_e_salvar(novos: Iterable[Ato], caminho: Path | None = None) -> dict:
    """
    Mescla os atos novos no consolidado, sem duplicar.

    Regras:
      * ID inédito: inclui.
      * ID existente: mantém o registro original (preserva coletado_em) e
        apenas preenche campos que estavam vazios ou amplia listas.
      * Retenção opcional: RETENCAO_DIAS > 0 remove atos mais antigos.
    Retorna contadores {novos, atualizados, total}.
    """
    caminho = caminho or ARQ_ATOS
    existentes = {a["id"]: a for a in carregar_atos(caminho)}
    n_novos = n_atual = 0

    for ato in novos:
        ato.validar()
        reg = asdict(ato)
        atual = existentes.get(ato.id)
        if atual is None:
            existentes[ato.id] = reg
            n_novos += 1
            continue
        mudou = False
        for chave, valor in reg.items():
            if chave == "coletado_em":
                continue
            if isinstance(valor, list):
                uniao = sorted(set(atual.get(chave) or []) | set(valor))
                if uniao != (atual.get(chave) or []):
                    atual[chave] = uniao
                    mudou = True
            elif valor not in (None, "", 0) and atual.get(chave) in (None, "", 0, "Não identificado"):
                atual[chave] = valor
                mudou = True
        if reg["score"] > (atual.get("score") or 0):
            atual["score"], atual["confianca"] = reg["score"], reg["confianca"]
            mudou = True
        n_atual += int(mudou)

    lista = list(existentes.values())

    retencao = int(os.getenv("RETENCAO_DIAS", "0") or 0)
    if retencao > 0:
        corte = (hoje_br() - timedelta(days=retencao)).isoformat()
        lista = [a for a in lista if a["data_publicacao"] >= corte]

    # Mais recentes primeiro; empate ordenado por UF, município e número.
    lista.sort(key=lambda a: (a.get("municipio") or "", a.get("numero_ato") or ""))
    lista.sort(key=lambda a: (a["data_publicacao"], -(a.get("score") or 0)), reverse=True)
    _gravar_json_atomico(caminho, lista)
    return {"novos": n_novos, "atualizados": n_atual, "total": len(lista)}


def registrar_execucao(fonte: str, estatisticas: dict, caminho: Path | None = None) -> None:
    """Atualiza data/meta.json com o resultado da execução de uma fonte."""
    caminho = caminho or ARQ_META
    meta = _ler_json(caminho, {})
    meta.setdefault("fontes", {})[fonte] = {"executado_em": agora_iso(), **estatisticas}
    meta["ultima_execucao"] = agora_iso()
    meta["total_atos"] = len(carregar_atos(caminho.parent / ARQ_ATOS.name))
    _gravar_json_atomico(caminho, meta)


def resumo_github(texto_md: str) -> None:
    """Escreve no painel de resumo do GitHub Actions, quando disponível."""
    destino = os.getenv("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write(texto_md + "\n")
