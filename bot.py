# bot.py — Bybit + TwelveData + MOEX ISS; crypto/FX/indices/stocks; 4H+1D; no Yahoo; 4-candle engulfing; gap-safe + session 4H for MOEX
# ВСЕГДА и ВЕЗДЕ работает ТОЛЬКО по ЗАКРЫТЫМ свечам.

import os, time, json, requests
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Set

# ========== CONFIG / ENV ==========

STATE_PATH       = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API           = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN          = int(os.getenv("DEM_LEN", "28"))
DEM_OB           = float(os.getenv("DEM_OB", "0.70"))
DEM_OS           = float(os.getenv("DEM_OS", "0.30"))
KLINE_4H         = os.getenv("KLINE_4H", "4h")
KLINE_1D         = os.getenv("KLINE_1D", "1d")
POLL_HRS         = int(os.getenv("POLL_HOURS", "1"))
POLL_SECONDS     = POLL_HRS * 3600

TWELVE_API_KEY   = os.getenv("TWELVEDATA_API_KEY", "")

# режим построения 4H по MOEX:
# "gap"     — только 4 подряд часа без дыр;
# "session" — внутри дня берём свечи пачками по 4 от начала дня.
MOEX_4H_MODE     = os.getenv("MOEX_4H_MODE", "gap").lower()

# ========== STATE ==========

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

def gc_state(state: Dict, days: int = 21):
    cutoff = int(time.time()) - days * 86400
    sent = state.get("sent", {})
    for k in list(sent.keys()):
        if isinstance(sent[k], int) and sent[k] < cutoff:
            del sent[k]
    state["sent"] = sent

STATE = load_state(STATE_PATH)

# ========== TELEGRAM ==========

def _chat_tokens() -> List[str]:
    if not TELEGRAM_CHAT:
        return []
    out = []
    for x in TELEGRAM_CHAT.split(","):
        x = x.strip()
        if x.startswith("-100"):
            out.append(x)
    return out

def tg_send_one(cid: str, text: str) -> bool:
    try:
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": cid,
            "text": text
        }, timeout=10)
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

# ========== HELPERS: ТОЛЬКО ЗАКРЫТЫЕ СВЕЧИ ==========

def closed_ohlc(ohlc: Optional[List[List[float]]]) -> List[List[float]]:
    """
    Возвращает только закрытые свечи:
    - если данных < 2, считаем, что нормальной истории нет;
    - всегда отрезаем последний бар (потенциально незакрытый).
    """
    if not ohlc or len(ohlc) < 2:
        return []
    return ohlc[:-1]

# ========== INDICATORS ==========

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
    """
    Берём последнюю НЕ-None точку индикатора.
    Серия соответствует ТОЛЬКО закрытым свечам.
    """
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

def wick_ge_body_pct(ohlc, idx, pct=0.25):
    if not ohlc:
        return False
    if not (-len(ohlc) <= idx < len(ohlc)):
        return False
    o,h,l,c = ohlc[idx][1:5]
    body = abs(c - o)
    if body <= 1e-12:
        return False
    upper = h - max(o,c)
    lower = min(o,c) - l
    return (upper >= pct*body) or (lower >= pct*body)

def engulfing_with_prior4(ohlc: List[List[float]]) -> bool:
    """
    Строго по ЗАКРЫТЫМ свечам.
    Используем 3 последние закрытые:
    -1: поглощающая
    -2: поглощённая, противоположного цвета
    -3: того же цвета, что -2 (фон тренда)
    """
    if not ohlc or len(ohlc) < 3:
        return False
    try:
        o2,h2,l2,c2 = ohlc[-1][1:5]  # поглощающая
        o3,h3,l3,c3 = ohlc[-2][1:5]  # поглощённая
        o4,h4,l4,c4 = ohlc[-3][1:5]  # фон
    except:
        return False

    bull2 = c2 >= o2
    bull3 = c3 >= o3
    bull4 = c4 >= o4

    cover = (min(o2,c2) <= min(o3,c3)) and (max(o2,c2) >= max(o3,c3))

    bull = bull2 and (not bull3) and (not bull4) and cover
    bear = (not bull2) and bull3 and bull4 and cover
    return bull or bear

def candle_pattern(ohlc: List[List[float]]) -> bool:
    """
    Свечной паттерн ищем ТОЛЬКО по закрытым свечам.
    Используем последние закрытые бары.
    """
    o = closed_ohlc(ohlc)
    if len(o) < 3:
        return False
    return wick_ge_body_pct(o, -1, 0.25) or engulfing_with_prior4(o)

# ========== BYBIT ==========

