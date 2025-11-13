# bot.py — Bybit primary → Yahoo fallback; 15 crypto majors; commodities/indices/FX/xStocks; source tags; 4-свечное поглощение; no Russell

import os, time, json, requests
from typing import List, Dict, Optional, Tuple, Set

# ===== ENV / CONFIG =====
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN  = int(os.getenv("DEM_LEN", "28"))
DEM_OB   = float(os.getenv("DEM_OB", "0.70"))
DEM_OS   = float(os.getenv("DEM_OS", "0.30"))
KLINE_4H = os.getenv("KLINE_4H", "4h")
KLINE_1D = os.getenv("KLINE_1D", "1d")
POLL_HRS = int(os.getenv("POLL_HOURS", "1"))
POLL_SECONDS = POLL_HRS * 3600

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ===== STATE =====
def load_state(path: str) -> Dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}}

def save_state(path: str, data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass

def gc_state(state: Dict, days: int = 21) -> None:
    try:
        cutoff = int(time.time()) - days*86400
        sent = state.get("sent", {})
        for k in list(sent.keys()):
            if isinstance(sent[k], int) and sent[k] < cutoff:
                del sent[k]
        state["sent"] = sent
    except Exception:
        pass

STATE = load_state(STATE_PATH)

# ===== TELEGRAM (group-only) =====
def _chat_tokens() -> List[str]:
    raw = (TELEGRAM_CHAT or "").strip()
    if not raw:
        return []
    toks = [x.strip() for x in raw.split(",") if x.strip()]
    return [t for t in toks if t.startswith("-100")]

def tg_send_one(cid: str, text: str) -> bool:
    try:
        r = requests.post(f"{TG_API}/sendMessage", json={"chat_id": cid, "text": text}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def _broadcast_signal(text: str, signal_key: str) -> bool:
    chats = _chat_tokens()
    if not TELEGRAM_TOKEN or not chats:
        return False
    ts_now = int(time.time())
    sent_any = False
    for cid in chats:
        k2 = f"{signal_key}|{cid}"
        if STATE["sent"].get(k2):
            continue
        if tg_send_one(cid, text):
            STATE["sent"][k2] = ts_now
            sent_any = True
    return sent_any

# ===== INDICATORS =====
def demarker_series(ohlc: List[List[float]], length: int) -> Optional[List[Optional[float]]]:
    if not ohlc or len(ohlc) < length + 2:
        return None
    highs=[x[2] for x in ohlc]; lows=[x[3] for x in ohlc]
    up=[0.0]; dn=[0.0]
    for i in range(1,len(ohlc)):
        up.append(max(highs[i]-highs[i-1],0.0))
        dn.append(max(lows[i-1]-lows[i],0.0))
    def sma(a,i,n): return sum(a[i-n+1:i+1])/n
    dem=[None]*len(ohlc)
    for i in range(length,len(ohlc)):
        u=sma(up,i,length); d=sma(dn,i,length)
        dem[i]=u/(u+d) if (u+d)!=0 else 0.5
    return dem

def last_closed(series):
    if not series or len(series)<2: return None
    i=len(series)-2
    while i>=0 and series[i] is None: i-=1
    return series[i] if i>=0 else None

def zone_of(v):
    if v is None: return None
    if v>=DEM_OB: return "OB"
    if v<=DEM_OS: return "OS"
    return None

def wick_ge_body_pct(ohlc, idx, pct=0.25):
    if not ohlc or not(-len(ohlc)<=idx<len(ohlc)): return False
    o,h,l,c=ohlc[idx][1:5]; body=abs(c-o)
    if body<=1e-12: return False
    upper=h-max(o,c); lower=min(o,c)-l
    return (upper>=pct*body) or (lower>=pct*body)

# --- Новая реализация поглощения по 4 свечам ---
def engulfing_with_prior4(ohlc) -> bool:
    """
    -1 : текущая (не закрыта), не используется
    -2 : поглощающая (закрыта)
    -3 : поглощённая, противоположного цвета -2
    -4 : того же цвета, что -3 (микро-тренд)
    Поглощение проверяем по ТЕЛАМ (fitili не учитываем).
    """
    if not ohlc or len(ohlc) < 4:
        return False
    try:
        o2,h2,l2,c2 = ohlc[-2][1:5]  # поглощающая
        o3,h3,l3,c3 = ohlc[-3][1:5]  # поглощённая
        o4,h4,l4,c4 = ohlc[-4][1:5]  # фон того же цвета, что -3
    except Exception:
        return False

    bull2 = c2 >= o2
    bull3 = c3 >= o3
    bull4 = c4 >= o4

    cover_body = (min(o2,c2) <= min(o3,c3)) and (max(o2,c2) >= max(o3,c3))

    bull_engulf = bull2 and (not bull3) and (not bull4) and cover_body
    bear_engulf = (not bull2) and bull3 and bull4 and cover_body
    return bull_engulf or bear_engulf

def candle_pattern(ohlc):
    if not ohlc or len(ohlc)<4: return False
    return wick_ge_body_pct(ohlc,-2,0.25) or engulfing_with_prior4(ohlc)

# ===== BYBIT API =====
BYBIT_BASE   = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_INSTR_EP  = f"{BYBIT_BASE}/v5/market/instruments-info"
BB_KLINES_EP = f"{BYBIT_BASE}/v5/market/kline"}
BB_TIMEOUT   = 15

_BB_LINEAR: Set[str] = set()  # PERP (linear)
_BB_SPOT:   Set[str] = set()

def refresh_bybit_instruments():
    global _BB_LINEAR, _BB_SPOT
    _BB_LINEAR=set(); _BB_SPOT=set()
    try:
        r = requests.get(BB_INSTR_EP, params={"category":"linear"}, timeout=BB_TIMEOUT)
        if r.status_code == 200:
            for it in (r.json().get("result") or {}).get("list") or []:
                sym = str(it.get("symbol","")).upper().strip()
                _BB_LINEAR.add(sym)
        r = requests.get(BB_INSTR_EP, params={"category":"spot"}, timeout=BB_TIMEOUT)
        if r.status_code == 200:
            for it in (r.json().get("result") or {}).get("list") or []:
                sym = str(it.get("symbol","")).upper().strip()
                _BB_SPOT.add(sym)
    except Exception:
        pass

def fetch_bybit_klines(symbol: str, interval: str, category: str, limit: int = 600) -> Optional[List[List[float]]]:
    iv = "240" if interval == "4h" else ("D" if interval.lower()=="1d" else interval)
    try:
        r = requests.get(BB_KLINES_EP, params={"category":category,"symbol":symbol,"interval":iv,"limit":str(limit)}, timeout=BB_TIMEOUT)
        if r.status_code != 200:
            return None
        lst = (r.json().get("result") or {}).get("list") or []
        if not lst:
            return None
        out=[]
        for k in lst:
            ts = int(k[0]); ts = ts//1000 if ts>10**12 else ts
            o=float(k[1]); h=float(k[2]); l=float(k[3]); c=float(k[4])
            if h<=0 or l<=0: continue
            out.append([ts,o,h,l,c])
        out.sort(key=lambda x:x[0])
        return out if out else None
    except Exception:
        return None

# ===== YAHOO =====
def fetch_yahoo_klines(symbol: str, interval: str) -> Optional[List[List[float]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": "180d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        j = (r.json().get("chart") or {}).get("result") or []
        if not j:
            return None
        j = j[0]
        ts = j.get("timestamp") or []
        q = ((j.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs, lows, closes = q.get("open"), q.get("high"), q.get("low"), q.get("close")
        if not (ts and opens and highs and lows and closes):
            return None
        out=[]
        for i in range(min(len(ts), len(opens), len(highs), len(lows), len(closes))):
            try:
                o=float(opens[i]); h=float(highs[i]); l=float(lows[i]); c=float(closes[i])
                if h<=0 or l<=0: continue
                out.append([int(ts[i]),o,h,l,c])
            except Exception:
                continue
        return out if out else None
    except Exception:
        return None

# ===== UNIVERSE / ALIASES =====
CRYPTO_BASES = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","DOT","LINK","LTC","MATIC","TON","ATOM","NEAR"]

ALIAS_TO_BB = {
    # US indices (no Russell)
    "ES":"US500USDT","^GSPC":"US500USDT",
    "NQ":"US100USDT","^NDX":"US100USDT",
    "YM":"US30USDT","^DJI":"US30USDT",
    "VIX":"VIXUSDT","DX":"DXYUSDT",
    # Global indices
    "DE40":"DE40USDT","FR40":"FR40USDT","UK100":"UK100USDT","JP225":"JP225USDT",
    "HK50":"HK50USDT","CN50":"CN50USDT","AU200":"AU200USDT","ES35":"ES35USDT","IT40":"IT40USDT",
    # Metals / Energy
    "GC":"XAUUSDT","SI":"XAGUSDT","HG":"XCUUSDT","PL":"XPTUSDT","PA":"XPDUSDT",
    "CL":"OILUSDT","BZ":"BRENTUSDT","NG":"GASUSDT",
    # FX (if present on Bybit)
    "EURUSD":"EURUSD","GBPUSD":"GBPUSD","USDJPY":"USDJPY","AUDUSD":"AUDUSD","NZDUSD":"NZDUSD",
    "USDCAD":"USDCAD","USDCHF":"USDCHF",
}

TOKEN_STOCKS = {
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","BRKB","AVGO","NFLX","AMD",
    "JPM","V","MA","UNH","LLY","XOM","KO","PEP"
}

YF_ONLY_DEFAULT = [
    "IMOEX.ME","RTSI.ME",
    "GAZP.ME","SBER.ME","LKOH.ME","ROSN.ME","TATN.ME","ALRS.ME","GMKN.ME","YNDX.ME",
    "MAGN.ME","MTSS.ME","CHMF.ME","AFLT.ME","PHOR.ME","MOEX.ME","BELU.ME","PIKK.ME","VTBR.ME","IRAO.ME"
]

# ===== FX DETECTION =====
FX_ISO = {"USD","EUR","JPY","GBP","AUD","NZD","CHF","CAD","MXN","CNY","HKD","SGD",
          "SEK","NOK","DKK","ZAR","TRY","PLN","CZK","HUF","ILS","KRW","TWD","THB",
          "INR","BRL","RUB","AED","SAR"}

def is_fx_pair(sym: str) -> bool:
    s = "".join(ch for ch in (sym or "").upper() if ch.isalpha())
    return len(s) >= 6 and s[:3] in FX_ISO and s[3:6] in FX_ISO

def fx_to_yahoo(sym: str) -> str:
    """
    Унифицированное преобразование FX-пары в тикер Yahoo:
    - если базовая валюта = USD (USDJPY) → 'JPY=X'
    - иначе стандартный формат, как на Yahoo: 'EURUSD=X', 'GBPUSD=X' и т.п.
    """
    letters = "".join(ch for ch in (sym or "").upper() if ch.isalpha())
    if len(letters) < 6:
        return letters
    base = letters[:3]
    quote = letters[3:6]
    if base == "USD":
        return f"{quote}=X"
    return f"{base}{quote}=X"

# ===== DISPLAY =====
def to_usdt_display(sym: str) -> str:
    s = (sym or "").upper().strip()
    # FX (включая тикеры вида XXXYYY и XXXYYY=X / JPY=X и т.п.)
    if is_fx_pair(s) or s.endswith("=X"):
        letters = "".join(ch for ch in s if ch.isalpha())
        base = quote = ""
        if s.endswith("=X"):
            if len(letters) == 4:
                # JPY=X, EUR=X → USD/JPY, USD/EUR
                quote = letters[:3]
                base = "USD"
            elif len(letters) >= 6:
                base = letters[:3]
                quote = letters[3:6]
        else:
            if len(letters) >= 6:
                base = letters[:3]
                quote = letters[3:6]
        if base and quote:
            return f"{base}{quote}-USD"
        # fallback, если что-то необычное
        return letters + "-USD" if letters else s
    # РФ тикеры .ME — как есть
    if s.endswith(".ME"):
        return s
    # прочее → -USDT
    if s.endswith("PERP"): return f"{s[:-4]}-USDT"
    if s.endswith("USDT") and "-" not in s: return f"{s[:-4]}-USDT"
    if s.endswith("-USD"): return s
    if s.endswith("-USDT"): return s
    if s.endswith("USD") and "-" not in s and len(s)>3: return s[:-3] + "-USDT"
    return s

def format_signal(symbol: str, sig: str, zone: Optional[str], src_tag: str) -> str:
    arrow  = "🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status = "⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    return f"{to_usdt_display(symbol)} [{src_tag}] {arrow}{status}".strip()

# ===== HELPERS =====
def bybit_symbol_for_alias(sym: str) -> Optional[str]:
    s = (sym or "").upper().strip().replace(" ", "")
    s = s.replace("=F","").replace("=X","").lstrip("^")
    if s in ALIAS_TO_BB:
        return ALIAS_TO_BB[s]
    s_norm = s.replace(".", "")  # BRK.B -> BRKB
    if s_norm in TOKEN_STOCKS:
        return s_norm + "USDT"
    return None

def _choose_src_tag(src4: str, src1: str) -> str:
    return "BB" if ("BB" in (src4, src1)) else "YF"

# ===== FETCH ROUTERS =====
def fetch_crypto(base: str, interval: str) -> Tuple[Optional[List[List[float]]], str, str]:
    # 1) Bybit PERP
    bb_linear = base + "USDT"
    bb_perp   = base + "PERP"
    if bb_linear in _BB_LINEAR:
        d = fetch_bybit_klines(bb_linear, interval, "linear")
        if d: return d, bb_linear, "BB"
    if bb_perp in _BB_LINEAR:
        d = fetch_bybit_klines(bb_perp, interval, "linear")
        if d: return d, bb_perp, "BB"
    # 2) Bybit SPOT
    bb_spot = base + "USDT"
    if bb_spot in _BB_SPOT:
        d = fetch_bybit_klines(bb_spot, interval, "spot")
        if d: return d, bb_spot, "BB"
    # 3) Yahoo SPOT
    return fetch_yahoo_klines(f"{base}-USD", interval), f"{base}-USD", "YF"

def fetch_other(symbol_hint: str, interval: str) -> Tuple[Optional[List[List[float]]], str, str]:
    bb = (bybit_symbol_for_alias(symbol_hint) or symbol_hint).upper()

    # Bybit linear сначала
    if bb in _BB_LINEAR:
        d = fetch_bybit_klines(bb, interval, "linear")
        if d: return d, bb, "BB"

    # Резерв Yahoo
    if bb.endswith(".ME"):
        return fetch_yahoo_klines(bb, interval), bb, "YF"

    if is_fx_pair(bb):
        yf = fx_to_yahoo(bb)
        return fetch_yahoo_klines(yf, interval), yf, "YF"

    if "USDT" in bb and "-" not in bb:
        yf = bb.replace("USDT", "-USD")
    elif "-" not in bb:
        yf = bb + "-USD"
    else:
        yf = bb
    return fetch_yahoo_klines(yf, interval), yf, "YF"

# ===== PLAN =====
def build_plan() -> List[Tuple[str, str]]:
    plan: List[Tuple[str,str]] = []
    for b in CRYPTO_BASES:
        plan.append(("CRYPTO", b))
    keys = sorted(set(list(ALIAS_TO_BB.keys()) + list(TOKEN_STOCKS)))
    for k in keys:
        plan.append(("OTHER", k))
    for y in YF_ONLY_DEFAULT:
        plan.append(("YF_ONLY", y))
    return plan

# ===== CORE =====
def process_crypto(base: str) -> bool:
    k4, name4, s4 = fetch_crypto(base, KLINE_4H)
    k1, name1, s1 = fetch_crypto(base, KLINE_1D)
    if not k4 or not k1:
        return False
    d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
    if not d4 or not d1:
        return False
    v4 = last_closed(d4); v1 = last_closed(d1)
    z4 = zone_of(v4); z1 = zone_of(v1)
    open4 = k4[-2][0]; open1 = k1[-2][0]
    dual_bar_id = max(open4, open1)
    sym = name4 or name1 or base
    tag = _choose_src_tag(s4, s1)
    if z4 and z1 and z4==z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{sig}|{z4}|{dual_bar_id}"
        return _broadcast_signal(format_signal(sym, sig, z4, tag), key)
    if z4 and not z1 and candle_pattern(k4):
        key = f"{sym}|1TF+CAN@4H|{z4}|{open4}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z4, tag), key)
    if z1 and not z4 and candle_pattern(k1):
        key = f"{sym}|1TF+CAN@1D|{z1}|{open1}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z1, tag), key)
    return False

def process_other(hint: str) -> bool:
    k4, name4, s4 = fetch_other(hint, KLINE_4H)
    k1, name1, s1 = fetch_other(hint, KLINE_1D)
    if not k4 or not k1:
        return False
    d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
    if not d4 or not d1:
        return False
    v4 = last_closed(d4); v1 = last_closed(d1)
    z4 = zone_of(v4); z1 = zone_of(v1)
    open4 = k4[-2][0]; open1 = k1[-2][0]
    dual_bar_id = max(open4, open1)
    sym = name4 or name1 or hint
    tag = _choose_src_tag(s4, s1)
    if z4 and z1 and z4==z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{sig}|{z4}|{dual_bar_id}"
        return _broadcast_signal(format_signal(sym, sig, z4, tag), key)
    if z4 and not z1 and candle_pattern(k4):
        key = f"{sym}|1TF+CAN@4H|{z4}|{open4}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z4, tag), key)
    if z1 and not z4 and candle_pattern(k1):
        key = f"{sym}|1TF+CAN@1D|{z1}|{open1}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z1, tag), key)
    return False

def process_yf_only(sym: str) -> bool:
    k4 = fetch_yahoo_klines(sym, KLINE_4H)
    k1 = fetch_yahoo_klines(sym, KLINE_1D)
    if not k4 or not k1:
        return False
    d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
    if not d4 or not d1:
        return False
    v4 = last_closed(d4); v1 = last_closed(d1)
    z4 = zone_of(v4); z1 = zone_of(v1)
    open4 = k4[-2][0]; open1 = k1[-2][0]
    dual_bar_id = max(open4, open1)
    tag = "YF"
    if z4 and z1 and z4==z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{sig}|{z4}|{dual_bar_id}"
        return _broadcast_signal(format_signal(sym, sig, z4, tag), key)
    if z4 and not з1 and candle_pattern(k4):
        key = f"{sym}|1TF+CAN@4H|{з4}|{open4}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", з4, tag), key)
    if з1 and not z4 and candle_pattern(k1):
        key = f"{sym}|1TF+CAN@1D|{з1}|{open1}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", з1, tag), key)
    return False

def main():
    while True:
        refresh_bybit_instruments()
        plan = build_plan()
        for kind, name in plan:
            if kind == "CRYPTO":
                process_crypto(name)
            elif kind == "OTHER":
                process_other(name)
            else:
                process_yf_only(name)
            time.sleep(1)
        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()