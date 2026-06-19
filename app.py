"""
GEX-QQQ — Gamma Exposure Dashboard dla QQQ
Pobiera dane wprost z publicznych endpointów Yahoo Finance (przez curl_cffi),
liczy gammę (Black-Scholes) i wyznacza Call Wall, Put Wall, Inflection (gamma flip)
oraz typ dnia (Trend / Balance).

Dlaczego nie yfinance? Jego wewnętrzny mechanizm "crumb" bywa blokowany przez Yahoo.
Pobieranie wprost jest stabilniejsze. curl_cffi udaje przeglądarkę Chrome (Yahoo nie blokuje),
a na Windowsie używamy certyfikatów z magazynu systemu, bo antywirus (Avast) skanuje HTTPS.
"""

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from curl_cffi import requests as creq
import numpy as np
from scipy.stats import norm
from datetime import datetime, date, timezone
import os, ssl, platform, time

app = Flask(__name__)
CORS(app)

TICKER = "QQQ"
RISK_FREE = 0.045          # stopa wolna od ryzyka (~4.5%)
NUM_EXPIRIES = 3           # ile najbliższych terminów wygaśnięcia brać pod uwagę
STRIKE_WINDOW = 0.06       # strike'i +/- 6% od ceny spot (reszta to szum)
CACHE_TTL = 300            # cache na 5 min, żeby nie odpytywać Yahoo za często

_cache = {"ts": 0, "data": None}
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


def fetch_options(s, crumb):
    """Zwraca (lista_expiry_unix, dict{expiry_unix: {'calls':[...], 'puts':[...]}})."""
    base = f"https://query1.finance.yahoo.com/v7/finance/options/{TICKER}"
    first = s.get(base, params={"crumb": crumb}, timeout=20).json()
    result = first["optionChain"]["result"][0]
    all_expiries = result["expirationDates"][:NUM_EXPIRIES]

    chains = {result["options"][0]["expirationDate"]: result["options"][0]}
    for exp in all_expiries:
        if exp in chains:
            continue
        j = s.get(base, params={"date": exp, "crumb": crumb}, timeout=20).json()
        chains[exp] = j["optionChain"]["result"][0]["options"][0]
    return all_expiries, chains


# --------------------------------------------------------------------------- #
#  Matematyka — gamma i GEX
# --------------------------------------------------------------------------- #

def bs_gamma(S, K, T, sigma, r=RISK_FREE):
    """Gamma z modelu Black-Scholes."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))


def years_to_expiry(unix_ts):
    exp = datetime.fromtimestamp(unix_ts, tz=timezone.utc).date()
    days = (exp - date.today()).days
    return max(days, 1) / 365.0


def compute_gex():
    # cache
    if _cache["data"] and (time.time() - _cache["ts"] < CACHE_TTL):
        return _cache["data"]

    s, crumb = make_session()
    spot = fetch_spot(s)
    expiries, chains = fetch_options(s, crumb)

    low, high = spot * (1 - STRIKE_WINDOW), spot * (1 + STRIKE_WINDOW)
    strike_gex = {}   # strike -> {'call':..,'put':..}

    for exp in expiries:
        chain = chains.get(exp)
        if not chain:
            continue
        T = years_to_expiry(exp)
        for opt_type, key in (("call", "calls"), ("put", "puts")):
            for o in chain.get(key, []):
                K = float(o.get("strike", 0))
                if K < low or K > high:
                    continue
                oi = float(o.get("openInterest", 0) or 0)
                iv = float(o.get("impliedVolatility", 0) or 0)
                if oi <= 0 or iv <= 0:
                    continue
                gamma = bs_gamma(spot, K, T, iv)
                gex = gamma * oi * 100 * spot * spot * 0.01   # GEX na 1% ruchu
                bucket = strike_gex.setdefault(K, {"call": 0.0, "put": 0.0})
                if opt_type == "call":
                    bucket["call"] += gex          # dealerzy long gamma na callach
                else:
                    bucket["put"] -= gex           # dealerzy short gamma na putach

    if not strike_gex:
        raise RuntimeError("Brak danych opcji w oknie strike'ów (giełda zamknięta?).")

    strikes = sorted(strike_gex.keys())
    calls = [strike_gex[k]["call"] for k in strikes]
    puts = [strike_gex[k]["put"] for k in strikes]
    nets = [c + p for c, p in zip(calls, puts)]

    call_wall = max(strikes, key=lambda k: strike_gex[k]["call"])
    put_wall = min(strikes, key=lambda k: strike_gex[k]["put"])

    # Inflection (gamma flip): strike gdzie skumulowany Net GEX przechodzi przez zero.
    # Może być kilka przejść — wybieramy to najbliższe cenie spot (główny flip).
    cumulative = np.cumsum(nets)
    crossings = []
    for i in range(1, len(cumulative)):
        if (cumulative[i - 1] < 0 <= cumulative[i]) or (cumulative[i - 1] >= 0 > cumulative[i]):
            x0, x1 = strikes[i - 1], strikes[i]
            y0, y1 = cumulative[i - 1], cumulative[i]
            crossings.append(round(x0 + (x1 - x0) * (-y0) / (y1 - y0), 2))
    if crossings:
        inflection = min(crossings, key=lambda x: abs(x - spot))
    else:
        inflection = strikes[int(np.argmin(np.abs(cumulative)))]

    is_positive = spot > inflection
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
    _cache.update(ts=time.time(), data=data)
    return data


# --------------------------------------------------------------------------- #
#  Routy
# --------------------------------------------------------------------------- #

@app.route("/api/gex")
def api_gex():
    try:
        return jsonify({"ok": True, "data": compute_gex()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