BYBIT_BASE = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_INSTR   = f"{BYBIT_BASE}/v5/market/instruments-info"
BB_KLINES  = f"{BYBIT_BASE}/v5/market/kline"
BB_TIMEOUT = 15

_BB_LINEAR: Set[str] = set()
_BB_SPOT:   Set[str] = set()

def refresh_bybit_instruments():
    global _BB_LINEAR, _BB_SPOT
    _BB_LINEAR=set(); _BB_SPOT=set()

    try:
        r = requests.get(BB_INSTR, params={"category":"linear"}, timeout=BB_TIMEOUT)
        if r.status_code==200:
            for it in (r.json().get("result") or {}).get("list") or []:
                sym=str(it.get("symbol","")).upper()
                _BB_LINEAR.add(sym)
    except:
        pass

    try:
        r = requests.get(BB_INSTR, params={"category":"spot"}, timeout=BB_TIMEOUT)
        if r.status_code==200:
            for it in (r.json().get("result") or {}).get("list") or []:
                sym=str(it.get("symbol","")).upper()
                _BB_SPOT.add(sym)
    except:
        pass

def fetch_bybit_klines(symbol: str, interval: str, category: str, limit=600):
    iv = "240" if interval=="4h" else ("D" if interval.lower()=="1d" else interval)
    try:
        r = requests.get(
            BB_KLINES,
            params={"category":category,"symbol":symbol,"interval":iv,"limit":limit},
            timeout=BB_TIMEOUT
        )
        if r.status_code!=200:
            return None
        lst = (r.json().get("result") or {}).get("list") or []
        out=[]
        for k in lst:
            ts=int(k[0]); ts=ts//1000 if ts>10**12 else ts
            o=float(k[1]); h=float(k[2]); l=float(k[3]); c=float(k[4])
            if h<=0 or l<=0: continue
            out.append([ts,o,h,l,c])
        out.sort(key=lambda x:x[0])
        return out
    except:
        return None

# ========== TWELVEDATA ==========

def fetch_twelvedata_klines(symbol: str, interval: str, limit=500):
    if not TWELVE_API_KEY:
        return None

    iv = interval.lower()
    td_iv = "4h" if iv=="4h" else "1day"

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
        if r.status_code!=200:
            return None
        j=r.json()
        if j.get("status")!="ok":
            return None

        out=[]
        for row in j.get("values") or []:
            dt=row.get("datetime")
            ts=None
            for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d"):
                try:
                    ts=int(datetime.strptime(dt,fmt).timestamp())
                    break
                except:
                    pass
            if ts is None:
                continue
            o=float(row["open"])
            h=float(row["high"])
            l=float(row["low"])
            c=float(row["close"])
            if h<=0 or l<=0: continue
            out.append([ts,o,h,l,c])

        return out
    except:
        return None

# ========== MOEX (RUS, 4H gap-safe + session mode) ==========

