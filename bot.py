# bot.py — Bybit + TwelveData (crypto/FX/indices/stocks/RU .ME); 4H+1D
# Только закрытые свечи, DeMarker(28), wick>=25%, engulfing, сигналы LIGHT / L+CAN / 1TF+CAN

import os, time, json, requests
from datetime import datetime
from typing import List, Dict

# ================= CONFIG =====================

STATE_PATH       = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API           = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN          = int(os.getenv("DEM_LEN", "28"))
DEM_OB           = float(os.getenv("DEM_OB", "0.70"))
DEM_OS           = float(os.getenv("DEM_OS", "0.30"))
KLINE_4H         = os.getenv("KLINE_4H", "4h")
KLINE_1D         = os.getenv("KLINE_1D", "1d")
POLL_HRS         = float(os.getenv("POLL_HOURS", "1"))
POLL_SECONDS     = int(POLL_HRS * 3600)

TWELVE_API_KEY   = os.getenv("TWELVEDATA_API_KEY", "")

# ================= STATE =====================

def load_state(path: str) -> Dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return {"sent": {}}

def save_state(path: str, data: Dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except:
        pass

def gc_state(state: Dict, days=21):
    cutoff = int(time.time()) - days * 86400
    sent = state.get("sent", {})
    for k, v in list(sent.items()):
        if isinstance(v, int) and v < cutoff:
            del sent[k]
    state["sent"] = sent

STATE = load_state(STATE_PATH)

# ================= TELEGRAM =====================

def _chat_tokens():
    if not TELEGRAM_CHAT:
        return []
    out = []
    for x in TELEGRAM_CHAT.split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out

def tg_send_one(cid, text):
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

def _broadcast_signal(text, signal_key):
    chats = _chat_tokens()
    if not TELEGRAM_TOKEN or not chats:
        return False
    ts = int(time.time())
    sent_any = False
    for cid in chats:
        k2 = f"{signal_key}|{cid}"
        if STATE["sent"].get(k2):
            continue
        if tg_send_one(cid, text):
            STATE["sent"][k2] = ts
            sent_any = True
    return sent_any

# ================= CLOSE BARS =====================

def closed_ohlc(ohlc):
    if not ohlc or len(ohlc) < 2:
        return []
    return ohlc[:-1]

# ================= INDICATORS =====================

def demarker_series(ohlc, length):
    if not ohlc or len(ohlc) < length + 1:
        return None
    highs = [x[2] for x in ohlc]
    lows  = [x[3] for x in ohlc]
    up = [0.0]
    dn = [0.0]
    for i in range(1, len(ohlc)):
        up.append(max(highs[i] - highs[i - 1], 0.0))
        dn.append(max(lows[i - 1] - lows[i], 0.0))
    def sma(a, i, n):
        return sum(a[i-n+1:i+1]) / n
    dem = [None] * len(ohlc)
    for i in range(length, len(ohlc)):
        u = sma(up, i, length)
        d = sma(dn, i, length)
        dem[i] = u/(u+d) if (u+d) != 0 else 0.5
    return dem

def last_closed(series):
    if not series:
        return None
    i = len(series) - 1
    while i >= 0 and series[i] is None:
        i -= 1
    return series[i] if i >= 0 else None

def zone_of(v):
    if v is None:
        return None
    if v >= DEM_OB:
        return "OB"
    if v <= DEM_OS:
        return "OS"
    return None

def wick_ge_body_pct(o, idx, pct=0.25):
    if not o:
        return False
    o_, h_, l_, c_ = o[idx][1:5]
    body = abs(c_ - o_)
    if body <= 1e-12:
        return False
    upper = h_ - max(o_, c_)
    lower = min(o_, c_) - l_
    return (upper >= pct*body) or (lower >= pct*body)

def engulfing_with_prior4(o):
    if not o or len(o) < 3:
        return False
    o2, h2, l2, c2 = o[-1][1:5]
    o3, h3, l3, c3 = o[-2][1:5]
    o4, h4, l4, c4 = o[-3][1:5]
    bull2 = c2 >= o2
    bull3 = c3 >= o3
    bull4 = c4 >= o4
    cover = (min(o2, c2) <= min(o3, c3)) and (max(o2, c2) >= max(o3, c3))
    bull = bull2 and (not bull3) and (not bull4) and cover
    bear = (not bull2) and bull3 and bull4 and cover
    return bull or bear

def candle_pattern(ohlc):
    o = closed_ohlc(ohlc)
    if len(o) < 3:
        return False
    return wick_ge_body_pct(o, -1, 0.25) or engulfing_with_prior4(o)

# ================= FORMAT TICKER =====================

def is_fx_sym(sym: str) -> bool:
    s = "".join(ch for ch in sym.upper() if ch.isalpha())
    return len(s) >= 6

def to_display(sym: str) -> str:
    s = sym.upper()
    if s.endswith(".ME"):
        return s
    if s.endswith("USDT"):
        return s[:-4] + "-USDT"
    if is_fx_sym(s) and len(s) == 6:
        return s + "-USD"
    return s

def format_signal(symbol: str, sig: str, zone: str, src: str) -> str:
    arrow = "🟢↑" if zone == "OS" else ("🔴↓" if zone == "OB" else "")
    status = "⚡" if sig == "LIGHT" else ("⚡🕯️" if sig == "L+CAN" else "🕯️")
    return f"{to_display(symbol)} [{src}] {arrow}{status}"

# ================= BYBIT =====================

BYBIT_BASE = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_KLINES  = f"{BYBIT_BASE}/v5/market/kline"
BB_TIMEOUT = 15

def fetch_bybit_klines(symbol, interval, category, limit=600):
    iv = "240" if interval == "4h" else ("D" if interval == "1d" else interval)
    try:
        r = requests.get(
            BB_KLINES,
            params={"category": category, "symbol": symbol, "interval": iv, "limit": limit},
            timeout=BB_TIMEOUT
        )
        if r.status_code != 200:
            return None
        lst = (r.json().get("result") or {}).get("list") or []
        out = []
        for k in lst:
            ts = int(k[0]); ts = ts//1000 if ts > 10**12 else ts
            o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
            if h <= 0 or l <= 0:
                continue
            out.append([ts, o, h, l, c])
        out.sort(key=lambda x: x[0])
        return out
    except:
        return None

# ================= TWELVEDATA =====================

def fetch_twelvedata_klines(symbol, interval, limit=500):
    if not TWELVE_API_KEY:
        return None
    td_iv = "4h" if interval == "4h" else "1day"
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol,
                "interval": td_iv,
                "outputsize": str(limit),
                "apikey": TWELVE_API_KEY,
                "order": "asc"
            },
            timeout=15
        )
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("status") != "ok":
            return None
        out = []
        for row in j.get("values") or []:
            dt = row["datetime"]
            ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    ts = int(datetime.strptime(dt, fmt).timestamp())
                    break
                except:
                    pass
            if ts is None:
                continue
            o = float(row["open"]); h = float(row["high"])
            l = float(row["low"]);  c = float(row["close"])
            if h <= 0 or l <= 0:
                continue
            out.append([ts, o, h, l, c])
        return out
    except:
        return None

