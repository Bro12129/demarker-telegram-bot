# bot.py — Bybit + Finnhub (crypto/FX/indices/stocks/RU .ME); 4H+1D
# Только закрытые свечи, DeMarker(28), wick>=25%, engulfing
# Сигналы:
#   ⚡ / ⚡🕯️  — совпадение 4H и 1D
#   1TF4H     — 4H + свечной паттерн
#   1TF1D     — 1D + свечной паттерн

import os, time, json, requests
from datetime import datetime
from typing import List, Dict, Optional

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

FINNHUB_API_KEY  = os.getenv("FINNHUB_API_KEY", "")

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

def _chat_tokens() -> List[str]:
    if not TELEGRAM_CHAT:
        return []
    out: List[str] = []
    for x in TELEGRAM_CHAT.split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out

def tg_send_one(cid: str, text: str) -> bool:
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

def _broadcast_signal(text: str, signal_key: str) -> bool:
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

def closed_ohlc(ohlc: Optional[List[List[float]]]) -> List[List[float]]:
    if not ohlc or len(ohlc) < 2:
        return []
    return ohlc[:-1]

# ================= INDICATORS =====================

def demarker_series(ohlc: List[List[float]], length: int):
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

def wick_ge_body_pct(o: List[List[float]], idx: int, pct=0.25) -> bool:
    if not o:
        return False
    if not (-len(o) <= idx < len(o)):
        return False
    o_, h_, l_, c_ = o[idx][1:5]
    body = abs(c_ - o_)
    if body <= 1e-12:
        return False
    upper = h_ - max(o_, c_)
    lower = min(o_, c_) - l_
    return (upper >= pct*body) or (lower >= pct*body)

def engulfing_with_prior4(o: List[List[float]]) -> bool:
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

def candle_pattern(ohlc: List[List[float]]) -> bool:
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

def fetch_bybit_klines(symbol: str, interval: str, category: str, limit=600):
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

# ================= FINNHUB =====================

FINNHUB_BASE   = os.getenv("FINNHUB_BASE", "https://finnhub.io/api/v1")
FH_TIMEOUT     = 15

def fx_to_fh(sym: str) -> str:
    s = sym.upper()
    if len(s) == 6 and s[:3].isalpha() and s[3:].isalpha():
        return f"OANDA:{s[:3]}_{s[3:]}"
    return s

def crypto_base_to_fh(base: str) -> str:
    return f"BINANCE:{base.upper()}USDT"

def fetch_finnhub_candles(kind: str, symbol: str, interval: str):
    if not FINNHUB_API_KEY:
        return None
    if interval == "4h":
        res = "240"
        span_days = 200
    else:
        res = "D"
        span_days = 800
    to_ts = int(time.time())
    from_ts = to_ts - span_days * 86400

    if kind == "CRYPTO":
        path = "/crypto/candle"
    elif kind == "FX":
        path = "/forex/candle"
    else:
        path = "/stock/candle"

    try:
        r = requests.get(
            FINNHUB_BASE + path,
            params={
                "symbol": symbol,
                "resolution": res,
                "from": from_ts,
                "to": to_ts,
                "token": FINNHUB_API_KEY
            },
            timeout=FH_TIMEOUT
        )
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("s") != "ok":
            return None
        t = j.get("t") or []
        o = j.get("o") or []
        h = j.get("h") or []
        l = j.get("l") or []
        c = j.get("c") or []
        out = []
        n = min(len(t), len(o), len(h), len(l), len(c))
        for i in range(n):
            ts = int(t[i])
            oo = float(o[i]); hh = float(h[i]); ll = float(l[i]); cc = float(c[i])
            if hh <= 0 or ll <= 0:
                continue
            out.append([ts, oo, hh, ll, cc])
        out.sort(key=lambda x: x[0])
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

RU_STOCKS = [
    "IMOEX.ME","RTSI.ME","GAZP.ME","SBER.ME","LKOH.ME","ROSN.ME","TATN.ME",
    "ALRS.ME","GMKN.ME","YNDX.ME","MAGN.ME","MTSS.ME","CHMF.ME","AFLT.ME",
    "PHOR.ME","MOEX.ME","BELU.ME","PIKK.ME","VTBR.ME","IRAO.ME"
]

FX = [
    "EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","USDCAD","USDCHF"
]

# ================= FETCH ROUTERS =====================

