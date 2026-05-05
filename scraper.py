"""
Scraper Porto de Lisboa — MODO DIAGNÓSTICO
═══════════════════════════════════════════════════════════════
Esta versão NÃO tenta extrair dados de navios.
Em vez disso, para cada uma das 3 páginas:
    1) Tira um screenshot de página inteira (.png)
    2) Guarda o HTML completo depois do JS renderizar (.html)

Os ficheiros vão para data/diagnostic/ e ficam visíveis no
repositório para análise.

Quando soubermos onde os dados estão escondidos, voltamos
ao scraper normal.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


PAGES = {
    "arrivals":   "https://www.portodelisboa.pt/previsao-de-chegadas",
    "departures": "https://www.portodelisboa.pt/partidas",
    "in_port":    "https://www.portodelisboa.pt/navios-em-porto",
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

OUT_DIR = Path("data/diagnostic")


def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    print(f"=== DIAGNÓSTICO Porto de Lisboa @ {fetched_at} ===\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summary = {
        "fetched_at": fetched_at,
        "pages": {},
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pt-PT",
            viewport={"width": 1400, "height": 1000},
        )
        page = context.new_page()
        cookies_done = False

        for kind, url in PAGES.items():
            print(f"[{kind}] {url}")
            page_info = {"url": url}

            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                # Esperar mais um pouco para conteúdos lazy-loaded
                page.wait_for_timeout(5000)

                # Aceitar cookies (apenas na primeira página)
                if not cookies_done:
                    try:
                        btn = page.locator("button:has-text('Aceitar')").first
                        if btn and btn.is_visible(timeout=2000):
                            btn.click(timeout=2000)
                            cookies_done = True
                            print("   ✓ banner cookies aceite")
                            page.wait_for_timeout(2000)
                    except Exception:
                        pass

                # Tentar localizar tabelas, iframes, e datatables
                stats = page.evaluate("""
                    () => {
                        const tables = document.querySelectorAll('table');
                        const iframes = document.querySelectorAll('iframe');
                        const dataTables = document.querySelectorAll('[class*="dataTable"], [class*="datatable"], [id*="dataTable"], [id*="datatable"]');
                        const lists = document.querySelectorAll('ul, ol');
                        const tablesInfo = Array.from(tables).map(t => ({
                            id: t.id || '',
                            cls: t.className || '',
                            rows: t.querySelectorAll('tr').length,
                            firstHeader: Array.from(t.querySelectorAll('th, thead td'))
                                .slice(0, 6).map(c => c.textContent.trim()),
                            firstRow: Array.from(t.querySelectorAll('tbody tr')).slice(0, 1)
                                .flatMap(r => Array.from(r.querySelectorAll('td')).slice(0, 6)
                                    .map(c => c.textContent.trim())),
                        }));
                        const iframesInfo = Array.from(iframes).map(f => ({
                            src: f.src || '',
                            id: f.id || '',
                            cls: f.className || '',
                        }));
                        return {
                            tableCount: tables.length,
                            iframeCount: iframes.length,
                            dataTableCount: dataTables.length,
                            listCount: lists.length,
                            tables: tablesInfo,
                            iframes: iframesInfo,
                            bodyTextLength: document.body.innerText.length,
                        };
                    }
                """)
                print(f"   tables={stats['tableCount']} iframes={stats['iframeCount']} dataTables={stats['dataTableCount']} lists={stats['listCount']}")
                page_info["stats"] = stats

                # Screenshot
                screenshot_path = OUT_DIR / f"{kind}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"   ✓ {screenshot_path}")

                # HTML
                html = page.content()
                html_path = OUT_DIR / f"{kind}.html"
                html_path.write_text(html, encoding="utf-8")
                print(f"   ✓ {html_path} ({len(html):,} bytes)")
                page_info["html_size"] = len(html)
                page_info["ok"] = True

            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"   ✗ {msg}")
                page_info["ok"] = False
                page_info["error"] = msg

            summary["pages"][kind] = page_info
            print()

        browser.close()

    # Sumário em JSON para fácil consulta
    summary_path = OUT_DIR / "_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✓ {summary_path}")

    # Manter o ficheiro principal vazio para a web-app não partir
    Path("data/lisbon_port.json").write_text(
        json.dumps({
            "fetched_at": fetched_at,
            "source": "portodelisboa.pt",
            "count": 0,
            "errors": ["Modo diagnóstico — scraping desactivado"],
            "ships": [],
            "hazards": [],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