# ================= TICKERS =====================

CRYPTO = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX",
    "DOT","LINK","LTC","MATIC","TON","ATOM","NEAR"
]

INDEX_PERP = [
    "US500USDT","US100USDT","US30USDT","VIXUSDT","DE40USDT",
    "FR40USDT","UK100USDT","JP225USDT","HK50USDT","CN50USDT",
    "AU200USDT","ES35USDT","IT40USDT"
]

METALS = [
    "XAUUSDT","XAGUSDT","XCUUSDT","XPTUSDT","XPDUSDT"
]

ENERGY = [
    "OILUSDT","BRENTUSDT","GASUSDT"
]

STOCKS = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","BRKB",
    "AVGO","NFLX","AMD","JPM","V","MA","UNH","LLY","XOM","KO","PEP"
]

# Российские акции/индексы — через TwelveData, тикеры .ME
RU_STOCKS = [
    "IMOEX.ME","RTSI.ME","GAZP.ME","SBER.ME","LKOH.ME","ROSN.ME","TATN.ME",
    "ALRS.ME","GMKN.ME","YNDX.ME","MAGN.ME","MTSS.ME","CHMF.ME","AFLT.ME",
    "PHOR.ME","MOEX.ME","BELU.ME","PIKK.ME","VTBR.ME","IRAO.ME"
]

