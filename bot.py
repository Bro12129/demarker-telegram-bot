# bot.py — DeMarker-28h (BB PERP→BB SPOT→YF SPOT; only 15 crypto; no dupes; USDT display; group-only)

import os, time, json, requests
from typing import List, Dict, Optional, Tuple, Set

# ============ CONFIG ============
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN  = int(os.getenv("DEM_LEN", "28"))
DEM_OB   = float(os.getenv("DEM_OB", "0.70"))
DEM_OS   = float(os.getenv("DEM_OS", "0.30"))

KLINE_4H   = os.getenv("KLINE_4H", "4h")
KLINE_1D   = os.getenv("KLINE_1D", "1d")
POLL_HOURS = int(os.getenv("POLL_HOURS", "1"))
POLL_SECONDS = POLL_HOURS * 3600

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ============ STATE ============
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

# ============ TELEGRAM (только группы -100…) ============
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

# ============ INDICATORS ============
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

def engulfing_with_prior(ohlc, idx):
    if len(ohlc)<4: return False
    o0,h0,l0,c0=ohlc[idx][1:5]; o1,h1,l1,c1=ohlc[idx-1][1:5]
    o2,c2=ohlc[idx-2][1],ohlc[idx-2][4]; o3,c3=ohlc[idx-3][1],ohlc[idx-3][4]
    bull0=c0>=o0; bull2=c2>=o2; bull3=c3>=o3
    if bull0:
        return (not bull2 and not bull3) and (min(o0,c0)<=min(o1,c1)) and (max(o0,c0)>=max(o1,c1))
    else:
        return (bull2 and bull3) and (min(o0,c0)<=min(o1,c1)) and (max(o0,c0)>=max(o1,c1))

def candle_pattern(ohlc):
    if not ohlc or len(ohlc)<4: return False
    return wick_ge_body_pct(ohlc,-2,0.25) or engulfing_with_prior(ohlc,-2)

# ============ SOURCES ============
BYBIT_BASE   = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_INSTR_EP  = f"{BYBIT_BASE}/v5/market/instruments-info"
BB_KLINES_EP = f"{BYBIT_BASE}/v5/market/kline"
BB_TIMEOUT   = 15

_BB_INFO: Dict[str, Dict[str, Optional[str]]] = {}  # symbol -> {"base":..., "quote":..., "cat":...}
_BB_LINEAR: Set[str] = set()  # PERP (linear)
_BB_SPOT:   Set[str] = set()

# 15 популярных крипто-пар (база)
ALLOWED_CRYPTO: List[str] = [
    "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","LINK",
    "LTC","MATIC","TON","TRX","SHIB"
]
ALLOWED_CRYPTO_SET = set(ALLOWED_CRYPTO)

def refresh_bybit_instruments():
    """Читаем каталоги: linear (перпы) и spot. Сохраняем base/quote и категорию."""
    global _BB_INFO, _BB_LINEAR, _BB_SPOT
    _BB_INFO = {}; _BB_LINEAR=set(); _BB_SPOT=set()
    try:
        # linear = перпы USDT
        r = requests.get(BB_INSTR_EP, params={"category":"linear"}, timeout=BB_TIMEOUT)
        if r.status_code == 200:
            lst = (r.json().get("result") or {}).get("list") or []
            for it in lst:
                sym = str(it.get("symbol","")).upper().strip()
                base = (it.get("baseCoin") or it.get("base") or "").upper()
                quote= (it.get("quoteCoin") or it.get("quote") or "").upper()
                _BB_INFO[sym] = {"base": base, "quote": quote, "cat":"linear"}
                _BB_LINEAR.add(sym)
        # spot
        r = requests.get(BB_INSTR_EP, params={"category":"spot"}, timeout=BB_TIMEOUT)
        if r.status_code == 200:
            lst = (r.json().get("result") or {}).get("list") or []
            for it in lst:
                sym = str(it.get("symbol","")).upper().strip()   # BTCUSDT и т.п.
                base = (it.get("baseCoin") or it.get("base") or "").upper()
                quote= (it.get("quoteCoin") or it.get("quote") or "").upper()
                _BB_INFO[sym] = {"base": base, "quote": quote, "cat":"spot"}
                _BB_SPOT.add(sym)
    except Exception:
        return

