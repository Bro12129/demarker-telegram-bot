# bot.py — DeMarker-28h (Bybit → Yahoo, USDT display, group-only)
# Источник 1: Bybit (linear+inverse). Источник 2: Yahoo fallback.
# Только сигналы, без логов. Пулл раз в POLL_HOURS.

import os, time, json, requests, re
from typing import List, Dict, Optional, Tuple, Set

# ============ CONFIG ============
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN  = 28
DEM_OB   = 0.70
DEM_OS   = 0.30

KLINE_4H   = os.getenv("KLINE_4H", "4h")
KLINE_1D   = os.getenv("KLINE_1D", "1d")
POLL_HOURS = int(os.getenv("POLL_HOURS", "1"))
POLL_SECONDS = POLL_HOURS * 3600

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

# ============ TELEGRAM ============
def _chat_tokens() -> List[str]:
    """Жёстко только групповые -100…  Любые другие ID игнорируются."""
    raw = (TELEGRAM_CHAT or "").strip()
    if not raw:
        return []
    toks = [x.strip() for x in raw.split(",") if x.strip()]
    toks = [t for t in toks if t.startswith("-100")]
    return toks

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
HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- Bybit (PERP: crypto + indices/commodities/FX/token-stocks) ---
BYBIT_BASE   = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_INSTR_EP  = f"{BYBIT_BASE}/v5/market/instruments-info"
BB_KLINES_EP = f"{BYBIT_BASE}/v5/market/kline"
BB_TIMEOUT   = 15
_BB_LINEAR: Set[str] = set()
_BB_INVERSE: Set[str] = set()

def refresh_bybit_instruments():
    global _BB_LINEAR, _BB_INVERSE
    _BB_LINEAR=set(); _BB_INVERSE=set()
    try:
        for cat in ("linear","inverse"):
            r = requests.get(BB_INSTR_EP, params={"category":cat}, timeout=BB_TIMEOUT)
            if r.status_code != 200:
                continue
            j = r.json()
            lst = (j.get("result") or {}).get("list") or []
            for it in lst:
                sym = str(it.get("symbol","")).upper().strip()
                if not sym: continue
                if cat=="linear": _BB_LINEAR.add(sym)
                else: _BB_INVERSE.add(sym)
    except Exception:
        return

def fetch_bybit_klines(symbol_bb: str, interval: str, category_hint: Optional[str]=None, limit: int = 600) -> Optional[List[List[float]]]:
    # Bybit intervals: 240 for 4h, D for 1d
    iv = "240" if interval == "4h" else ("D" if interval.lower() == "1d" else interval)
    cats = [category_hint] if category_hint in ("linear","inverse") else ["linear","inverse"]
    try:
        for cat in cats:
            r = requests.get(BB_KLINES_EP, params={"category":cat, "symbol":symbol_bb, "interval":iv, "limit":str(limit)}, timeout=BB_TIMEOUT)
            if r.status_code != 200:
                continue
            j = r.json()
            lst = (j.get("result") or {}).get("list") or []
            if not lst:
                continue
            out=[]
            for k in lst:
                ts = int(k[0]); 
                if ts > 10**12: ts //= 1000
                o=float(k[1]); h=float(k[2]); l=float(k[3]); c=float(k[4])
                if h<=0 or l<=0: continue
                out.append([ts,o,h,l,c])
            out.sort(key=lambda x:x[0])
            return out if out else None
        return None
    except Exception:
        return None

# --- Yahoo (fallback) ---
def fetch_yahoo_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": "180d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200: return None
        chart = r.json().get("chart", {})
        results = chart.get("result", [])
        if not results: return None
        j = results[0]
        ts = j.get("timestamp") or []
        ind = j.get("indicators", {}).get("quote", [])
        if not ts or not ind: return None
        q = ind[0]
        opens, highs, lows, closes = q.get("open"), q.get("high"), q.get("low"), q.get("close")
        if not (opens and highs and lows and closes): return None
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

