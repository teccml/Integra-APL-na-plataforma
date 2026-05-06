"""
Scraper Porto de Lisboa — VERSÃO COM DIAGNÓSTICO ALARGADO
═══════════════════════════════════════════════════════════════
Mantém a estratégia da tabela React mas:
  • Captura screenshot em momentos-chave de cada página
    (chegadas e partidas), gravando-os em data/diagnostic/
  • Loga em detalhe o que vê em cada passo
  • Tem tempos de espera mais generosos
  • Tenta múltiplas estratégias para preencher datas
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


URL_ARRIVALS    = "https://www.portodelisboa.pt/previsao-de-chegadas"
URL_DEPARTURES  = "https://www.portodelisboa.pt/partidas"
URL_IN_PORT     = "https://www.portodelisboa.pt/navios-em-porto"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

WINDOW_DAYS_BACK    = 2
WINDOW_DAYS_FORWARD = 2

CRUISE_TERMINALS = {
    "santa apol":  "Santa Apolónia",
    "apolóni":     "Santa Apolónia",
    "apoloni":     "Santa Apolónia",
    "sotagus":     "Santa Apolónia",
    "jardim":      "Jardim do Tabaco",
    "tabaco":      "Jardim do Tabaco",
    "rocha":       "Rocha Conde Óbidos",
    "óbidos":      "Rocha Conde Óbidos",
    "obidos":      "Rocha Conde Óbidos",
    "alcântara":   "Alcântara",
    "alcantara":   "Alcântara",
    "liscont":     "Alcântara",
}

HAZARD_TERMINAL = "Terminal Multiusos do Poço do Bispo"
HAZARD_TERMINAL_KEYS = ["poço", "poco", "bispo", "tmpb", "multiusos"]

HAZARD_KEYWORDS = [
    "imdg", "químic", "quimic", "combust", "gnl", "glp", "gás", "gas",
    "tanker", "perigos", "hazard", "fuel", "oil", "petroleo", "petróleo",
    "petroleiro", "metano", "metanol", "etileno", "propano", "butano",
    "amónia", "amonia", "enxofre", "sulfur", "tanque", "crude",
    "abastecimento de combust",
]

CRUISE_TYPE_KEYWORDS = [
    "cruzeiro", "cruise", "passageiros", "passenger", "passageiro",
]

OUTPUT_PATH    = Path("data/lisbon_port.json")
DIAGNOSTIC_DIR = Path("data/diagnostic")


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_date_iso(date_str: str) -> Optional[str]:
    if not date_str:
        return None
    s = date_str.strip()

    mt = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[\sT](\d{1,2})[:h.](\d{2}))?", s)
    if mt:
        y, m, d = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        hh = int(mt.group(4)) if mt.group(4) else 0
        mm = int(mt.group(5)) if mt.group(5) else 0
        try:
            return datetime(y, m, d, hh, mm).isoformat()
        except ValueError:
            return None

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


def classify(term_str: str, motive_str: str, type_str: str) -> tuple[str, bool, bool]:
    t = (term_str or "").lower()
    motive = (motive_str or "").lower()
    ty = (type_str or "").lower()

    if any(k in t for k in HAZARD_TERMINAL_KEYS):
        return HAZARD_TERMINAL, True, False

    is_hazard_content = (
        any(k in ty for k in HAZARD_KEYWORDS)
        or any(k in motive for k in HAZARD_KEYWORDS)
    )
    if is_hazard_content:
        return HAZARD_TERMINAL, True, False

    is_cruise_type = any(k in ty for k in CRUISE_TYPE_KEYWORDS)

    matched_term = None
    for key, name in CRUISE_TERMINALS.items():
        if key in t:
            matched_term = name
            break

    if is_cruise_type:
        return matched_term or "Alcântara", False, True

    return matched_term or normalise(term_str) or "Outro", False, False


def fingerprint(name: str, terminal: str, date_iso: Optional[str], type_label: str) -> tuple:
    return (name.lower(), terminal.lower(), date_iso or "", type_label)


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


def snap(page: Page, name: str) -> None:
    """Tira screenshot e guarda em data/diagnostic/."""
    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    p = DIAGNOSTIC_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        print(f"      📸 {p}")
    except Exception as e:
        print(f"      ⚠ screenshot falhou: {e}")


def extract_react_rows(page: Page) -> list[dict]:
    return page.evaluate("""
        () => {
            const out = [];
            const headerCells = document.querySelectorAll('.rdt_TableHeadRow .rdt_TableCol');
            const headers = Array.from(headerCells).map(c =>
                c.textContent.trim().toLowerCase().replace(/\\s+/g, ' ')
            );
            const rowEls = document.querySelectorAll('.rdt_TableRow');
            rowEls.forEach(r => {
                const cells = Array.from(r.querySelectorAll('.rdt_TableCell'))
                    .map(c => c.textContent.trim());
                const obj = { _cells: cells, _headers: headers };
                headers.forEach((h, i) => {
                    if (h) obj[h] = cells[i] || '';
                });
                out.push(obj);
            });
            return out;
        }
    """)


def get_field(row: dict, *keys: str) -> str:
    for k in keys:
        for rk, rv in row.items():
            if rk.startswith("_"):
                continue
            if k in rk:
                return rv or ""
    return ""


def row_to_record(row: dict, default_type: str, source_label: str) -> Optional[dict]:
    name = get_field(row, "navio", "vessel", "ship", "nome")
    if not name:
        return None

    eta = get_field(row, "eta")
    etd = get_field(row, "etd")
    ata = get_field(row, "ata")
    atd = get_field(row, "atd")

    if default_type == "arrival":
        primary_date = eta or ata
    elif default_type == "departure":
        primary_date = atd or etd
    else:
        primary_date = ata or eta or atd

    type_str = get_field(row, "tipo de navio", "tipo")
    motive   = get_field(row, "motivo de escala", "motivo", "operação", "operacao")
    terminal = get_field(row, "local atribuido", "local", "terminal", "cais")

    terminal_norm, is_hazard, is_cruise = classify(terminal, motive, type_str)
    if not is_hazard and not is_cruise:
        return None

    iso_date = parse_date_iso(primary_date)
    if not iso_date:
        return None

    cargo_text = normalise(motive)
    if type_str:
        cargo_text = cargo_text + (" · " if cargo_text else "") + normalise(type_str)

    return {
        "name":      normalise(name),
        "line":      "",
        "type":      "hazard" if is_hazard else default_type,
        "terminal":  terminal_norm,
        "from":      "",
        "to":        "",
        "date":      iso_date,
        "hour":      hour_from_iso(iso_date),
        "pax":       0,
        "cargo":     cargo_text,
        "is_hazard": is_hazard,
        "ship_type": normalise(type_str),
        "source":    source_label,
    }


# ════════════════════════════════════════════════════════════
# PREENCHER DATAS — múltiplas estratégias
# ════════════════════════════════════════════════════════════
def fill_date_inputs(page: Page, d_start: str, d_end: str) -> bool:
    """
    Tenta várias formas de preencher as datas.
    Devolve True se conseguiu.
    """
    try:
        date_inputs = page.locator("input[type='date']")
        count = date_inputs.count()
        print(f"      [datas] inputs type=date encontrados: {count}")

        if count >= 2:
            # Estratégia 1: fill simples
            date_inputs.nth(0).fill(d_start)
            date_inputs.nth(1).fill(d_end)
            page.wait_for_timeout(500)

            # Verificar se ficaram preenchidos
            v0 = date_inputs.nth(0).input_value()
            v1 = date_inputs.nth(1).input_value()
            print(f"      [datas] após fill: '{v0}' → '{v1}'")

            if v0 == d_start and v1 == d_end:
                return True

            # Estratégia 2: forçar via JavaScript
            page.evaluate(f"""
                () => {{
                    const inputs = document.querySelectorAll('input[type="date"]');
                    if (inputs.length >= 2) {{
                        const setVal = (el, v) => {{
                            const native = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            native.call(el, v);
                            el.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }};
                        setVal(inputs[0], '{d_start}');
                        setVal(inputs[1], '{d_end}');
                    }}
                }}
            """)
            page.wait_for_timeout(500)
            v0 = date_inputs.nth(0).input_value()
            v1 = date_inputs.nth(1).input_value()
            print(f"      [datas] após JS: '{v0}' → '{v1}'")
            return (v0 == d_start and v1 == d_end)

        # Sem inputs type=date — tentar input genérico
        all_inputs = page.locator("input")
        print(f"      [datas] inputs totais na página: {all_inputs.count()}")
        return False
    except Exception as e:
        print(f"      [datas] excepção: {e}")
        return False


# ════════════════════════════════════════════════════════════
# SCRAPING POR PÁGINA
# ════════════════════════════════════════════════════════════
def scrape_in_port(page: Page, today: datetime) -> list[dict]:
    label = "in_port"
    print(f"\n[{label}] {URL_IN_PORT}")
    page.goto(URL_IN_PORT, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)
    page.wait_for_timeout(2000)

    try:
        page.wait_for_selector(".rdt_TableRow", timeout=20_000)
    except PWTimeout:
        print("    ✗ tabela não apareceu")
        snap(page, f"{label}_no_table")
        return []

    page.wait_for_timeout(2000)
    rows = extract_react_rows(page)
    print(f"    ✓ {len(rows)} linhas brutas")
    if rows:
        print(f"    cabeçalhos: {rows[0].get('_headers')}")

    out = []
    for row in rows:
        rec = row_to_record(row, "transit", label)
        if rec:
            out.append(rec)
    print(f"    → {len(out)} registos relevantes")
    return out


def scrape_search_page(page: Page, url: str, label: str, default_type: str,
                       today: datetime) -> list[dict]:
    print(f"\n[{label}] {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)
    page.wait_for_timeout(2000)

    snap(page, f"{label}_1_initial")

    d_start = (today - timedelta(days=WINDOW_DAYS_BACK)).strftime("%Y-%m-%d")
    d_end   = (today + timedelta(days=WINDOW_DAYS_FORWARD)).strftime("%Y-%m-%d")
    print(f"    intervalo desejado: {d_start} → {d_end}")

    filled = fill_date_inputs(page, d_start, d_end)
    print(f"    datas preenchidas: {filled}")

    snap(page, f"{label}_2_after_fill")

    # Clicar Pesquisar
    try:
        search_btn = page.locator("button:has-text('Pesquisar')").first
        if search_btn and search_btn.is_visible(timeout=3000):
            search_btn.click(timeout=5000)
            print("    ✓ click em Pesquisar")
            # Esperar resposta — pode demorar até 8s
            page.wait_for_timeout(5000)
        else:
            print("    ⚠ botão Pesquisar não visível")
    except Exception as e:
        print(f"    ⚠ erro a clicar Pesquisar: {e}")

    snap(page, f"{label}_3_after_search")

    # Esperar pela tabela. Aceitar tanto linhas como mensagem "Não há registo".
    has_rows = False
    try:
        page.wait_for_selector(".rdt_TableRow, :text('Não há registo')", timeout=20_000)
        has_rows = page.locator(".rdt_TableRow").count() > 0
    except PWTimeout:
        print("    ✗ nem tabela nem mensagem 'Não há registo' apareceram")
        snap(page, f"{label}_4_timeout")

    if not has_rows:
        # Verificar se aparece mensagem de "sem dados"
        body_text = page.locator("body").inner_text()
        if "Não há registo" in body_text:
            print("    (página informa 'Não há registo para exibir')")
        else:
            # Conta tabelas e linhas para diagnóstico
            n_rows = page.locator(".rdt_TableRow").count()
            n_thead = page.locator(".rdt_TableHeadRow").count()
            n_tableEls = page.locator("[class*='rdt_Table']").count()
            print(f"    rdt_TableRow={n_rows} rdt_TableHeadRow={n_thead} qualquer rdt_Table*={n_tableEls}")
        return []

    page.wait_for_timeout(2000)
    rows = extract_react_rows(page)
    print(f"    ✓ {len(rows)} linhas brutas")
    if rows:
        print(f"    cabeçalhos: {rows[0].get('_headers')}")
        # Mostra primeiras 2 linhas para diagnóstico
        for i, r in enumerate(rows[:2]):
            print(f"      linha[{i}] cells: {r.get('_cells')}")

    out = []
    for row in rows:
        rec = row_to_record(row, default_type, label)
        if rec:
            out.append(rec)
    print(f"    → {len(out)} registos relevantes")
    return out


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now()
    print(f"=== Scraper Porto de Lisboa @ {fetched_at} ===")

    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pt-PT",
            viewport={"width": 1400, "height": 1000},
        )
        page = context.new_page()

        try:
            recs = scrape_in_port(page, today)
            all_records.extend(recs)
        except Exception as e:
            msg = f"in_port: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}")
            errors.append(msg)

        try:
            recs = scrape_search_page(page, URL_ARRIVALS, "arrivals", "arrival", today)
            all_records.extend(recs)
        except Exception as e:
            msg = f"arrivals: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}")
            errors.append(msg)

        try:
            recs = scrape_search_page(page, URL_DEPARTURES, "departures", "departure", today)
            all_records.extend(recs)
        except Exception as e:
            msg = f"departures: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}")
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

    print(f"\n=== Total filtrado: {len(filtered)} (cruz={len(ships)} haz={len(hazards)}) ===")

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
    print(f"✓ {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