def fetch_crypto(base: str, interval: str):
    # 1) Bybit linear USDT
    bb_lin = base + "USDT"
    d = fetch_bybit_klines(bb_lin, interval, "linear")
    if d:
        return d, bb_lin, "BB"

    # 2) Bybit PERP
    bb_perp = base + "PERP"
    d = fetch_bybit_klines(bb_perp, interval, "linear")
    if d:
        return d, bb_perp, "BB"

    # 3) Bybit spot
    d = fetch_bybit_klines(bb_lin, interval, "spot")
    if d:
        return d, bb_lin, "BB"

    # 4) Finnhub crypto (BINANCE)
    fh_sym = crypto_base_to_fh(base)
    d = fetch_finnhub_candles("CRYPTO", fh_sym, interval)
    return d, fh_sym, "FH"

def fetch_other(sym: str, interval: str):
    # 1) Все, что на Bybit как USDT-перпы/CFD: сначала пробуем Bybit
    if sym.endswith("USDT"):
        d = fetch_bybit_klines(sym, interval, "linear")
        if d:
            return d, sym, "BB"
        # если Bybit ничего не дал — альтернативы здесь не навязываем
        return None, sym, "BB"

    # 2) FX через Finnhub (OANDA:EUR_USD и т.п.)
    if len(sym) == 6 and sym[:3].isalpha() and sym[3:].isalpha():
        fh_sym = fx_to_fh(sym)
        d = fetch_finnhub_candles("FX", fh_sym, interval)
        return d, fh_sym, "FH"

    # 3) Акции (US и .ME) — через Finnhub stock candles
    fh_sym = sym
    d = fetch_finnhub_candles("STOCK", fh_sym, interval)
    return d, fh_sym, "FH"

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

def process_symbol(kind: str, name: str) -> bool:
    # грузим оба таймфрейма, но 1TF-сигналы могут работать и по одному
    if kind == "CRYPTO":
        k4_raw, n4, s4 = fetch_crypto(name, KLINE_4H)
        k1_raw, n1, s1 = fetch_crypto(name, KLINE_1D)
    else:
        k4_raw, n4, s4 = fetch_other(name, KLINE_4H)
        k1_raw, n1, s1 = fetch_other(name, KLINE_1D)

    have4 = bool(k4_raw)
    have1 = bool(k1_raw)
    if not have4 and not have1:
        return False

    k4 = closed_ohlc(k4_raw) if have4 else None
    k1 = closed_ohlc(k1_raw) if have1 else None
    if have4 and not k4:
        have4 = False
    if have1 and not k1:
        have1 = False
    if not have4 and not have1:
        return False

    d4 = demarker_series(k4, DEM_LEN) if have4 else None
    d1 = demarker_series(k1, DEM_LEN) if have1 else None

    v4 = last_closed(d4) if d4 is not None else None
    v1 = last_closed(d1) if d1 is not None else None

    z4 = zone_of(v4) if v4 is not None else None
    z1 = zone_of(v1) if v1 is not None else None

    pat4 = candle_pattern(k4) if have4 else False
    pat1 = candle_pattern(k1) if have1 else False

    open4 = k4[-1][0] if have4 else None
    open1 = k1[-1][0] if have1 else None
    dual  = max([x for x in (open4, open1) if x is not None])

    sym = n4 or n1 or name
    src = "BB" if "BB" in (s4, s1) else "FH"

    sent = False

    # A) Совпадение двух ТФ (4H+1D)
    if z4 and z1 and z4 == z1:
        sig = "L+CAN" if (pat4 or pat1) else "LIGHT"
        key = f"{sym}|{sig}|{z4}|{dual}|{src}"
        sent |= _broadcast_signal(format_signal(sym, sig, z4, src), key)

    # B) 1TF4H — только 4 часа: крайность + свеча
    if have4 and z4 and pat4:
        sig = "1TF4H"
        key = f"{sym}|{sig}|{z4}|{open4}|{src}"
        sent |= _broadcast_signal(format_signal(sym, sig, z4, src), key)

    # C) 1TF1D — только день: крайность + свеча
    if have1 and z1 and pat1:
        sig = "1TF1D"
        key = f"{sym}|{sig}|{z1}|{open1}|{src}"
        sent |= _broadcast_signal(format_signal(sym, sig, z1, src), key)

    return sent

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