# ============ ALIASES (YF/фьючерс → Bybit) ============
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
    # FX (если есть на Bybit как перпы)
    "EURUSD":"EURUSD","GBPUSD":"GBPUSD","USDJPY":"USDJPY","AUDUSD":"AUDUSD","NZDUSD":"NZDUSD",
    "USDCAD":"USDCAD","USDCHF":"USDCHF"
}

def bb_from_other(sym: str) -> Optional[str]:
    """Map Yahoo/фьючерс/индекс/акция в Bybit-символ, если существует."""
    s = (sym or "").upper().strip().replace(" ", "")
    s = s.replace("=F","").replace("=X","").lstrip("^")
    if s in _ALIAS_TO_BB:
        return _ALIAS_TO_BB[s]
    # фьючерсные шорткоды → индексы
    idx_map = {"ES":"US500USDT","NQ":"US100USDT","YM":"US30USDT","RTY":"US2000USDT","DX":"DXYUSDT","VIX":"VIXUSDT",
               "GSPC":"US500USDT","NDX":"US100USDT","DJI":"US30USDT","RUT":"US2000USDT"}
    if s in idx_map:
        return idx_map[s]
    # акции: AAPL → AAPLUSDT (если есть токен-акция у Bybit)
    if s.isalpha() and 1 < len(s) <= 6:
        return s + "USDT"
    return None

# ============ ROUTER ============
def _is_bybit_symbol(sym: str) -> bool:
    su = (sym or "").upper().strip()
    return (su in _BB_LINEAR) or (su in _BB_INVERSE)

def fetch_klines(sym: str, interval: str) -> Tuple[Optional[List[List[float]]], Optional[str]]:
    """
    Порядок: Bybit → Yahoo.
    Возврат: (klines, src) где src ∈ {"BB","YF",None}
    """
    su = (sym or "").upper().strip()

    # 1) Если уже Bybit-символ
    if _is_bybit_symbol(su):
        data = fetch_bybit_klines(su, interval)
        if data: return data, "BB"
        # последний шанс: прокси в Yahoo
        yproxy = su.replace("USDT","-USD")
        data = fetch_yahoo_klines(yproxy, interval)
        if data: return data, "YF"
        return None, None

    # 2) Попробовать замапить в Bybit через алиасы/эвристику
    bb = bb_from_other(su)
    if bb and _is_bybit_symbol(bb):
        data = fetch_bybit_klines(bb, interval)
        if data: return data, "BB"

    # 3) Yahoo fallback по исходному имени (YF-стиль)
    data = fetch_yahoo_klines(su, interval)
    if data: return data, "YF"
    return None, None

# ============ DISPLAY (BASE-USDT) ============
def _to_usdt_display(sym: str) -> str:
    """
    Приводит к BASE-USDT для отображения.
    - Bybit: BTCUSDT → BTC-USDT; US500USDT → US500-USDT; AAPLUSDT → AAPL-USDT
    - Yahoo crypto: BTC-USD → BTC-USDT
    - FX без USDT (EURUSD и т.п.) оставляем как есть.
    """
    s = (sym or "").upper().strip()

    # Bybit: ...USDT без дефиса
    if s.endswith("USDT") and "-" not in s:
        base = s[:-4]
        return f"{base}-USDT"

    # Yahoo crypto: ...-USD → ...-USDT
    if s.endswith("-USD"):
        return s[:-4] + "-USDT"

    # Уже норм
    if s.endswith("-USDT"):
        return s

    # Безопасная попытка для USD-хвоста
    if s.endswith("USD") and "-" not in s and len(s) > 3:
        fx = {"USD","EUR","JPY","GBP","AUD","NZD","CHF","CAD","MXN","CNY","HKD","SGD",
              "SEK","NOK","DKK","ZAR","TRY","PLN","CZK","HUF","ILS","KRW","TWD","THB","INR","BRL","RUB","AED","SAR"}
        letters = "".join(ch for ch in s if ch.isalpha())
        if len(letters) == 6 and letters[:3] in fx and letters[3:6] in fx:
            return s  # EURUSD и др. FX оставляем
        return s[:-3] + "-USDT"

    return s

