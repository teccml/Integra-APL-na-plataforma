"""
Scraper Porto de Lisboa — VERSÃO REVISTA
═══════════════════════════════════════════════════════════════
Estratégia única e fiável: parsing da tabela React em todas as
3 páginas. Já não tenta descarregar CSV.

  • Navios em Porto    → tabela React directa (.rdt_TableRow)
  • Previsão Chegadas  → tabela React directa (.rdt_TableRow)
  • Partidas           → tabela React directa (.rdt_TableRow)

Janela temporal: hoje-2 a hoje+2 dias.

Output: data/lisbon_port.json com todos os navios normalizados.
"""

from __future__ import annotations

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

WINDOW_DAYS_BACK    = 2
WINDOW_DAYS_FORWARD = 2

# Mapeamento de terminais
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

# Palavras-chave alargadas (TIPO DE NAVIO + MOTIVO DE ESCALA + carga)
HAZARD_KEYWORDS = [
    "imdg", "químic", "quimic", "combust", "gnl", "glp", "gás", "gas",
    "tanker", "perigos", "hazard", "fuel", "oil", "petroleo", "petróleo",
    "petroleiro", "metano", "metanol", "etileno", "propano", "butano",
    "amónia", "amonia", "enxofre", "sulfur", "tanque", "crude",
    "abastecimento de combust",
]

# Tipos de navio que classificamos como "cruzeiro"
CRUISE_TYPE_KEYWORDS = [
    "cruzeiro", "cruise", "passageiros", "passenger", "passageiro",
]

