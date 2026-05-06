"""
Scraper Porto de Lisboa — VIA API JSON
═══════════════════════════════════════════════════════════════
Estratégia definitiva:

  1. Abre a página com Playwright para obter o X-CSRF-Token e
     cookies de sessão (gerados por JavaScript no cliente).
  2. Faz POST directo ao endpoint /api/jsonws/invoke com o
     payload JSON apropriado para cada tipo de dados.
  3. Faz parse da resposta JSON.

Endpoints descobertos:
  • /apl.processosweb/get-chegadas      (chegadas)
  • /apl.processosweb/get-partidas      (partidas — assumido)
  • /apl.processosweb/get-navios-porto  (em porto — assumido)

Se algum dos endpoints "assumidos" estiver errado, testamos
alternativas comuns; em último recurso usamos parsing da
tabela React como fallback.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


# ════════════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ════════════════════════════════════════════════════════════
BASE = "https://www.portodelisboa.pt"

URL_ARRIVALS    = f"{BASE}/previsao-de-chegadas"
URL_DEPARTURES  = f"{BASE}/partidas"
URL_IN_PORT     = f"{BASE}/navios-em-porto"
API_ENDPOINT    = f"{BASE}/api/jsonws/invoke"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# Endpoints da API JSON-WS
API_PATH_ARRIVALS   = "/apl.processosweb/get-chegadas"
API_PATH_DEPARTURES = "/apl.processosweb/get-partidas"
# Para "Navios em Porto" experimentamos várias hipóteses
API_PATH_IN_PORT_CANDIDATES = [
    "/apl.processosweb/get-navios-porto",
    "/apl.processosweb/get-navios-em-porto",
    "/apl.processosweb/get-naviosporto",
    "/apl.processosweb/get-navios",
]

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


def parse_date_iso(date_str: Any) -> Optional[str]:
    """Aceita strings em vários formatos e devolve ISO8601."""
    if not date_str:
        return None
    s = str(date_str).strip()

    # Já vem em ISO?
    mt = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[\sT](\d{1,2})[:h.](\d{2})(?::(\d{1,2}))?)?", s)
    if mt:
        try:
            y, m, d = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
            hh = int(mt.group(4)) if mt.group(4) else 0
            mm = int(mt.group(5)) if mt.group(5) else 0
            return datetime(y, m, d, hh, mm).isoformat()
        except ValueError:
            return None

    # dd/mm/yyyy ou dd-mm-yyyy
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


def classify(term_str: str, motive_str: str, type_str: str) -> tuple[str, bool, bool]:
    """Devolve (terminal, é_perigoso, é_cruzeiro)."""
    t = (term_str or "").lower()
    motive = (motive_str or "").lower()
    ty = (type_str or "").lower()

    if any(k in t for k in HAZARD_TERMINAL_KEYS):
        return HAZARD_TERMINAL, True, False

    if any(k in ty for k in HAZARD_KEYWORDS) or any(k in motive for k in HAZARD_KEYWORDS):
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
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


# ════════════════════════════════════════════════════════════
# SESSÃO: token + cookies + chamada à API
# ════════════════════════════════════════════════════════════
def grab_session(page: Page, url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Vai à página, espera a renderização, e extrai:
      • x-csrf-token (de meta tag, atributo, ou variável global)
      • cookie string completa para usar em pedidos seguintes
    Devolve (token, cookie_str). Pode devolver (None, cookie) se
    não conseguir extrair token — nesse caso o site às vezes
    aceita o pedido na mesma se a sessão tiver cookies válidos.
    """
    print(f"  [sessão] a abrir {url}")
    page.goto(url, wait_until="networkidle", timeout=60_000)
    accept_cookies(page)
    page.wait_for_timeout(2500)

    token = page.evaluate("""
        () => {
            // 1. Token em meta tag (Liferay padrão)
            const meta = document.querySelector('meta[name="csrf-token"]')
                      || document.querySelector('meta[name="X-CSRF-Token"]');
            if (meta) return meta.getAttribute('content');

            // 2. Variável global Liferay
            if (window.Liferay && Liferay.authToken) return Liferay.authToken;

            // 3. Em scripts inline procuramos "Liferay.authToken = '...'"
            for (const s of document.scripts) {
                const m = (s.textContent || '').match(
                    /authToken\\s*[:=]\\s*['\"]([A-Za-z0-9]+)['\"]/);
                if (m) return m[1];
                const m2 = (s.textContent || '').match(
                    /csrfToken\\s*[:=]\\s*['\"]([A-Za-z0-9]+)['\"]/i);
                if (m2) return m2[1];
            }
            return null;
        }
    """)

    cookies = page.context.cookies()
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    print(f"  [sessão] token: {(token[:20] + '...') if token else 'NÃO ENCONTRADO'}")
    print(f"  [sessão] cookies: {len(cookies)} valores")
    return token, cookie_str