def fetch_moex_klines(sym: str, interval: str, limit=500):
    """
    sym: 'SBER.ME', 'IMOEX.ME' и т.п.

    interval:
      - '1d' / '1day' / 'd' → дневные свечи (interval=24).
      - '4h' / '240'       → часовые свечи (interval=60),
                             4H строится:
                             * MOEX_4H_MODE = 'gap'     → только 4 подряд часа без дыр;
                             * MOEX_4H_MODE = 'session' → внутри дня пачками по 4 от начала дня.
    """

    if not sym.endswith(".ME"):
        return None
    base = sym[:-3]

    if base in ("IMOEX","RTSI"):
        engine="stock"; market="index"
    else:
        engine="stock"; market="shares"

    iv = (interval or "").lower()
    want_4h = iv in ("4h","240")
    moex_iv = 60 if want_4h else 24
    raw_limit = limit*8 if want_4h else limit   # запас по данным

    url = f"https://iss.moex.com/iss/engines/{engine}/markets/{market}/securities/{base}/candles.json"

    try:
        r = requests.get(url, params={"interval":moex_iv,"limit":raw_limit}, timeout=15)
        if r.status_code!=200:
            return None
        j=r.json()
        c=j.get("candles") or {}
        cols=c.get("columns") or []
        data=c.get("data") or []
        idx={name:i for i,name in enumerate(cols)}
        need=["begin","open","high","low","close"]
        if any(x not in idx for x in need):
            return None

        raw=[]
        for row in data:
            try:
                ts = int(datetime.strptime(row[idx["begin"]],"%Y-%m-%d %H:%M:%S").timestamp())
                o  = float(row[idx["open"]])
                h  = float(row[idx["high"]])
                l  = float(row[idx["low"]])
                c_ = float(row[idx["close"]])
                if h<=0 or l<=0:
                    continue
                raw.append([ts,o,h,l,c_])
            except:
                pass

        raw.sort(key=lambda x:x[0])
        if not raw:
            return None

        if not want_4h:
            return raw[-limit:] if len(raw)>limit else raw

        # 4H режим "gap": только 4 подряд часа (без временных дыр)
        if MOEX_4H_MODE == "gap":
            out=[]
            buf=[]
            for bar in raw:
                ts, o,h,l,c_ = bar
                if not buf:
                    buf.append(bar)
                    continue
                prev_ts = buf[-1][0]
                if ts - prev_ts != 3600:
                    buf = [bar]
                    continue
                buf.append(bar)
                if len(buf) == 4:
                    ts4 = buf[0][0]
                    o4  = buf[0][1]
                    c4  = buf[-1][4]
                    h4  = max(x[2] for x in buf)
                    l4  = min(x[3] for x in buf)
                    out.append([ts4,o4,h4,l4,c4])
                    buf = []
            if not out:
                return None
            return out[-limit:] if len(out)>limit else out

        # 4H режим "session": внутри дня пачками по 4 бара от начала дня
        out=[]
        day_bars: Dict[str, List[List[float]]] = {}
        for bar in raw:
            ts = bar[0]
            dstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            day_bars.setdefault(dstr, []).append(bar)

        for dstr in sorted(day_bars.keys()):
            bars = day_bars[dstr]
            bars.sort(key=lambda x:x[0])
            n = len(bars) // 4
            for i in range(n):
                chunk = bars[i*4:(i+1)*4]
                ts4 = chunk[0][0]
                o4  = chunk[0][1]
                c4  = chunk[-1][4]
                h4  = max(x[2] for x in chunk)
                l4  = min(x[3] for x in chunk)
                out.append([ts4,o4,h4,l4,c4])

        if not out:
            return None
        out.sort(key=lambda x:x[0])
        return out[-limit:] if len(out)>limit else out

    except:
        return None

# ========== UNIVERSE ==========

CRYPTO = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX",
    "DOT","LINK","LTC","MATIC","TON","ATOM","NEAR"
]

ALIAS = {
    "ES":"US500USDT","^GSPC":"US500USDT",
    "NQ":"US100USDT","^NDX":"US100USDT",
    "YM":"US30USDT","^DJI":"US30USDT",
    "VIX":"VIXUSDT","DX":"DXYUSDT",

    "DE40":"DE40USDT","FR40":"FR40USDT","UK100":"UK100USDT","JP225":"JP225USDT",
    "HK50":"HK50USDT","CN50":"CN50USDT","AU200":"AU200USDT","ES35":"ES35USDT","IT40":"IT40USDT",

    "GC":"XAUUSDT","SI":"XAGUSDT","HG":"XCUUSDT","PL":"XPTUSDT",
    "PA":"XPDUSDT","CL":"OILUSDT","BZ":"BRENTUSDT","NG":"GASUSDT",

    "EURUSD":"EURUSD","GBPUSD":"GBPUSD","USDJPY":"USDJPY",
    "AUDUSD":"AUDUSD","NZDUSD":"NZDUSD","USDCAD":"USDCAD","USDCHF":"USDCHF",
}

TOKEN_STOCKS = {
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","BRKB",
    "AVGO","NFLX","AMD","JPM","V","MA","UNH","LLY","XOM","KO","PEP"
}

MOEX_LIST = [
    "IMOEX.ME","RTSI.ME","GAZP.ME","SBER.ME","LKOH.ME","ROSN.ME","TATN.ME",
    "ALRS.ME","GMKN.ME","YNDX.ME","MAGN.ME","MTSS.ME","CHMF.ME","AFLT.ME",
    "PHOR.ME","MOEX.ME","BELU.ME","PIKK.ME","VTBR.ME","IRAO.ME"
]

FX_ISO = {
    "USD","EUR","JPY","GBP","AUD","NZD","CHF","CAD",
    "MXN","CNY","HKD","SGD","SEK","NOK","DKK","ZAR","TRY","PLN",
    "CZK","HUF","ILS","KRW","TWD","THB","INR","BRL","RUB","AED","SAR"
}

def is_fx(sym: str):
    s="".join(ch for ch in sym.upper() if ch.isalpha())
    return len(s)>=6 and s[:3] in FX_ISO and s[3:6] in FX_ISO

def fx_to_td(sym: str):
    s="".join(ch for ch in sym.upper() if ch.isalpha())
    return f"{s[:3]}/{s[3:6]}"