OUTPUT_PATH = Path("data/lisbon_port.json")


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_date_iso(date_str: str) -> Optional[str]:
    """Aceita yyyy-mm-dd hh:mm, yyyy-mm-dd, dd/mm/yyyy [hh:mm], dd-mm-yyyy [hh:mm]."""
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
    """
    Devolve (terminal_normalizado, é_perigoso, é_cruzeiro).

    Lógica:
      1) Terminal IMDG (Poço do Bispo / TMPB) → IMDG
      2) Tipo OU motivo contém palavra-chave IMDG → IMDG
      3) Tipo é cruzeiro/passageiros → cruzeiro (no terminal correspondente)
      4) Terminal é de cruzeiros → cruzeiro
      5) Outro → descartar
    """
    t = (term_str or "").lower()
    motive = (motive_str or "").lower()
    ty = (type_str or "").lower()

    # 1. Terminal IMDG explícito
    if any(k in t for k in HAZARD_TERMINAL_KEYS):
        return HAZARD_TERMINAL, True, False

    # 2. Carga / tipo IMDG
    is_hazard_content = (
        any(k in ty for k in HAZARD_KEYWORDS)
        or any(k in motive for k in HAZARD_KEYWORDS)
    )
    if is_hazard_content:
        return HAZARD_TERMINAL, True, False

    # 3. Cruzeiro pelo tipo
    is_cruise_type = any(k in ty for k in CRUISE_TYPE_KEYWORDS)

    # 4. Tentar mapear terminal de cruzeiros
    matched_term = None
    for key, name in CRUISE_TERMINALS.items():
        if key in t:
            matched_term = name
            break

    if is_cruise_type:
        return matched_term or "Alcântara", False, True
    if matched_term and is_cruise_type:
        return matched_term, False, True

    # 5. Não é IMDG nem cruzeiro
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
# EXTRACÇÃO DA TABELA REACT
# ════════════════════════════════════════════════════════════
def extract_react_rows(page: Page) -> list[dict]:
    """
    Devolve a tabela React como lista de dicts:
      {
        'navio': 'MSC OPERA',
        'eta':   '2026-05-08 07:31',
        ...
      }
    Os nomes das chaves são os cabeçalhos em minúsculas.
    """
    return page.evaluate("""
        () => {
            const out = [];
            // Cabeçalhos
            const headerCells = document.querySelectorAll('.rdt_TableHeadRow .rdt_TableCol');
            const headers = Array.from(headerCells).map(c => {
                // Limpar — alguns headers têm ícones embutidos
                return c.textContent.trim().toLowerCase()
                    .replace(/\\s+/g, ' ');
            });

            // Linhas
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
    """Procura por chave parcial nos headers da row."""
    for k in keys:
        for rk, rv in row.items():
            if rk.startswith("_"):
                continue
            if k in rk:
                return rv or ""
    return ""


def row_to_record(row: dict, default_type: str, source_label: str) -> Optional[dict]:
    """Converte uma row da tabela React num registo normalizado."""
    name = get_field(row, "navio", "vessel", "ship", "nome")
    if not name:
        return None

    # Datas: depende da página
    # - Chegadas:  ETA (1ª) e ETD (2ª)
    # - Partidas:  ATD (1ª) e ATA (2ª)
    # - In port:   ATA (1ª) e ETD (2ª)
    eta = get_field(row, "eta")
    etd = get_field(row, "etd")
    ata = get_field(row, "ata")
    atd = get_field(row, "atd")

    # Para chegadas: usar ETA (chegada prevista)
    # Para partidas: usar ATD (saída efectiva)
    # Para in_port: usar ATA (entrada efectiva)
    primary_date = None
    if default_type == "arrival":
        primary_date = eta or ata
    elif default_type == "departure":
        primary_date = atd or etd
    else:  # transit / in_port
        primary_date = ata or eta or atd

    type_str   = get_field(row, "tipo de navio", "tipo")
    motive     = get_field(row, "motivo de escala", "motivo", "operação", "operacao")
    terminal   = get_field(row, "local atribuido", "local", "terminal", "cais")

    terminal_norm, is_hazard, is_cruise = classify(terminal, motive, type_str)

    # Filtramos: só cruzeiros e IMDG
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
# SCRAPING POR PÁGINA
# ════════════════════════════════════════════════════════════
def scrape_page(page: Page, url: str, label: str, default_type: str,
                today: datetime, fill_dates: bool = False) -> list[dict]:
    print(f"[{label}] {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)
    page.wait_for_timeout(2000)

    # Para chegadas e partidas, podemos opcionalmente forçar o intervalo
    # de datas que queremos. Mas como o site já vem com datas pré-preenchidas
    # que normalmente cobrem +/- 3 dias, os dados já lá estão à partida.
    # Se fill_dates for True, garantimos que cobrem ±2 dias.
    if fill_dates:
        d_start = (today - timedelta(days=WINDOW_DAYS_BACK)).strftime("%Y-%m-%d")
        d_end   = (today + timedelta(days=WINDOW_DAYS_FORWARD)).strftime("%Y-%m-%d")
        print(f"    a forçar intervalo: {d_start} → {d_end}")
        try:
            date_inputs = page.locator("input[type='date']")
            if date_inputs.count() >= 2:
                date_inputs.nth(0).fill(d_start)
                date_inputs.nth(1).fill(d_end)
                page.locator("button:has-text('Pesquisar')").first.click(timeout=5000)
                page.wait_for_timeout(3000)
                print(f"    ✓ pesquisa submetida")
        except Exception as e:
            print(f"    ⚠ não consegui preencher datas: {e}")

    # Esperar pela tabela
    try:
        page.wait_for_selector(".rdt_TableRow", timeout=15_000)
    except PWTimeout:
        print("    (sem linhas — página vazia)")
        return []

    page.wait_for_timeout(2000)

    raw_rows = extract_react_rows(page)
    print(f"    {len(raw_rows)} linhas brutas (cabeçalhos: {raw_rows[0].get('_headers') if raw_rows else 'n/a'})")

    out = []
    for row in raw_rows:
        rec = row_to_record(row, default_type, label)
        if rec:
            out.append(rec)
    print(f"    {len(out)} registos relevantes (cruzeiros + IMDG)")
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
        )
        page = context.new_page()

        # 1. Navios em Porto
        try:
            recs = scrape_page(page, URL_IN_PORT, "in_port", "transit", today, fill_dates=False)
            all_records.extend(recs)
            print()
        except Exception as e:
            msg = f"in_port: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}\n")
            errors.append(msg)

        # 2. Previsão de Chegadas (com forçar datas para garantir cobertura ±2d)
        try:
            recs = scrape_page(page, URL_ARRIVALS, "arrivals", "arrival", today, fill_dates=True)
            all_records.extend(recs)
            print()
        except Exception as e:
            msg = f"arrivals: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}\n")
            errors.append(msg)

        # 3. Partidas (com forçar datas)
        try:
            recs = scrape_page(page, URL_DEPARTURES, "departures", "departure", today, fill_dates=True)
            all_records.extend(recs)
            print()
        except Exception as e:
            msg = f"departures: {type(e).__name__}: {e}"
            print(f"    ✗ {msg}\n")
            errors.append(msg)

        browser.close()

    # Filtrar pela janela ±2 dias e deduplicar
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

    print(f"=== Total filtrado (±2d, deduplicado): {len(filtered)} ===")
    print(f"  cruzeiros:        {len(ships)}")
    print(f"  matérias perig.:  {len(hazards)}")
    if errors:
        print(f"  erros:            {len(errors)}")
        for e in errors:
            print(f"    - {e}")

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