def format_signal(symbol: str, sig: str, zone: Optional[str], src: str) -> str:
    arrow="🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status="⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    src_tag = "[BB]" if src=="BB" else ("[YF]" if src=="YF" else "")
    disp = _to_usdt_display(symbol)
    return f"{disp} {src_tag} {arrow}{status}".strip()

# ============ SCAN UNIVERSE ============
# Статические семена для Yahoo (то, чего нет на Bybit, но нужно сканировать)
STATIC_SEEDS: List[str] = [
    # RU / индексы / примеры
    "IMOEX.ME","RTSI.ME",
    # При желании добавляй сюда любые YF-символы (GAZP.ME, SBER.ME, RF, и т.д.)
    "RF","AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","BRK-B",
    # Crypto в YF-стиле (как резерв — если на Bybit нет пары)
    "BTC-USD","ETH-USD","SOL-USD","XRP-USD","BNB-USD","ADA-USD","DOGE-USD"
]

SCAN_SYMBOLS: List[str] = []

def rebuild_scan_universe() -> None:
    """
    Сканируем:
      - все инструменты Bybit (linear+inverse);
      - плюс STATIC_SEEDS (YF-символы), без дублей.
    """
    global SCAN_SYMBOLS
    seeds: List[str] = []

    # Все Bybit-символы
    for bb in sorted(_BB_LINEAR | _BB_INVERSE):
        seeds.append(bb)

    # Добавить статические YF-семена, которых нет на Bybit
    bb_set = set(_BB_LINEAR | _BB_INVERSE)
    for y in STATIC_SEEDS:
        if y.upper() not in bb_set:
            seeds.append(y)

    # Уникализация
    seen=set(); out=[]
    for s in seeds:
        su=s.upper()
        if su in seen: continue
        seen.add(su); out.append(s)
    SCAN_SYMBOLS = out

# ============ CORE ============
def process_symbol(sym: str) -> bool:
    try:
        k4, s4 = fetch_klines(sym, KLINE_4H)
        k1, s1 = fetch_klines(sym, KLINE_1D)
        if not k4 or not k1:
            return False

        src = "BB" if (s4=="BB" or s1=="BB") else "YF"

        d4 = demarker_series(k4, DEM_LEN)
        d1 = demarker_series(k1, DEM_LEN)
        if not d4 or not d1:
            return False

        v4 = last_closed(d4); v1 = last_closed(d1)
        z4 = zone_of(v4);     z1 = zone_of(v1)

        open4 = k4[-2][0]; open1 = k1[-2][0]
        dual_bar_id = max(open4, open1)

        # LIGHT / L+CAN
        if z4 and z1 and z4 == z1:
            sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
            key = f"{sym}|{src}|{sig}|{z4}|{dual_bar_id}"
            return _broadcast_signal(format_signal(sym, sig, z4, src), key)

        # 1TF+CAN
        if z4 and not z1 and candle_pattern(k4):
            key = f"{sym}|{src}|1TF+CAN@4H|{z4}|{open4}"
            return _broadcast_signal(format_signal(sym, "1TF+CAN", z4, src), key)

        if z1 and not z4 and candle_pattern(k1):
            key = f"{sym}|{src}|1TF+CAN@1D|{z1}|{open1}"
            return _broadcast_signal(format_signal(sym, "1TF+CAN", z1, src), key)

        return False
    except Exception:
        return False

def main():
    while True:
        refresh_bybit_instruments()   # все инструменты Bybit
        rebuild_scan_universe()       # итоговый список (Bybit + статические YF)

        for s in SCAN_SYMBOLS:
            process_symbol(s)
            time.sleep(1)

        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()