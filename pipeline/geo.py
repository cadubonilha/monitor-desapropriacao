"""
Inferência de UF e município a partir do texto do ato (usada nas esferas
federal e estadual, em que o Diário não informa o território diretamente).

Determinístico: tabela de UFs + Regex. Quando não há certeza, devolve
UF "BR" (âmbito nacional/indeterminado) e município None.
"""
from __future__ import annotations

import re
from collections import Counter

from .filtro_juridico import dobrar

UFS = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso do sul": "MS", "mato grosso": "MT", "minas gerais": "MG",
    "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI",
    "rio de janeiro": "RJ", "rio grande do norte": "RN", "rio grande do sul": "RS",
    "rondonia": "RO", "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP",
    "sergipe": "SE", "tocantins": "TO",
}
SIGLAS = set(UFS.values())
NOMES_POR_SIGLA = {v: k for k, v in UFS.items()}

# Nomes mais longos primeiro, para "mato grosso do sul" vencer "mato grosso".
_NOMES = "|".join(sorted(map(re.escape, UFS), key=len, reverse=True))
RE_ESTADO = re.compile(r"\bestado\s+d[eoa]s?\s+(" + _NOMES + r")\b")
RE_MUNICIPIO = re.compile(
    r"\bmunicipios?\s+de\s+([a-z][a-z' ]{1,40}?)\s*(?:,|/|-|\(|\bno\b|\bna\b|\bestado\b|\be\b|\bem\b|\.)"
)
RE_MUN_SIGLA = re.compile(r"\bmunicipios?\s+de\s+([a-z][a-z' ]{1,40}?)\s*[/\-]\s*([a-z]{2})\b")


def _titulo(nome: str, original: str, pos: int) -> str:
    """Recupera a grafia original (com acentos) do trecho dobrado."""
    trecho = original[pos: pos + len(nome)]
    return trecho.strip(" ,.-/") or nome.title()


def inferir_local(texto: str, uf_padrao: str = "BR") -> tuple[str, str | None]:
    """
    Devolve (UF, município) a partir do texto. Regras, em ordem:
      1. "Município de X/UF" ou "Município de X - UF".
      2. "Município de X, Estado do Y".
      3. UF mais citada em "Estado do Y".
    """
    if not texto:
        return uf_padrao, None
    dob = dobrar(texto)

    m = RE_MUN_SIGLA.search(dob)
    if m and m.group(2).upper() in SIGLAS:
        return m.group(2).upper(), _titulo(m.group(1), texto, m.start(1))

    estados = Counter(UFS[e] for e in RE_ESTADO.findall(dob))
    uf = estados.most_common(1)[0][0] if estados else uf_padrao

    m = RE_MUNICIPIO.search(dob)
    municipio = None
    if m:
        nome = m.group(1).strip()
        # Descarta capturas genéricas ("municipio de origem", "de sua localização").
        if nome not in {"origem", "destino", "sua localizacao", "situacao"} and len(nome) >= 3:
            municipio = _titulo(nome, texto, m.start(1))
    return uf, municipio
