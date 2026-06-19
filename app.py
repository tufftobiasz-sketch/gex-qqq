"""
GEX-QQQ — Gamma Exposure Dashboard dla QQQ
Pobiera dane wprost z publicznych endpointów Yahoo Finance (przez curl_cffi),
liczy gammę (Black-Scholes) i wyznacza Call Wall, Put Wall, Inflection (gamma flip)
oraz typ dnia (Trend / Balance).

Dlaczego nie yfinance? Jego wewnętrzny mechanizm "crumb" bywa blokowany przez Yahoo.
Pobieranie wprost jest stabilniejsze. curl_cffi udaje przeglądarkę Chrome (Yahoo nie blokuje),
a na Windowsie używamy certyfikatów z magazynu systemu, bo antywirus (Avast) skanuje HTTPS.
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from curl_cffi import requests as creq
import numpy as np
from datetime import datetime, date, timezone
import os, ssl, platform, time

# gęstość rozkładu normalnego (zamiast scipy — żeby uniknąć ciężkiej instalacji)
_SQRT_2PI = np.sqrt(2 * np.pi)
def norm_pdf(x):
    return np.exp(-0.5 * np.square(x)) / _SQRT_2PI

app = Flask(__name__)
CORS(app)

TICKER = "QQQ"
RISK_FREE = 0.045          # stopa wolna od ryzyka (~4.5%)
AGG_EXPIRIES = 3           # ile najbliższych terminów sumować w trybie "suma"
STRIKE_WINDOW = 0.10       # okno do WYKRESU/ścian: +/- 10% od ceny spot
FLIP_WINDOW = 0.25         # okno do liczenia gamma flip: +/- 25% (cały istotny łańcuch)
CACHE_TTL = 300            # cache na 5 min, żeby nie odpytywać Yahoo za często

_cache = {}                # klucz: wybór wygaśnięcia -> {"ts":.., "data":..}
_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "win_ca_bundle.pem")


# --------------------------------------------------------------------------- #
#  Warstwa danych — Yahoo Finance
# --------------------------------------------------------------------------- #

def _windows_ca_bundle():
    """Na Windowsie buduje plik PEM z certyfikatami magazynu systemu (zawiera CA Avasta)."""
    if platform.system() != "Windows":
        return None
    if os.path.exists(_BUNDLE):
        return _BUNDLE
    import certifi
    pems = [open(certifi.where()).read()]
    for store in ("ROOT", "CA"):
        for cert, enc, _ in ssl.enum_certificates(store):
            if enc == "x509_asn":
                pems.append(ssl.DER_cert_to_PEM_cert(cert))
    with open(_BUNDLE, "w") as f:
        f.write("\n".join(pems))
    return _BUNDLE


def make_session():
    """Sesja curl_cffi z impersonacją Chrome, poprawnym certyfikatem i tokenem crumb."""
    bundle = _windows_ca_bundle()
    s = creq.Session(impersonate="chrome")
    if bundle:
        s.verify = bundle
    s.get("https://fc.yahoo.com", timeout=20)                      # ustaw ciasteczka
    crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).text
    return s, crumb


def fetch_spot(s):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?range=1d&interval=1d"
    j = s.get(url, timeout=20).json()
    return float(j["chart"]["result"][0]["meta"]["regularMarketPrice"])


def _unix_to_date(u):
    return datetime.fromtimestamp(u, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_expiry_map(s, crumb):
    """Pobiera listę wszystkich terminów wygaśnięcia (z Yahoo) + pierwszy łańcuch.
    Zwraca: (mapa {data->unix}, prefetched {unix->chain})."""
    base = f"https://query1.finance.yahoo.com/v7/finance/options/{TICKER}"
    first = s.get(base, params={"crumb": crumb}, timeout=20).json()
    result = first["optionChain"]["result"][0]
    expmap = {_unix_to_date(u): u for u in result["expirationDates"]}
    first_chain = result["options"][0]
    prefetched = {first_chain["expirationDate"]: first_chain}
    return expmap, prefetched


def fetch_chain(s, crumb, exp_unix, prefetched):
    if exp_unix in prefetched:
        return prefetched[exp_unix]
    base = f"https://query1.finance.yahoo.com/v7/finance/options/{TICKER}"
    j = s.get(base, params={"date": exp_unix, "crumb": crumb}, timeout=20).json()
    chain = j["optionChain"]["result"][0]["options"][0]
    prefetched[exp_unix] = chain
    return chain


# --------------------------------------------------------------------------- #
#  Matematyka — gamma i GEX
# --------------------------------------------------------------------------- #

def bs_gamma(S, K, T, sigma, r=RISK_FREE):
    """Gamma z modelu Black-Scholes."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return norm_pdf(d1) / (S * sigma * np.sqrt(T))


def years_to_expiry(unix_ts):
    exp = datetime.fromtimestamp(unix_ts, tz=timezone.utc).date()
    days = (exp - date.today()).days
    return max(days, 1) / 365.0


