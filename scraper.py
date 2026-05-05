"""
Scraper Porto de Lisboa
═════════════════════════════════════════════════════════════
Corre de hora a hora via GitHub Actions. Vai a cada uma das
páginas do portodelisboa.pt, deixa o JavaScript renderizar a
tabela, extrai os dados e escreve um único JSON em
    data/lisbon_port.json
que a web-app vai ler.

A página é JavaScript-rendered (Liferay), por isso usamos
Playwright + Chromium em modo headless.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ════════════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ════════════════════════════════════════════════════════════
PAGES = {
    "arrival":   "https://www.portodelisboa.pt/previsao-de-chegadas",
    "departure": "https://www.portodelisboa.pt/partidas",
    "in_port":   "https://www.portodelisboa.pt/navios-em-porto",
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Mapeamento de terminais de cruzeiro (lower-case keys)
CRUISE_TERMINALS = {
    "santa apol":  "Santa Apolónia",
    "apolóni":     "Santa Apolónia",
    "apoloni":     "Santa Apolónia",
    "jardim":      "Jardim do Tabaco",
    "tabaco":      "Jardim do Tabaco",
    "rocha":       "Rocha Conde Óbidos",
    "óbidos":      "Rocha Conde Óbidos",
    "obidos":      "Rocha Conde Óbidos",
    "alcântara":   "Alcântara",
    "alcantara":   "Alcântara",
}

HAZARD_TERMINAL = "Terminal Multiusos do Poço do Bispo"
HAZARD_TERMINAL_KEYS = ["poço", "poco", "bispo", "multiusos"]

# Palavras-chave que indicam carga IMDG / matérias perigosas
HAZARD_KEYWORDS = [
    "imdg", "químic", "quimic", "combust", "gnl", "glp", "gás", "gas",
    "tanker", "perigos", "hazard", "fuel", "oil", "petroleo", "petróleo",
    "metano", "metanol", "etileno", "propano", "butano", "amónia",
    "amonia", "enxofre", "sulfur", "químico", "químicos",
]

OUTPUT_PATH = Path("data/lisbon_port.json")


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def normalise(s: str) -> str:
    """Limpa whitespace duplicado."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_date_iso(date_str: str, hour_str: str = "") -> Optional[str]:
    """
    Converte data + hora em ISO8601. Aceita:
      - dd/mm/yyyy  ou  dd-mm-yyyy
      - yyyy-mm-dd
      - hora opcional como hh:mm  ou  hhHmm
    Retorna None se não conseguir parsear.
    """
    if not date_str:
        return None
    s = date_str.strip()
    y = m = d = None

    mt = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", s)
    if mt:
        d, m, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
    else:
        mt = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
        if mt:
            y, m, d = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))

    if not (y and m and d):
        return None

    hh = mm = 0
    if hour_str:
        ht = re.search(r"(\d{1,2})[:hH.](\d{2})", hour_str)
        if ht:
            hh, mm = int(ht.group(1)), int(ht.group(2))

    try:
        return datetime(y, m, d, hh, mm).isoformat()
    except ValueError:
        return None


def classify_terminal(term_str: str, cargo_str: str) -> tuple[str, bool]:
    """
    Devolve (nome_terminal_normalizado, é_matéria_perigosa).
    Heurística:
      1) Se o terminal mencionar Poço do Bispo / Multiusos → IMDG
      2) Se a carga indicar palavras-chave IMDG → IMDG
      3) Se o terminal corresponder a um terminal de cruzeiro → cruzeiro
      4) Caso contrário → fallback para Alcântara (e cruzeiro)
    """
    t = (term_str or "").lower()
    c = (cargo_str or "").lower()

    if any(k in t for k in HAZARD_TERMINAL_KEYS):
        return HAZARD_TERMINAL, True
    if any(k in c for k in HAZARD_KEYWORDS):
        return HAZARD_TERMINAL, True

    for key, name in CRUISE_TERMINALS.items():
        if key in t:
            return name, False

    return "Alcântara", False


def pick(row: dict, *keys: str) -> str:
    """Devolve o primeiro valor cujo nome de coluna contenha algum dos keys."""
    for k in keys:
        for header in row:
            if k in header:
                return row[header]
    return ""


