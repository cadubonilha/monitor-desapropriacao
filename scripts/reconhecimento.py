"""
Reconhecimento de portais (execução manual, não faz parte da coleta diária).

Objetivo: o ambiente de desenvolvimento do projeto não alcança os portais
de Diários Oficiais. Este script roda no GitHub Actions, que alcança, e
grava em debug/ o status HTTP, os cabeçalhos e o início do HTML de cada URL
candidata. Com esses arquivos versionados no repositório, os raspadores
diretos (a começar pelo DIOE/PR) são escritos sobre a estrutura real, sem
adivinhação.

As URLs do DIOE/PR e da legislação estadual são CANDIDATAS (não verificadas).

Uso: Actions > Reconhecimento de portais > Run workflow
     ou: python scripts/reconhecimento.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "debug"
LIMITE = 150_000  # bytes guardados por página

hoje = datetime.now(timezone(timedelta(hours=-3))).date()
ontem_util = hoje - timedelta(days=1 if hoje.weekday() != 0 else 3)

ALVOS = {
    # Validação das fontes já implementadas
    "qd_api": "https://api.queridodiario.org.br/gazettes?querystring=%22utilidade%20p%C3%BAblica%22%20%2B%20desapropria*"
              f"&published_since={(hoje - timedelta(days=10)).isoformat()}&size=2&excerpt_size=300",
    "qd_txt_antigo": "https://data.queridodiario.ok.org.br/3303302/2026-10-17/e3bb40da5aa87461f7ba6df894b592019f5985a7.txt",
    "qd_txt_novo": "https://data.queridodiario.org.br/3303302/2026-10-17/e3bb40da5aa87461f7ba6df894b592019f5985a7.txt",
    "dou_leitura": f"https://www.in.gov.br/leiturajornal?data={ontem_util.strftime('%d-%m-%Y')}&secao=do1",
    "inlabs_raiz": "https://inlabs.in.gov.br/",
    "google_news_rss": "https://news.google.com/rss/search?q=%22utilidade+p%C3%BAblica%22+desapropria%C3%A7%C3%A3o+"
                       "%22Paran%C3%A1%22+site%3Agov.br+when%3A7d&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    # Candidatas para o raspador estadual do Paraná
    "pr_imprensa_oficial": "http://www.imprensaoficial.pr.gov.br/",
    "pr_dioe_documentos": "https://www.documentos.dioe.pr.gov.br/dioe/consultaPublicaPDF.do?action=pgLocalizar",
    "pr_dioe_raiz": "https://www.documentos.dioe.pr.gov.br/",
    "pr_legislacao": "https://www.legislacao.pr.gov.br/legislacao/",
    "pr_aen": "https://www.aen.pr.gov.br/",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def main() -> int:
    DESTINO.mkdir(exist_ok=True)
    relatorio = {"executado_em": datetime.now(timezone.utc).isoformat(), "alvos": {}}
    for nome, url in ALVOS.items():
        info: dict = {"url": url}
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR"},
                             timeout=60, allow_redirects=True)
            corpo = r.content[:LIMITE]
            info.update(
                status=r.status_code,
                url_final=r.url,
                content_type=r.headers.get("Content-Type"),
                tamanho_total=len(r.content),
                redirecionamentos=[h.url for h in r.history],
                links_amostra=sorted(set(re.findall(rb'href="([^"#]{5,200})"', corpo)))[:120],
                formularios=re.findall(rb"<form[^>]{0,400}>", corpo)[:20],
            )
            info["links_amostra"] = [x.decode("utf-8", "replace") for x in info["links_amostra"]]
            info["formularios"] = [x.decode("utf-8", "replace") for x in info["formularios"]]
            ext = "json" if "json" in (info["content_type"] or "") else (
                "xml" if "xml" in (info["content_type"] or "") else "html")
            (DESTINO / f"{nome}.{ext}").write_bytes(corpo)
        except requests.RequestException as e:
            info["erro"] = f"{type(e).__name__}: {e}"
        relatorio["alvos"][nome] = info
        print(f"{nome:22s} {info.get('status', info.get('erro'))}")
    (DESTINO / "relatorio.json").write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
