"""
Scraper Porto de Lisboa — VERSÃO FINAL
═══════════════════════════════════════════════════════════════
Estratégia híbrida:

  • Navios em Porto    → parsing das linhas da tabela React
                         (selectors: .rdt_TableRow / .rdt_TableCell)
  • Previsão Chegadas  → preencher datas, clicar "Pesquisar",
                         descarregar CSV via "Tabela completa (csv)"
  • Partidas           → idem chegadas

Janela temporal: hoje-2 a hoje+2 dias.

Output: data/lisbon_port.json com todos os navios normalizados.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


# ════════════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ════════════════════════════════════════════════════════════
URL_ARRIVALS    = "https://www.portodelisboa.pt/previsao-de-chegadas"
URL_DEPARTURES  = "https://www.portodelisboa.pt/partidas"
URL_IN_PORT     = "https://www.portodelisboa.pt/navios-em-porto"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Janela: 2 dias para trás, 2 para a frente
WINDOW_DAYS_BACK    = 2
WINDOW_DAYS_FORWARD = 2

# Mapeamento de terminais
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

HAZARD_KEYWORDS = [
    "imdg", "químic", "quimic", "combust", "gnl", "glp", "gás", "gas",
    "tanker", "perigos", "hazard", "fuel", "oil", "petroleo", "petróleo",
    "metano", "metanol", "etileno", "propano", "butano", "amónia",
    "amonia", "enxofre", "sulfur",
]

# Tipos de navio que classificamos como "cruzeiro"
CRUISE_TYPE_KEYWORDS = ["cruzeiro", "cruise", "passageiros", "passenger", "passageiro"]

OUTPUT_PATH = Path("data/lisbon_port.json")


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_date_iso(date_str: str) -> Optional[str]:
    """
    Converte data em ISO8601. Aceita:
      - 'yyyy-mm-dd hh:mm'  (formato da tabela React)
      - 'yyyy-mm-dd'
      - 'dd/mm/yyyy [hh:mm]'
      - 'dd-mm-yyyy [hh:mm]'
    """
    if not date_str:
        return None
    s = date_str.strip()

    # yyyy-mm-dd hh:mm  ou  yyyy-mm-dd
    mt = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[\sT](\d{1,2})[:h.](\d{2}))?", s)
    if mt:
        y, m, d = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        hh = int(mt.group(4)) if mt.group(4) else 0
        mm = int(mt.group(5)) if mt.group(5) else 0
        try:
            return datetime(y, m, d, hh, mm).isoformat()
        except ValueError:
            return None

    # dd/mm/yyyy ou dd-mm-yyyy
    mt = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})(?:\s+(\d{1,2})[:h.](\d{2}))?", s)
    if mt:
        d, m, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        hh = int(mt.group(4)) if mt.group(4) else 0
        mm = int(mt.group(5)) if mt.group(5) else 0
        try:
            return datetime(y, m, d, hh, mm).isoformat()
        except ValueError:
            return None

    return None


def hour_from_iso(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return ""


def classify_terminal_and_kind(term_str: str, cargo_str: str, type_str: str) -> tuple[str, bool, bool]:
    """
    Devolve (terminal_normalizado, é_perigoso, é_cruzeiro).
    """
    t = (term_str or "").lower()
    c = (cargo_str or "").lower()
    ty = (type_str or "").lower()

    is_hazard = (
        any(k in t for k in HAZARD_TERMINAL_KEYS)
        or any(k in c for k in HAZARD_KEYWORDS)
        or any(k in ty for k in HAZARD_KEYWORDS)
    )

    is_cruise = any(k in ty for k in CRUISE_TYPE_KEYWORDS)

    matched_term = None
    for key, name in CRUISE_TERMINALS.items():
        if key in t:
            matched_term = name
            break

    if is_hazard:
        return HAZARD_TERMINAL, True, False
    if matched_term:
        return matched_term, False, True   # se está num terminal de cruzeiro, é cruzeiro
    if is_cruise:
        return matched_term or "Alcântara", False, True

    # Não é IMDG nem cruzeiro — descartamos depois
    return matched_term or normalise(term_str) or "Outro", False, False


def fingerprint(name: str, terminal: str, date_iso: Optional[str], type_label: str) -> tuple:
    return (name.lower(), terminal.lower(), date_iso or "", type_label)


# ════════════════════════════════════════════════════════════
# COOKIES BANNER
# ════════════════════════════════════════════════════════════
def accept_cookies(page: Page) -> bool:
    try:
        btn = page.locator("button:has-text('Aceitar')").first
        if btn and btn.is_visible(timeout=2000):
            btn.click(timeout=2000)
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


# ════════════════════════════════════════════════════════════
# 1. NAVIOS EM PORTO — parsing da tabela React
# ════════════════════════════════════════════════════════════
def scrape_in_port(page: Page) -> list[dict]:
    print(f"[in_port] {URL_IN_PORT}")
    page.goto(URL_IN_PORT, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)

    try:
        page.wait_for_selector(".rdt_TableRow", timeout=20_000)
    except PWTimeout:
        print("    (sem linhas — página vazia)")
        return []

    page.wait_for_timeout(2000)

    rows = page.evaluate("""
        () => {
            const out = [];
            const headers = Array.from(document.querySelectorAll('.rdt_TableHeadRow .rdt_TableCol'))
                .map(c => c.textContent.trim().toLowerCase());
            const rowEls = document.querySelectorAll('.rdt_TableRow');
            rowEls.forEach(r => {
                const cells = Array.from(r.querySelectorAll('.rdt_TableCell'))
                    .map(c => c.textContent.trim());
                out.push({ headers, cells });
            });
            return out;
        }
    """)

    print(f"    {len(rows)} linhas extraídas")
    if not rows:
        return []

    out = []
    for r in rows:
        cells = r.get("cells") or []
        headers = r.get("headers") or []
        if len(cells) < 6:
            continue

        def col(idx: int, *header_keys: str) -> str:
            for k in header_keys:
                for i, h in enumerate(headers):
                    if k in h and i < len(cells):
                        return cells[i]
            return cells[idx] if idx < len(cells) else ""

        name        = col(0, "navio", "vessel", "ship")
        date_in     = col(1, "ata", "entrada", "data inicial", "início")
        date_out    = col(2, "etd", "saída", "data final", "fim")
        ship_type   = col(3, "tipo")
        operation   = col(4, "operação", "operacao")
        terminal    = col(5, "terminal", "cais", "berço", "berco")

        terminal_norm, is_hazard, is_cruise = classify_terminal_and_kind(
            terminal, operation, ship_type
        )

        if not is_hazard and not is_cruise:
            continue

        rec = {
            "name":      normalise(name),
            "line":      "",
            "type":      "transit",
            "terminal":  terminal_norm,
            "from":      "",
            "to":        "",
            "date":      parse_date_iso(date_in) or parse_date_iso(date_out),
            "hour":      hour_from_iso(parse_date_iso(date_in)),
            "pax":       0,
            "cargo":     normalise(operation) + (" · " + normalise(ship_type) if ship_type else ""),
            "is_hazard": is_hazard,
            "ship_type": normalise(ship_type),
            "source":    "in_port",
        }
        if rec["name"]:
            out.append(rec)

    return out


# ════════════════════════════════════════════════════════════
# 2/3. CHEGADAS / PARTIDAS via CSV
# ════════════════════════════════════════════════════════════
def scrape_csv_page(page: Page, url: str, page_label: str, default_type: str, today: datetime) -> list[dict]:
    print(f"[{page_label}] {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)
    page.wait_for_timeout(1500)

    d_start = (today - timedelta(days=WINDOW_DAYS_BACK)).strftime("%Y-%m-%d")
    d_end   = (today + timedelta(days=WINDOW_DAYS_FORWARD)).strftime("%Y-%m-%d")
    print(f"    intervalo: {d_start} → {d_end}")

    try:
        date_inputs = page.locator("input[type='date']")
        count = date_inputs.count()
        if count >= 2:
            date_inputs.nth(0).fill(d_start)
            date_inputs.nth(1).fill(d_end)
            print(f"    ✓ datas preenchidas")
        else:
            print(f"    ✗ esperava 2 inputs date, encontrei {count}")
            return []
    except Exception as e:
        print(f"    ✗ erro a preencher datas: {e}")
        return []

    try:
        page.locator("button:has-text('Pesquisar')").first.click(timeout=5000)
        page.wait_for_timeout(3000)
        print("    ✓ pesquisa submetida")
    except Exception as e:
        print(f"    ✗ erro a clicar Pesquisar: {e}")
        return []

    csv_text = ""
    try:
        with page.expect_download(timeout=15_000) as dl_info:
            page.locator("a:has-text('Tabela completa'), button:has-text('Tabela completa')").first.click(timeout=5000)
        download = dl_info.value
        csv_path = download.path()
        if csv_path:
            csv_text = Path(csv_path).read_text(encoding="utf-8", errors="replace")
            print(f"    ✓ CSV descarregado ({len(csv_text)} bytes)")
    except Exception as e:
        print(f"    ⚠ download CSV falhou: {e}")
        try:
            page.wait_for_selector(".rdt_TableRow", timeout=5000)
            return parse_react_table(page, default_type)
        except Exception:
            print("    ✗ também não há linhas React — sem dados")
            return []

    if not csv_text.strip():
        return []

    return parse_csv(csv_text, default_type)


def parse_csv(text: str, default_type: str) -> list[dict]:
    """Parser CSV — detecta separador automaticamente."""
    sample = text[:2000]
    semi = sample.count(";")
    comma = sample.count(",")
    sep = ";" if semi > comma else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    out = []
    for row in reader:
        rec = csv_row_to_record(row, default_type)
        if rec:
            out.append(rec)
    return out


def csv_row_to_record(row: dict, default_type: str) -> Optional[dict]:
    if not row:
        return None
    norm_row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

    def pick(*keys: str) -> str:
        for k in keys:
            for rk, rv in norm_row.items():
                if k in rk:
                    return rv
        return ""

    name = pick("navio", "ship", "vessel", "nome")
    if not name:
        return None

    date_s    = pick("data", "date", "eta", "etd")
    cargo     = pick("carga", "cargo", "tipo de carga", "mercador", "operação", "operacao")
    company   = pick("armador", "companhia", "operador", "agente", "line")
    terminal  = pick("terminal", "cais", "berço", "berco")
    ship_type = pick("tipo")
    frm       = pick("procedên", "procedenc", "from", "origem", "porto anterior")
    to_       = pick("destino", "to", "próximo", "proximo", "next")
    pax_s     = pick("passageiros", "pax", "passengers")

    terminal_norm, is_hazard, is_cruise = classify_terminal_and_kind(terminal, cargo, ship_type)

    if not is_hazard and not is_cruise:
        return None

    iso_date = parse_date_iso(date_s)
    pax = 0
    digits = re.sub(r"\D", "", pax_s)
    if digits:
        try:
            pax = int(digits)
        except ValueError:
            pax = 0

    return {
        "name":      normalise(name),
        "line":      normalise(company),
        "type":      "hazard" if is_hazard else default_type,
        "terminal":  terminal_norm,
        "from":      normalise(frm),
        "to":        normalise(to_),
        "date":      iso_date,
        "hour":      hour_from_iso(iso_date),
        "pax":       pax,
        "cargo":     normalise(cargo),
        "is_hazard": is_hazard,
        "ship_type": normalise(ship_type),
        "source":    default_type,
    }


def parse_react_table(page: Page, default_type: str) -> list[dict]:
    rows = page.evaluate("""
        () => {
            const out = [];
            const headers = Array.from(document.querySelectorAll('.rdt_TableHeadRow .rdt_TableCol'))
                .map(c => c.textContent.trim().toLowerCase());
            document.querySelectorAll('.rdt_TableRow').forEach(r => {
                const cells = Array.from(r.querySelectorAll('.rdt_TableCell'))
                    .map(c => c.textContent.trim());
                const obj = {};
                headers.forEach((h, i) => obj[h] = cells[i] || '');
                out.push(obj);
            });
            return out;
        }
    """)
    out = []
    for r in rows:
        rec = csv_row_to_record(r, default_type)
        if rec:
            out.append(rec)
    return out


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now()
    print(f"=== Scraper Porto de Lisboa @ {fetched_at} ===\n")

    all_records: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pt-PT",
            viewport={"width": 1400, "height": 1000},
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            recs = scrape_in_port(page)
            print(f"    → {len(recs)} registos\n")
            all_records.extend(recs)
        except Exception as e:
            msg = f"in_port: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}\n")
            errors.append(msg)

        try:
            recs = scrape_csv_page(page, URL_ARRIVALS, "arrivals", "arrival", today)
            print(f"    → {len(recs)} registos\n")
            all_records.extend(recs)
        except Exception as e:
            msg = f"arrivals: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}\n")
            errors.append(msg)

        try:
            recs = scrape_csv_page(page, URL_DEPARTURES, "departures", "departure", today)
            print(f"    → {len(recs)} registos\n")
            all_records.extend(recs)
        except Exception as e:
            msg = f"departures: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}\n")
            errors.append(msg)

        browser.close()

    today_mid = datetime(today.year, today.month, today.day)
    min_date = today_mid - timedelta(days=WINDOW_DAYS_BACK)
    max_date = today_mid + timedelta(days=WINDOW_DAYS_FORWARD, hours=23, minutes=59)

    seen = set()
    filtered = []
    for r in all_records:
        if not r.get("date"):
            continue
        try:
            d = datetime.fromisoformat(r["date"])
        except ValueError:
            continue
        if d < min_date or d > max_date:
            continue
        fp = fingerprint(r["name"], r["terminal"], r["date"], r["type"])
        if fp in seen:
            continue
        seen.add(fp)
        filtered.append(r)

    ships   = [r for r in filtered if not r["is_hazard"]]
    hazards = [r for r in filtered if r["is_hazard"]]

    print(f"\n=== Total filtrado (±2d): {len(filtered)} ===")
    print(f"  cruzeiros:        {len(ships)}")
    print(f"  matérias perig.:  {len(hazards)}")
    if errors:
        print(f"  erros:            {len(errors)}")

    payload = {
        "fetched_at": fetched_at,
        "source":     "portodelisboa.pt",
        "count":      len(filtered),
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