def to_display(sym: str):
    s=sym.upper()
    if s.endswith(".ME"):
        return s
    if is_fx(s):
        letters="".join(ch for ch in s if ch.isalpha())
        b=letters[:3]; q=letters[3:6]
        return f"{b}{q}-USD"
    if s.endswith("USDT"): return s[:-4]+"-USDT"
    if s.endswith("USD") and "-" not in s: return s[:-3]+"-USDT"
    return s

def format_signal(symbol, sig, zone, src):
    arrow = "🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status = "⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    return f"{to_display(symbol)} [{src}] {arrow}{status}"

def bybit_alias(sym: str):
    s=sym.upper().replace(" ","").replace("=F","").replace("=X","").lstrip("^")
    if s in ALIAS: return ALIAS[s]
    s2=s.replace(".","")
    if s2 in TOKEN_STOCKS:
        return s2+"USDT"
    return None

# ========== FETCH ROUTERS ==========

def fetch_crypto(base: str, interval: str):
    bb_lin = base+"USDT"
    bb_perp = base+"PERP"

    if bb_lin in _BB_LINEAR:
        d=fetch_bybit_klines(bb_lin, interval,"linear")
        if d: return d,bb_lin,"BB"

    if bb_perp in _BB_LINEAR:
        d=fetch_bybit_klines(bb_perp, interval,"linear")
        if d: return d,bb_perp,"BB"

    if bb_lin in _BB_SPOT:
        d=fetch_bybit_klines(bb_lin,interval,"spot")
        if d: return d,bb_lin,"BB"

    td = f"{base}/USD"
    return fetch_twelvedata_klines(td,interval), td,"TD"

def fetch_other(sym: str, interval: str):
    alias = bybit_alias(sym)
    if alias:
        if alias in _BB_LINEAR:
            d=fetch_bybit_klines(alias,interval,"linear")
            if d: return d,alias,"BB"

    if sym.endswith(".ME"):
        d=fetch_moex_klines(sym,interval)
        return d,sym,"MOEX"

    if is_fx(sym):
        td = fx_to_td(sym)
        return fetch_twelvedata_klines(td,interval), td,"TD"

    return fetch_twelvedata_klines(sym,interval), sym,"TD"

# ========== PLAN ==========

def build_plan():
    plan=[]
    for b in CRYPTO:
        plan.append(("CRYPTO",b))
    for k in sorted(set(list(ALIAS.keys()) + list(TOKEN_STOCKS))):
        plan.append(("OTHER",k))
    for m in MOEX_LIST:
        plan.append(("OTHER",m))
    return plan

# ========== CORE ==========

def process_symbol(kind, name):
    if kind=="CRYPTO":
        k4_raw, n4, s4 = fetch_crypto(name,KLINE_4H)
        k1_raw, n1, s1 = fetch_crypto(name,KLINE_1D)
    else:
        k4_raw, n4, s4 = fetch_other(name,KLINE_4H)
        k1_raw, n1, s1 = fetch_other(name,KLINE_1D)

    if not k4_raw or not k1_raw:
        return False

    # работаем ТОЛЬКО по закрытым свечам
    k4 = closed_ohlc(k4_raw)
    k1 = closed_ohlc(k1_raw)
    if not k4 or not k1:
        return False

    d4 = demarker_series(k4,DEM_LEN)
    d1 = demarker_series(k1,DEM_LEN)
    if not d4 or not d1:
        return False

    v4 = last_closed(d4)
    v1 = last_closed(d1)
    z4 = zone_of(v4)
    z1 = zone_of(v1)

    # времена последних ЗАКРЫТЫХ баров
    open4 = k4[-1][0]
    open1 = k1[-1][0]
    dual  = max(open4,open1)

    sym = n4 or n1 or name
    if sym.endswith(".ME"):
        src = "MOEX"
    elif "BB" in (s4,s1):
        src = "BB"
    else:
        src = "TD"

    if z4 and z1 and z4==z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{sig}|{z4}|{dual}"
        return _broadcast_signal(format_signal(sym,sig,z4,src), key)

    if z4 and not z1 and candle_pattern(k4):
        key = f"{sym}|1TF+CAN@4H|{z4}|{open4}"
        return _broadcast_signal(format_signal(sym,"1TF+CAN",z4,src), key)

    if z1 and not z4 and candle_pattern(k1):
        key = f"{sym}|1TF+CAN@1D|{z1}|{open1}"
        return _broadcast_signal(format_signal(sym,"1TF+CAN",z1,src), key)

    return False

# ========== MAIN LOOP ==========

def main():
    while True:
        refresh_bybit_instruments()
        plan = build_plan()
        for kind,name in plan:
            process_symbol(kind,name)
            time.sleep(1)

        gc_state(STATE,21)
        save_state(STATE_PATH,STATE)
        time.sleep(POLL_SECONDS)

if __name__=="__main__":
    main()