FX = [
    "EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","USDCAD","USDCHF"
]

# ================= FETCH ROUTERS =====================

def fx_to_td(sym):
    return sym[:3] + "/" + sym[3:]

def fetch_crypto(base, interval):
    bb_lin = base + "USDT"
    d = fetch_bybit_klines(bb_lin, interval, "linear")
    if d:
        return d, bb_lin, "BB"

    bb_perp = base + "PERP"
    d = fetch_bybit_klines(bb_perp, interval, "linear")
    if d:
        return d, bb_perp, "BB"

    d = fetch_bybit_klines(bb_lin, interval, "spot")
    if d:
        return d, bb_lin, "BB"

    td = base + "/USD"
    return fetch_twelvedata_klines(td, interval), td, "TD"

def fetch_other(sym, interval):
    # Bybit USDT-перпы/CFD
    if sym.endswith("USDT"):
        d = fetch_bybit_klines(sym, interval, "linear")
        if d:
            return d, sym, "BB"

    # FX по ISO
    if len(sym) == 6 and sym[:3].isalpha() and sym[3:].isalpha():
        td = fx_to_td(sym)
        return fetch_twelvedata_klines(td, interval), td, "TD"

    # Всё остальное (включая .ME) — напрямую в TwelveData
    return fetch_twelvedata_klines(sym, interval), sym, "TD"

# ================= PLAN =====================

def build_plan():
    plan = []
    for x in CRYPTO:     plan.append(("CRYPTO", x))
    for x in INDEX_PERP: plan.append(("OTHER", x))
    for x in METALS:     plan.append(("OTHER", x))
    for x in ENERGY:     plan.append(("OTHER", x))
    for x in STOCKS:     plan.append(("OTHER", x))
    for x in FX:         plan.append(("OTHER", x))
    for x in RU_STOCKS:  plan.append(("OTHER", x))
    return plan

# ================= CORE =====================

def process_symbol(kind, name):
    if kind == "CRYPTO":
        k4_raw, n4, s4 = fetch_crypto(name, KLINE_4H)
        k1_raw, n1, s1 = fetch_crypto(name, KLINE_1D)
    else:
        k4_raw, n4, s4 = fetch_other(name, KLINE_4H)
        k1_raw, n1, s1 = fetch_other(name, KLINE_1D)

    # Оба ТФ должны быть, иначе сигнала нет
    if not k4_raw or not k1_raw:
        return False

    k4 = closed_ohlc(k4_raw)
    k1 = closed_ohlc(k1_raw)
    if not k4 or not k1:
        return False

    d4 = demarker_series(k4, DEM_LEN)
    d1 = demarker_series(k1, DEM_LEN)
    if d4 is None or d1 is None:
        return False

    v4 = last_closed(d4)
    v1 = last_closed(d1)
    z4 = zone_of(v4)
    z1 = zone_of(v1)

    open4 = k4[-1][0]
    open1 = k1[-1][0]
    dual  = max(open4, open1)

    sym = n4 or n1 or name
    src = "BB" if "BB" in (s4, s1) else "TD"

    if z4 and z1 and z4 == z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{sig}|{z4}|{dual}|{src}"
        return _broadcast_signal(format_signal(sym, sig, z4, src), key)

    if z4 and not z1 and candle_pattern(k4):
        sig = "1TF4H"
        key = f"{sym}|{sig}|{z4}|{open4}|{src}"
        return _broadcast_signal(format_signal(sym, sig, z4, src), key)

    if z1 and not z4 and candle_pattern(k1):
        sig = "1TF1D"
        key = f"{sym}|{sig}|{z1}|{open1}|{src}"
        return _broadcast_signal(format_signal(sym, sig, z1, src), key)

    return False

# ================= MAIN =====================

def main():
    while True:
        plan = build_plan()
        for kind, name in plan:
            process_symbol(kind, name)
            time.sleep(1)
        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()