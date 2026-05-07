"""
Scraper Porto de Lisboa — VERSÃO 5 (campos correctos)
═══════════════════════════════════════════════════════════════
Baseado nos campos REAIS devolvidos pela API JSON-WS, identi-
ficados via inspecção do _raw_sample.json:

  • zona                 → terminal (ex.: "CAIS DE STª APOLONIA/...")
  • motivoEntrada        → motivo (Turismo, Descarga, Carga, etc.)
  • tipoNavioContrucao   → tipo do navio (Cruzeiro, Petroleiro...)
  • nv_tipoNavio         → tipo em maiúsculas (CRUZEIROS)
  • agente               → agência local
  • nv_armador           → armador real
  • portoProcedencia     → origem
  • portoDestino         → destino
  • passageirosTra/Emb/Des → passageiros (cálculo composto)
  • bandeira             → bandeira do navio
  • imo                  → número IMO

Output: data/lisbon_port.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


BASE = "https://www.portodelisboa.pt"

URL_ARRIVALS    = f"{BASE}/previsao-de-chegadas"
URL_DEPARTURES  = f"{BASE}/partidas"
URL_IN_PORT     = f"{BASE}/navios-em-porto"
API_ENDPOINT    = f"{BASE}/api/jsonws/invoke"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

API_PATH_ARRIVALS   = "/apl.processosweb/get-chegadas"
API_PATH_DEPARTURES = "/apl.processosweb/get-partidas"
API_PATH_IN_PORT_CANDIDATES = [
    "/apl.processosweb/get-navios-em-porto",
    "/apl.processosweb/get-navios-porto",
    "/apl.processosweb/get-em-porto",
    "/apl.processosweb/get-naviosemporto",
]

WINDOW_DAYS_BACK    = 2
WINDOW_DAYS_FORWARD = 2

# ════════════════════════════════════════════════════════════
# MAPEAMENTO DE TERMINAIS
# Os valores no campo `zona` aparecem em maiúsculas, ex.:
#   "CAIS DE STª APOLONIA/TERMINAL DE PASSAGEIROS"
#   "TERMINAL MULTIPURPOSE DE LISBOA"
#   "TERM CONTENT ALCANTARA / LISCONT"
#   "CAIS DA ROCHA/TERMINAL DE PASSAGEIROS"
#   "CAIS DO POÇO DO BISPO / TMPB"
# Procuramos por palavras-chave (lowercase) dentro da zona.
# ════════════════════════════════════════════════════════════
CRUISE_TERMINALS = {
    "santa apol":   "Santa Apolónia",
    "stª apol":     "Santa Apolónia",
    "sta apol":     "Santa Apolónia",
    "apolóni":      "Santa Apolónia",
    "apoloni":      "Santa Apolónia",
    "sotagus":      "Santa Apolónia",
    "jardim do tab":"Jardim do Tabaco",
    "jardim tabaco":"Jardim do Tabaco",
    "tabaco":       "Jardim do Tabaco",
    "rocha":        "Rocha Conde Óbidos",
    "óbidos":       "Rocha Conde Óbidos",
    "obidos":       "Rocha Conde Óbidos",
    "alcântara":    "Alcântara",
    "alcantara":    "Alcântara",
    "liscont":      "Alcântara",
}

HAZARD_TERMINAL = "Terminal Multiusos do Poço do Bispo"
HAZARD_TERMINAL_KEYS = ["poço", "poco", "bispo", "tmpb", "multiusos", "multipurpose"]

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
def normalise(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def parse_date_iso(date_str: Any) -> Optional[str]:
    if not date_str:
        return None
    s = str(date_str).strip()
    # API devolve "yyyy-mm-dd hh:mm:ss.0"
    mt = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[\sT](\d{1,2}):(\d{2})(?::(\d{1,2}))?)?", s)
    if mt:
        try:
            y, m, d = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
            hh = int(mt.group(4)) if mt.group(4) else 0
            mm = int(mt.group(5)) if mt.group(5) else 0
            return datetime(y, m, d, hh, mm).isoformat()
        except ValueError:
            return None

    mt = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})(?:\s+(\d{1,2})[:h.](\d{2}))?", s)
    if mt:
        try:
            d, m, y = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
            hh = int(mt.group(4)) if mt.group(4) else 0
            mm = int(mt.group(5)) if mt.group(5) else 0
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


def match_cruise_terminal(text: str) -> Optional[str]:
    t = (text or "").lower()
    for key, name in CRUISE_TERMINALS.items():
        if key in t:
            return name
    return None


def classify(zone_str: str, motive_str: str, type_str: str) -> tuple[str, bool, bool]:
    """Devolve (terminal_normalizado, é_perigoso, é_cruzeiro)."""
    z = (zone_str or "").lower()
    motive = (motive_str or "").lower()
    ty = (type_str or "").lower()

    if any(k in z for k in HAZARD_TERMINAL_KEYS):
        return HAZARD_TERMINAL, True, False

    if any(k in ty for k in HAZARD_KEYWORDS) or any(k in motive for k in HAZARD_KEYWORDS):
        return HAZARD_TERMINAL, True, False

    is_cruise_type = any(k in ty for k in CRUISE_TYPE_KEYWORDS)
    matched = match_cruise_terminal(zone_str)

    if is_cruise_type or matched:
        return matched or "Alcântara", False, True

    return matched or normalise(zone_str) or "Outro", False, False


def fingerprint(name: str, terminal: str, date_iso: Optional[str], type_label: str) -> tuple:
    return (name.lower(), terminal.lower(), date_iso or "", type_label)


def accept_cookies(page: Page) -> bool:
    try:
        btn = page.locator("button:has-text('Aceitar')").first
        if btn and btn.is_visible(timeout=2000):
            btn.click(timeout=2000)
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


def grab_session(page: Page, url: str) -> Optional[str]:
    print(f"  [sessão] a abrir {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)
    page.wait_for_timeout(2500)

    token = page.evaluate("""
        () => {
            const meta = document.querySelector('meta[name="csrf-token"]')
                      || document.querySelector('meta[name="X-CSRF-Token"]');
            if (meta) return meta.getAttribute('content');
            if (window.Liferay && Liferay.authToken) return Liferay.authToken;
            for (const s of document.scripts) {
                const m = (s.textContent || '').match(/authToken\\s*[:=]\\s*['\"]([A-Za-z0-9]+)['\"]/);
                if (m) return m[1];
                const m2 = (s.textContent || '').match(/csrfToken\\s*[:=]\\s*['\"]([A-Za-z0-9]+)['\"]/i);
                if (m2) return m2[1];
            }
            return null;
        }
    """)
    print(f"  [sessão] token: {(token[:20] + '...') if token else 'NÃO ENCONTRADO'}")
    return token


def call_api(page: Page, api_path: str, payload: dict, token: Optional[str]) -> Any:
    body_obj = {api_path: payload}
    body_json = json.dumps(body_obj, ensure_ascii=False)
    print(f"  [API] POST {api_path}")

    headers = {
        "Accept":       "*/*",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin":       BASE,
    }
    if token:
        headers["x-csrf-token"] = token

    result = page.evaluate("""
        async ({ url, body, headers }) => {
            try {
                const r = await fetch(url, { method:'POST', headers, body, credentials:'include' });
                return { status: r.status, body: await r.text() };
            } catch (e) { return { status: 0, error: String(e) }; }
        }
    """, {"url": API_ENDPOINT, "body": body_json, "headers": headers})

    status = result.get("status", 0)
    if status != 200:
        print(f"  [API] HTTP {status}")
        return None
    txt = result.get("body", "")
    print(f"  [API] resposta {len(txt)} bytes")
    if not txt:
        return None
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return None


def coerce_records(payload: Any) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if "exception" in payload:
            return []
        for key in ("data", "rows", "result", "results", "navios", "items", "list"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        for v in payload.values():
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                inner = coerce_records(v)
                if inner:
                    return inner
    return []


def field(d: dict, *keys: str) -> str:
    """Procura case-insensitive, exact-match primeiro."""
    if not d:
        return ""
    lower = {k.lower(): v for k, v in d.items() if isinstance(k, str)}
    for k in keys:
        kl = k.lower()
        if kl in lower:
            v = lower[kl]
            if v is not None and str(v).strip():
                return str(v)
    return ""


def int_field(d: dict, *keys: str) -> int:
    s = field(d, *keys)
    if not s:
        return 0
    digits = re.sub(r"\D", "", s)
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


# ════════════════════════════════════════════════════════════
# CONVERSÃO API → REGISTO NORMALIZADO
# (campos REAIS conforme _raw_sample.json)
# ════════════════════════════════════════════════════════════
def record_from_api(d: dict, default_type: str, source_label: str) -> Optional[dict]:
    name = field(d, "navio", "nv_nome", "nv_nomeAbrev")
    if not name:
        return None

    eta = field(d, "eta")
    etd = field(d, "etd")
    ata = field(d, "ata")
    atd = field(d, "atd")

    if default_type == "arrival":
        primary = eta or ata
    elif default_type == "departure":
        primary = atd or etd
    else:
        primary = ata or eta or atd

    iso_date = parse_date_iso(primary)
    if not iso_date:
        return None

    # Campos certos da API
    type_str = field(d, "tipoNavioContrucao", "nv_tipoNavio")
    motive   = field(d, "motivoEntrada")
    zone     = field(d, "zona")  # ◄── este é o terminal!

    # Companhia: prefere o agente local, depois o armador
    company  = field(d, "agente", "nv_armador")

    # Origem / Destino
    frm      = field(d, "portoProcedencia")
    to_      = field(d, "portoDestino")

    # Passageiros: total a bordo = trânsito + max(embarque, desembarque)
    pax_emb  = int_field(d, "passageirosEmb")
    pax_des  = int_field(d, "passageirosDes")
    pax_tra  = int_field(d, "passageirosTra")
    pax      = pax_tra + max(pax_emb, pax_des)

    flag     = field(d, "bandeira")
    imo      = field(d, "imo", "nv_numImo")

    terminal_norm, is_hazard, is_cruise = classify(zone, motive, type_str)
    if not is_hazard and not is_cruise:
        return None

    cargo_text = normalise(motive)
    if type_str:
        cargo_text = cargo_text + (" · " if cargo_text else "") + normalise(type_str)

    return {
        "name":       normalise(name),
        "line":       normalise(company),
        "type":       "hazard" if is_hazard else default_type,
        "terminal":   terminal_norm,
        "from":       normalise(frm),
        "to":         normalise(to_),
        "date":       iso_date,
        "hour":       hour_from_iso(iso_date),
        "pax":        pax,
        "cargo":      cargo_text,
        "is_hazard":  is_hazard,
        "ship_type":  normalise(type_str),
        "flag":       normalise(flag),
        "imo":        normalise(imo),
        "source":     source_label,
    }


# ════════════════════════════════════════════════════════════
# FALLBACK: tabela React (apenas para in_port)
# ════════════════════════════════════════════════════════════
def parse_react_table_records(page: Page, default_type: str, source_label: str) -> list[dict]:
    rows = page.evaluate("""
        () => {
            const out = [];
            const headerCells = document.querySelectorAll('.rdt_TableHeadRow .rdt_TableCol');
            const headers = Array.from(headerCells).map(c =>
                c.textContent.trim().toLowerCase().replace(/\\s+/g, ' '));
            document.querySelectorAll('.rdt_TableRow').forEach(r => {
                const cells = Array.from(r.querySelectorAll('.rdt_TableCell'))
                    .map(c => c.textContent.trim());
                const obj = {};
                headers.forEach((h, i) => { if (h) obj[h] = cells[i] || ''; });
                out.push(obj);
            });
            return out;
        }
    """)
    out = []
    for row in rows:
        # Campos visíveis na tabela "Navios em Porto"
        name = row.get("nome do navio", "") or row.get("nome do navio▲", "")
        if not name:
            continue
        date_in = row.get("ata", "") or row.get("ata▲", "")
        type_str = row.get("tipo de navio", "") or row.get("tipo de navio▲", "")
        motive = row.get("motivo de escala", "") or row.get("motivo de escala▲", "")
        zone = row.get("local atribuido", "") or row.get("local atribuido▲", "")

        terminal_norm, is_hazard, is_cruise = classify(zone, motive, type_str)
        if not is_hazard and not is_cruise:
            continue

        iso_date = parse_date_iso(date_in)
        if not iso_date:
            continue

        cargo_text = normalise(motive)
        if type_str:
            cargo_text = cargo_text + (" · " if cargo_text else "") + normalise(type_str)

        out.append({
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
            "flag":      "",
            "imo":       "",
            "source":    source_label,
        })
    return out


# ════════════════════════════════════════════════════════════
# SCRAPING POR PÁGINA
# ════════════════════════════════════════════════════════════
def scrape_via_api(page: Page, page_url: str, label: str, default_type: str,
                   api_paths: list[str], api_payload: dict) -> list[dict]:
    print(f"\n[{label}] {page_url}")
    token = grab_session(page, page_url)

    for api_path in api_paths:
        result = call_api(page, api_path, api_payload, token)
        records = coerce_records(result)
        if records:
            print(f"  [{label}] API '{api_path}' devolveu {len(records)} registos")
            out = []
            for r in records:
                rec = record_from_api(r, default_type, label)
                if rec:
                    out.append(rec)
            print(f"  [{label}] {len(out)} relevantes (cruzeiros + IMDG)")
            return out

    print(f"  [{label}] fallback: tabela React")
    try:
        page.wait_for_selector(".rdt_TableRow", timeout=15_000)
        page.wait_for_timeout(1500)
        out = parse_react_table_records(page, default_type, label)
        print(f"  [{label}] fallback devolveu {len(out)} registos")
        return out
    except PWTimeout:
        return []


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════
def main() -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now()
    print(f"=== Scraper Porto de Lisboa @ {fetched_at} ===")

    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)

    d_start = (today - timedelta(days=WINDOW_DAYS_BACK)).strftime("%Y-%m-%d")
    d_end   = (today + timedelta(days=WINDOW_DAYS_FORWARD)).strftime("%Y-%m-%d")
    print(f"intervalo: {d_start} → {d_end}\n")

    payload_dates = {"dataIni": d_start, "dataFim": d_end}

    all_records: list[dict] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT, locale="pt-PT",
            viewport={"width": 1400, "height": 1000},
        )
        page = context.new_page()

        try:
            recs = scrape_via_api(page, URL_ARRIVALS, "arrivals", "arrival",
                                  [API_PATH_ARRIVALS], payload_dates)
            all_records.extend(recs)
        except Exception as e:
            errors.append(f"arrivals: {type(e).__name__}: {e}")

        try:
            recs = scrape_via_api(page, URL_DEPARTURES, "departures", "departure",
                                  [API_PATH_DEPARTURES], payload_dates)
            all_records.extend(recs)
        except Exception as e:
            errors.append(f"departures: {type(e).__name__}: {e}")

        try:
            recs = scrape_via_api(page, URL_IN_PORT, "in_port", "transit",
                                  API_PATH_IN_PORT_CANDIDATES, {})
            all_records.extend(recs)
        except Exception as e:
            errors.append(f"in_port: {type(e).__name__}: {e}")

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

    print(f"\n=== Total: {len(filtered)} (cruz={len(ships)} haz={len(hazards)}) ===")

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