def fetch_bybit_klines(symbol: str, interval: str, category: str, limit: int = 600) -> Optional[List[List[float]]]:
    iv = "240" if interval == "4h" else ("D" if interval.lower()=="1d" else interval)
    try:
        r = requests.get(BB_KLINES_EP, params={"category":category, "symbol":symbol, "interval":iv, "limit":str(limit)}, timeout=BB_TIMEOUT)
        if r.status_code != 200:
            return None
        lst = (r.json().get("result") or {}).get("list") or []
        if not lst: return None
        out=[]
        for k in lst:
            ts = int(k[0]); 
            if ts > 10**12: ts //= 1000
            o=float(k[1]); h=float(k[2]); l=float(k[3]); c=float(k[4])
            if h<=0 or l<=0: continue
            out.append([ts,o,h,l,c])
        out.sort(key=lambda x:x[0])
        return out if out else None
    except Exception:
        return None

# --- Yahoo (SPOT fallback) ---
def fetch_yahoo_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": "180d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200: return None
        j = (r.json().get("chart") or {}).get("result") or []
        if not j: return None
        j = j[0]
        ts = j.get("timestamp") or []
        q = ((j.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs, lows, closes = q.get("open"), q.get("high"), q.get("low"), q.get("close")
        if not (ts and opens and highs and lows and closes): return None
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

# ============ ALIASES (индексы/металлы/FX → Bybit) ============
_ALIAS_TO_BB = {
    # Индексы США
    "ES":"US500USDT","NQ":"US100USDT","YM":"US30USDT","RTY":"US2000USDT",
    "^GSPC":"US500USDT","^NDX":"US100USDT","^DJI":"US30USDT","^RUT":"US2000USDT",
    "VIX":"VIXUSDT","DX":"DXYUSDT",
    # Глобальные индексы
    "DE40":"DE40USDT","FR40":"FR40USDT","UK100":"UK100USDT","JP225":"JP225USDT",
    "HK50":"HK50USDT","CN50":"CN50USDT","AU200":"AU200USDT","ES35":"ES35USDT","IT40":"IT40USDT",
    # Металлы / Энергия
    "GC":"XAUUSDT","SI":"XAGUSDT","HG":"XCUUSDT","PL":"XPTUSDT","PA":"XPDUSDT",
    "CL":"OILUSDT","BZ":"BRENTUSDT","NG":"GASUSDT",
    # FX (если есть как перпы)
    "EURUSD":"EURUSD","GBPUSD":"GBPUSD","USDJPY":"USDJPY","AUDUSD":"AUDUSD","NZDUSD":"NZDUSD",
    "USDCAD":"USDCAD","USDCHF":"USDCHF",
}

def bb_from_other(sym: str) -> Optional[str]:
    s = (sym or "").upper().strip().replace(" ", "")
    s = s.replace("=F","").replace("=X","").lstrip("^")
    return _ALIAS_TO_BB.get(s)

# ============ CRYPTO ROUTER (PERP→SPOT→YF) ============
def _choose_crypto_symbol(base: str) -> Tuple[str, str]:
    """
    Вернёт (kind, name):
      kind ∈ {"BB_LINEAR","BB_SPOT","YF_SPOT"}
      name = символ для запроса свечей
    """
    # 1) Bybit PERP (linear): BTCUSDT, но иногда Bybit даёт LTCPERP — нормализуем
    linear = base + "USDT"
    perp_alt = base + "PERP"
    if linear in _BB_LINEAR:
        return "BB_LINEAR", linear
    if perp_alt in _BB_LINEAR:
        return "BB_LINEAR", perp_alt

    # 2) Bybit SPOT
    spot = base + "USDT"
    if spot in _BB_SPOT:
        return "BB_SPOT", spot

    # 3) Yahoo spot
    return "YF_SPOT", f"{base}-USD"

def fetch_crypto_klines(base: str, interval: str) -> Tuple[Optional[List[List[float]]], Optional[str], Optional[str]]:
    kind, name = _choose_crypto_symbol(base)
    if kind == "BB_LINEAR":
        data = fetch_bybit_klines(name, interval, category="linear")
        return data, "BB", name
    if kind == "BB_SPOT":
        data = fetch_bybit_klines(name, interval, category="spot")
        return data, "BB", name
    data = fetch_yahoo_klines(name, interval)
    return data, "YF", name

# ============ DISPLAY (BASE-USDT) ============
def _to_usdt_display(sym: str) -> str:
    s = (sym or "").upper().strip()
    if s.endswith("PERP"):  # LTCPERP → LTC-USDT
        return f"{s[:-4]}-USDT"
    if s.endswith("USDT") and "-" not in s:
        return f"{s[:-4]}-USDT"
    if s.endswith("-USD"):
        return s[:-4] + "-USDT"
    if s.endswith("-USDT"):
        return s
    if s.endswith("USD") and "-" not in s and len(s) > 3:
        return s[:-3] + "-USDT"
    return s

def format_signal(symbol: str, sig: str, zone: Optional[str], src: str) -> str:
    arrow="🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status="⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    src_tag = "[BB]" if src=="BB" else ("[YF]" if src=="YF" else "")
    disp = _to_usdt_display(symbol)
    return f"{disp} {src_tag} {arrow}{status}".strip()

# ============ SCAN LIST ============
# Yahoo запас: РФ или иные, которые точно не на Bybit
STATIC_YF: List[str] = ["IMOEX.ME","RTSI.ME","RF"]

CRYPTO_BASES = ALLOWED_CRYPTO  # только 15 мейджоров
OTHER_BYBIT_SYMBOLS: Set[str] = set()  # индексы/металлы/FX (из _ALIAS_TO_BB значений)

def rebuild_scan_universe() -> List[Tuple[str, str]]:
    """
    Возвращает список (kind, name):
      kind ∈ {"CRYPTO","BYBIT_OTHER","YF_ONLY"}
    CRYPTO — база (BTC/ETH/…): будет PERP→SPOT→YF.
    BYBIT_OTHER — прямые bybit-символы (индексы/металлы/FX).
    YF_ONLY — статические YF тикеры.
    """
    global OTHER_BYBIT_SYMBOLS
    OTHER_BYBIT_SYMBOLS = set(_ALIAS_TO_BB.values())  # US500USDT, XAUUSDT, ...

    items: List[Tuple[str, str]] = []
    # 1) Крипта: 15 баз
    for b in CRYPTO_BASES:
        items.append(("CRYPTO", b))
    # 2) Индексы/металлы/FX с Bybit, если реально есть в каталоге
    bb_all = set(_BB_LINEAR) | set(_BB_SPOT)
    for sym in sorted(OTHER_BYBIT_SYMBOLS):
        if sym in bb_all:
            items.append(("BYBIT_OTHER", sym))
    # 3) YF-only
    for y in STATIC_YF:
        items.append(("YF_ONLY", y))
    return items

# ============ CORE ============
def process_crypto(base: str) -> bool:
    k4, s4, name4 = fetch_crypto_klines(base, KLINE_4H)
    k1, s1, name1 = fetch_crypto_klines(base, KLINE_1D)
    if not k4 or not k1: 
        return False
    src = "BB" if (s4=="BB" or s1=="BB") else "YF"
    d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
    if not d4 or not d1: return False
    v4 = last_closed(d4); v1 = last_closed(d1)
    z4 = zone_of(v4);     z1 = zone_of(v1)
    open4 = k4[-2][0]; open1 = k1[-2][0]
    dual_bar_id = max(open4, open1)

    sym_for_key = (name4 or name1 or base)
    if z4 and z1 and z4 == z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym_for_key}|{src}|{sig}|{z4}|{dual_bar_id}"
        return _broadcast_signal(format_signal(sym_for_key, sig, z4, src), key)
    if z4 and not z1 and candle_pattern(k4):
        key = f"{sym_for_key}|{src}|1TF+CAN@4H|{z4}|{open4}"
        return _broadcast_signal(format_signal(sym_for_key, "1TF+CAN", z4, src), key)
    if z1 and not z4 and candle_pattern(k1):
        key = f"{sym_for_key}|{src}|1TF+CAN@1D|{z1}|{open1}"
        return _broadcast_signal(format_signal(sym_for_key, "1TF+CAN", z1, src), key)
    return False

def process_bybit_other(sym: str) -> bool:
    # всегда Bybit linear (если есть), иначе Yahoo по прокси имени
    k4 = fetch_bybit_klines(sym, KLINE_4H, category="linear") or fetch_yahoo_klines(sym.replace("USDT","-USD"), KLINE_4H)
    k1 = fetch_bybit_klines(sym, KLINE_1D, category="linear") or fetch_yahoo_klines(sym.replace("USDT","-USD"), KLINE_1D)
    if not k4 or not k1: return False
    src = "BB" if isinstance(k4[0][0], int) and isinstance(k1[0][0], int) and sym in _BB_LINEAR else "YF"
    d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
    if not d4 or not d1: return False
    v4 = last_closed(d4); v1 = last_closed(d1)
    z4 = zone_of(v4);     z1 = zone_of(v1)
    open4 = k4[-2][0]; open1 = k1[-2][0]
    dual_bar_id = max(open4, open1)

    if z4 and z1 and z4 == z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{src}|{sig}|{z4}|{dual_bar_id}"
        return _broadcast_signal(format_signal(sym, sig, z4, src), key)
    if z4 and not z1 and candle_pattern(k4):
        key = f"{sym}|{src}|1TF+CAN@4H|{z4}|{open4}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z4, src), key)
    if z1 and not z4 and candle_pattern(k1):
        key = f"{sym}|{src}|1TF+CAN@1D|{z1}|{open1}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z1, src), key)
    return False

def process_yf_only(sym: str) -> bool:
    k4 = fetch_yahoo_klines(sym, KLINE_4H)
    k1 = fetch_yahoo_klines(sym, KLINE_1D)
    if not k4 or not k1: return False
    d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
    if not d4 or not d1: return False
    v4 = last_closed(d4); v1 = last_closed(d1)
    z4 = zone_of(v4);     z1 = zone_of(v1)
    open4 = k4[-2][0]; open1 = k1[-2][0]
    dual_bar_id = max(open4, open1)
    src = "YF"
    if z4 and z1 and z4 == z1:
        sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
        key = f"{sym}|{src}|{sig}|{z4}|{dual_bar_id}"
        return _broadcast_signal(format_signal(sym, sig, z4, src), key)
    if z4 and not z1 and candle_pattern(k4):
        key = f"{sym}|{src}|1TF+CAN@4H|{z4}|{open4}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z4, src), key)
    if z1 and not z4 and candle_pattern(k1):
        key = f"{sym}|{src}|1TF+CAN@1D|{z1}|{open1}"
        return _broadcast_signal(format_signal(sym, "1TF+CAN", z1, src), key)
    return False

def main():
    while True:
        refresh_bybit_instruments()
        plan = rebuild_scan_universe()

        for kind, name in plan:
            if kind == "CRYPTO":
                process_crypto(name)        # name = base (BTC/ETH/…)
            elif kind == "BYBIT_OTHER":
                process_bybit_other(name)   # name = bybit symbol (US500USDT, XAUUSDT, …)
            else:
                process_yf_only(name)       # name = YF symbol (IMOEX.ME, RF, …)
            time.sleep(1)

        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()