def call_api(page: Page, api_path: str, payload: dict, token: Optional[str], cookie_str: str) -> Any:
    """
    Chama o endpoint /api/jsonws/invoke usando o motor de fetch
    do próprio browser (Playwright). Devolve o JSON ou None.

    Usar fetch dentro do browser tem 2 vantagens:
      • já tem o user-agent e contexto da sessão activa;
      • dispensa ter de replicar TLS/cookies/headers manualmente.
    """
    body_obj = { api_path: payload }
    body_json = json.dumps(body_obj, ensure_ascii=False)
    print(f"  [API] POST {api_path} body={body_json}")

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
                const r = await fetch(url, {
                    method:      'POST',
                    headers:     headers,
                    body:        body,
                    credentials: 'include',
                });
                const txt = await r.text();
                return { status: r.status, body: txt };
            } catch (e) {
                return { status: 0, error: String(e) };
            }
        }
    """, {"url": API_ENDPOINT, "body": body_json, "headers": headers})

    status = result.get("status", 0)
    if status != 200:
        print(f"  [API] HTTP {status} — {result.get('error') or result.get('body','')[:200]}")
        return None

    txt = result.get("body", "")
    print(f"  [API] resposta {len(txt)} bytes")
    if not txt:
        return None
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        print(f"  [API] JSON inválido: {e} — preview: {txt[:200]}")
        return None


# ════════════════════════════════════════════════════════════
# NORMALIZAÇÃO DOS REGISTOS
# ════════════════════════════════════════════════════════════
def coerce_records(payload: Any) -> list[dict]:
    """
    Liferay JSON-WS pode devolver:
      • lista directa de objectos
      • objecto com chave 'data', 'rows', 'result', 'navios'…
      • mensagem de erro (objecto com 'exception')
    Devolve sempre uma lista (eventualmente vazia) de dicts.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if "exception" in payload:
            print(f"    erro API: {payload.get('exception')}")
            return []
        for key in ("data", "rows", "result", "results", "navios", "items", "list"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        # Pode estar dentro do api_path como chave
        for v in payload.values():
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                inner = coerce_records(v)
                if inner:
                    return inner
    return []


def field(d: dict, *keys: str) -> str:
    """Procura chaves de forma case-insensitive e parcial."""
    if not d:
        return ""
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        kl = k.lower()
        if kl in lower:
            v = lower[kl]
            return "" if v is None else str(v)
    # match parcial
    for k in keys:
        kl = k.lower()
        for rk, rv in lower.items():
            if kl in rk:
                return "" if rv is None else str(rv)
    return ""


def record_from_api(d: dict, default_type: str, source_label: str) -> Optional[dict]:
    name = field(d, "navio", "nomeNavio", "vessel", "ship", "nome")
    if not name:
        return None

    # Possíveis nomes para datas conforme o endpoint
    eta = field(d, "eta", "dataEta", "previstaChegada", "previsao")
    etd = field(d, "etd", "dataEtd", "previstaPartida")
    ata = field(d, "ata", "dataAta", "dataChegada", "chegada")
    atd = field(d, "atd", "dataAtd", "dataPartida", "partida")
    generic_date = field(d, "data", "date")

    if default_type == "arrival":
        primary = eta or generic_date or ata
    elif default_type == "departure":
        primary = atd or generic_date or etd
    else:
        primary = ata or eta or atd or generic_date

    iso_date = parse_date_iso(primary)
    if not iso_date:
        return None

    type_str  = field(d, "tipoNavio", "tipo")
    motive    = field(d, "motivoEscala", "motivo", "operacao", "operação")
    terminal  = field(d, "localAtribuido", "local", "terminal", "cais", "berco", "berço")

    terminal_norm, is_hazard, is_cruise = classify(terminal, motive, type_str)
    if not is_hazard and not is_cruise:
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
# FALLBACK: tabela React (caso a API falhe)
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
        # Mapeia headers da tabela para o esperado pelo record_from_api
        adapted = {
            "navio":          row.get("nome do navio", "") or row.get("nome do navio▲", ""),
            "eta":            row.get("eta", "") or row.get("eta▲", ""),
            "etd":            row.get("etd", "") or row.get("etd▲", ""),
            "ata":            row.get("ata", "") or row.get("ata▲", ""),
            "atd":            row.get("atd", "") or row.get("atd▲", ""),
            "tipoNavio":      row.get("tipo de navio", "") or row.get("tipo de navio▲", ""),
            "motivoEscala":   row.get("motivo de escala", "") or row.get("motivo de escala▲", ""),
            "localAtribuido": row.get("local atribuido", "") or row.get("local atribuido▲", ""),
        }
        rec = record_from_api(adapted, default_type, source_label)
        if rec:
            out.append(rec)
    return out


# ════════════════════════════════════════════════════════════
# SCRAPING POR PÁGINA
# ════════════════════════════════════════════════════════════
def scrape_via_api(page: Page, page_url: str, label: str, default_type: str,
                   api_paths: list[str], api_payload: dict) -> list[dict]:
    print(f"\n[{label}] {page_url}")
    token, cookies = grab_session(page, page_url)

    # Tenta API endpoints na ordem
    for api_path in api_paths:
        result = call_api(page, api_path, api_payload, token, cookies)
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
        else:
            print(f"  [{label}] API '{api_path}' sem registos — a tentar próxima/fallback")

    # Fallback: parsing da tabela React directamente do DOM
    print(f"  [{label}] fallback: tabela React")
    try:
        page.wait_for_selector(".rdt_TableRow", timeout=15_000)
        page.wait_for_timeout(1500)
        out = parse_react_table_records(page, default_type, label)
        print(f"  [{label}] fallback devolveu {len(out)} registos")
        return out
    except PWTimeout:
        print(f"  [{label}] fallback também sem dados")
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
            user_agent=USER_AGENT,
            locale="pt-PT",
            viewport={"width": 1400, "height": 1000},
        )
        page = context.new_page()

        # Chegadas
        try:
            recs = scrape_via_api(
                page, URL_ARRIVALS, "arrivals", "arrival",
                [API_PATH_ARRIVALS], payload_dates,
            )
            all_records.extend(recs)
        except Exception as e:
            msg = f"arrivals: {type(e).__name__}: {e}"
            print(f"  ✗ {msg}")
            errors.append(msg)

        # Partidas
        try:
            recs = scrape_via_api(
                page, URL_DEPARTURES, "departures", "departure",
                [API_PATH_DEPARTURES], payload_dates,
            )
            all_records.extend(recs)
        except Exception as e:
            msg = f"departures: {type(e).__name__}: {e}"
            print(f"  ✗ {msg}")
            errors.append(msg)

        # Em Porto — não tem datas, paylod {} ou {today}
        try:
            recs = scrape_via_api(
                page, URL_IN_PORT, "in_port", "transit",
                API_PATH_IN_PORT_CANDIDATES, {},
            )
            all_records.extend(recs)
        except Exception as e:
            msg = f"in_port: {type(e).__name__}: {e}"
            print(f"  ✗ {msg}")
            errors.append(msg)

        browser.close()

    # Filtragem ±2d e deduplicação
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