def compute_gex(selected_expiry=None):
    """selected_expiry: data 'YYYY-MM-DD' (jeden termin) albo None (suma najbliższych)."""
    cache_key = selected_expiry or "aggregate"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached["ts"] < CACHE_TTL):
        return cached["data"]

    s, crumb = make_session()
    spot = fetch_spot(s)
    expmap, prefetched = fetch_expiry_map(s, crumb)

    available = sorted(expmap.keys())

    # ustal które wygaśnięcia liczymy
    if selected_expiry == "agg":
        selected_expiry = None                       # tryb sumy
        target_dates = available[:AGG_EXPIRIES]
    elif selected_expiry and selected_expiry in expmap:
        target_dates = [selected_expiry]             # konkretny dzień
    else:
        selected_expiry = available[0]               # DOMYŚLNIE: najbliższe wygaśnięcie
        target_dates = [available[0]]

    disp_low, disp_high = spot * (1 - STRIKE_WINDOW), spot * (1 + STRIKE_WINDOW)
    flip_low, flip_high = spot * (1 - FLIP_WINDOW), spot * (1 + FLIP_WINDOW)
    strike_gex = {}
    # zbieramy surowe opcje (szersze okno) do profilu gammy / gamma flip
    opt_K, opt_T, opt_IV, opt_OI, opt_SIGN = [], [], [], [], []

    for d in target_dates:
        exp_unix = expmap[d]
        T = years_to_expiry(exp_unix)
        chain = fetch_chain(s, crumb, exp_unix, prefetched)
        for opt_type, key in (("call", "calls"), ("put", "puts")):
            sign = 1.0 if opt_type == "call" else -1.0   # dealerzy long call / short put
            for o in chain.get(key, []):
                K = float(o.get("strike", 0))
                if K < flip_low or K > flip_high:
                    continue
                oi = float(o.get("openInterest", 0) or 0)
                iv = float(o.get("impliedVolatility", 0) or 0)
                if oi <= 0 or iv <= 0:
                    continue
                # cały łańcuch (±25%) idzie do profilu gammy / flip
                opt_K.append(K); opt_T.append(T); opt_IV.append(iv)
                opt_OI.append(oi); opt_SIGN.append(sign)
                # tylko ±10% idzie na wykres słupkowy i do ścian
                if disp_low <= K <= disp_high:
                    gex = bs_gamma(spot, K, T, iv) * oi * 100 * spot * spot * 0.01
                    bucket = strike_gex.setdefault(K, {"call": 0.0, "put": 0.0})
                    bucket["call" if sign > 0 else "put"] += sign * gex

    if not strike_gex:
        raise RuntimeError("Brak danych opcji w oknie strike'ów (giełda zamknięta?).")

    strikes = sorted(strike_gex.keys())
    calls = [strike_gex[k]["call"] for k in strikes]
    puts = [strike_gex[k]["put"] for k in strikes]
    nets = [c + p for c, p in zip(calls, puts)]
    cumulative = np.cumsum(nets)

    call_wall = max(strikes, key=lambda k: strike_gex[k]["call"])
    put_wall = min(strikes, key=lambda k: strike_gex[k]["put"])

    # --- Gamma flip metodą profilu: dla różnych hipotetycznych cen liczymy
    #     całkowitą gammę dealerów i szukamy gdzie zmienia znak (to prawdziwy flip) ---
    aK = np.array(opt_K); aT = np.array(opt_T); aIV = np.array(opt_IV)
    aOI = np.array(opt_OI); aSIGN = np.array(opt_SIGN)

    def total_gex_at(S):
        d1 = (np.log(S / aK) + (RISK_FREE + 0.5 * aIV ** 2) * aT) / (aIV * np.sqrt(aT))
        g = norm_pdf(d1) / (S * aIV * np.sqrt(aT))
        return float(np.sum(aSIGN * g * aOI * 100 * S * S * 0.01))

    prices = np.linspace(spot * 0.85, spot * 1.15, 600)
    profile = np.array([total_gex_at(S) for S in prices])
    flips = []
    for i in range(1, len(profile)):
        if (profile[i - 1] < 0 <= profile[i]) or (profile[i - 1] >= 0 > profile[i]):
            x0, x1 = prices[i - 1], prices[i]
            y0, y1 = profile[i - 1], profile[i]
            flips.append(round(x0 + (x1 - x0) * (-y0) / (y1 - y0), 2))
    inflection = (min(flips, key=lambda x: abs(x - spot)) if flips
                  else round(float(prices[int(np.argmin(np.abs(profile)))]), 2))

    # regime liczymy WPROST ze znaku całkowitej gammy przy obecnej cenie (nie ze spot vs flip)
    is_positive = total_gex_at(spot) > 0
    if is_positive:
        day_type, day_state = "BALANCE DAY", "balance"
        day_desc = "Dodatnia gamma — dealerzy tłumią ruchy. Rynek mean-reverting (zakres)."
    else:
        day_type, day_state = "TREND DAY", "trend"
        day_desc = "Ujemna gamma — dealerzy wzmacniają ruchy. Rynek kierunkowy (trend)."

    data = {
        "ticker": TICKER,
        "spot": round(spot, 2),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "available_expiries": available,
        "selected_expiry": selected_expiry,      # None = suma
        "agg_count": AGG_EXPIRIES,
        "strike_window_pct": int(STRIKE_WINDOW * 100),
        "strikes": strikes,
        "call_gex": calls,
        "put_gex": puts,
        "net_gex": nets,
        "cumulative_gex": [float(x) for x in cumulative],
        "call_wall": call_wall,
        "put_wall": put_wall,
        "inflection": inflection,
        "total_gex": float(np.sum(nets)),
        "day_type": day_type,
        "day_desc": day_desc,
        "day_state": day_state,
    }
    _cache[cache_key] = {"ts": time.time(), "data": data}
    return data


# --------------------------------------------------------------------------- #
#  Routy
# --------------------------------------------------------------------------- #

@app.route("/api/gex")
def api_gex():
    expiry = request.args.get("expiry") or None
    try:
        return jsonify({"ok": True, "data": compute_gex(expiry)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