# ════════════════════════════════════════════════════════════
# SCRAPING
# ════════════════════════════════════════════════════════════
def scrape_table(page, url: str) -> list[dict]:
    """Vai à URL, espera que a tabela renderize, devolve lista de dicts."""
    print(f"  → {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)

    # Esperar que apareça pelo menos uma linha de dados.
    # Se a página não tiver navios neste momento, tolera-se o timeout.
    try:
        page.wait_for_selector("table tbody tr", timeout=20_000)
    except PWTimeout:
        print("    (tabela não renderizou — pode estar sem dados)")
        return []

    # Extracção do DOM via JS — apanha a maior tabela e devolve linhas como dicts
    rows = page.evaluate("""
        () => {
            const tables = Array.from(document.querySelectorAll('table'));
            if (!tables.length) return [];
            const best = tables.reduce((a, b) =>
                a.querySelectorAll('tr').length >= b.querySelectorAll('tr').length ? a : b);
            const trs = Array.from(best.querySelectorAll('tr'));
            if (trs.length < 2) return [];
            const headers = Array.from(trs[0].querySelectorAll('th, td'))
                .map(c => c.textContent.trim().toLowerCase());
            const out = [];
            for (let i = 1; i < trs.length; i++) {
                const cells = Array.from(trs[i].querySelectorAll('td, th'))
                    .map(c => c.textContent.trim());
                if (cells.every(c => !c)) continue;
                const row = {};
                headers.forEach((h, j) => row[h] = cells[j] || '');
                out.push(row);
            }
            return out;
        }
    """)

    print(f"    {len(rows)} linhas extraídas")
    return rows


def row_to_record(raw: dict, default_type: str) -> Optional[dict]:
    """Converte uma linha bruta da tabela num registo normalizado."""
    if not raw:
        return None

    name = pick(raw, "navio", "ship", "vessel", "nome")
    name = normalise(name)
    if not name:
        return None

    date_s = pick(raw, "data", "date")
    hour_s = pick(raw, "hora", "time", "eta", "etd")
    cargo  = pick(raw, "carga", "cargo", "tipo de carga", "mercador")
    line   = pick(raw, "armador", "companhia", "operador", "agente")
    term   = pick(raw, "terminal", "cais", "berço", "berco")
    frm    = pick(raw, "procedên", "procedenc", "from", "origem", "porto anterior")
    to_    = pick(raw, "destino", "to", "próximo", "proximo", "next")
    pax_s  = pick(raw, "passageiros", "pax", "passengers")

    terminal, is_hazard = classify_terminal(term, cargo)
    iso_date = parse_date_iso(date_s, hour_s)

    pax = 0
    digits = re.sub(r"\D", "", pax_s)
    if digits:
        try:
            pax = int(digits)
        except ValueError:
            pax = 0

    return {
        "name":      normalise(name),
        "line":      normalise(line),
        "type":      "hazard" if is_hazard else default_type,
        "terminal":  terminal,
        "from":      normalise(frm),
        "to":        normalise(to_),
        "date":      iso_date,
        "hour":      normalise(hour_s),
        "pax":       pax,
        "cargo":     normalise(cargo),
        "is_hazard": is_hazard,
    }


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    print(f"=== Scraper Porto de Lisboa @ {fetched_at} ===\n")

    all_records: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pt-PT",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # Aceitar cookies à primeira página (depois persiste no contexto)
        cookies_accepted = False

        for kind, url in PAGES.items():
            print(f"[{kind}]")
            try:
                rows = scrape_table(page, url)

                # Tentar fechar o banner de cookies, se ainda existir
                if not cookies_accepted:
                    try:
                        btn = page.locator("button:has-text('Aceitar')").first
                        if btn and btn.is_visible(timeout=1500):
                            btn.click(timeout=1500)
                            cookies_accepted = True
                            print("    (banner de cookies aceite)")
                    except Exception:
                        pass

                for raw in rows:
                    rec = row_to_record(raw, kind if kind != "in_port" else "transit")
                    if rec:
                        all_records.append(rec)
            except Exception as e:
                msg = f"{kind}: {type(e).__name__}: {e}"
                print(f"    ✗ {msg}")
                errors.append(msg)
            print()

        browser.close()

    # Deduplicação por (nome, terminal, data, tipo)
    seen = set()
    unique = []
    for r in all_records:
        k = (r["name"], r["terminal"], r["date"], r["type"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(r)

    ships   = [r for r in unique if not r["is_hazard"]]
    hazards = [r for r in unique if r["is_hazard"]]

    print(f"=== Total: {len(unique)} registos ===")
    print(f"  cruzeiros:        {len(ships)}")
    print(f"  matérias perig.:  {len(hazards)}")
    if errors:
        print(f"  erros:            {len(errors)}")
        for e in errors:
            print(f"    - {e}")

    payload = {
        "fetched_at": fetched_at,
        "source":     "portodelisboa.pt",
        "count":      len(unique),
        "errors":     errors,
        "ships":      ships,
        "hazards":    hazards,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✓ {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
