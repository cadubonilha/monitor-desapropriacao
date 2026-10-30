"""Gera index.html (GitHub Pages) a partir de dashboard.html, acrescentando o esqueleto HTML."""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
src = (RAIZ / "dashboard.html").read_text(encoding="utf-8")
pagina = (
    "<!doctype html>\n<!-- Gerado a partir de dashboard.html (fonte única). Para atualizar: python scripts/gerar_index.py -->\n"
    '<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
    "body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>\n"
    "</head><body>\n" + src + "\n</body></html>\n"
)
(RAIZ / "index.html").write_text(pagina, encoding="utf-8")
print("index.html atualizado")
