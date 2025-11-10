# bot.py — DeMarker-28h Hybrid (BingX priority, Yahoo fallback)
# clean: only signals, no logs, group-only, 60-minute polling
import os, time, json, requests
from typing import List, Dict, Optional, Tuple

# ============ CONFIG ============
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")   # -100..., можно через запятую
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN  = 28
DEM_OB   = 0.70
DEM_OS   = 0.30
POLL_HOURS = 1
POLL_SECONDS = POLL_HOURS * 3600  # 60 min

TELEGRAM_GROUP_ONLY = True  # только группа

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
    """Удаляем ключи старше N дней (по времени отправки)."""
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
    raw = (TELEGRAM_CHAT or "").strip()
    if not raw:
        return []
    toks = [x.strip() for x in raw.split(",") if x.strip()]
    # Разрешаем только отрицательные chat_id (частные чаты/группы).
    toks = [t for t in toks if (t.startswith("-100") or (t.startswith("-") and t[1:].isdigit()))]
    # Если включён "только группы" — пропускаем только -100...
    if TELEGRAM_GROUP_ONLY:
        toks = [t for t in toks if t.startswith("-100")]
    return toks

def tg_send_one(cid: str, text: str) -> bool:
    try:
        r = requests.post(f"{TG_API}/sendMessage", json={"chat_id": cid, "text": text}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def _broadcast_signal(text: str, signal_key: str) -> bool:
    """
    Дедупликация по ключу signal_key + chat_id.
    """
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

# === DISPLAY (оставляем, но НЕ используем в тексте, чтобы не путать тикеры) ===
def normalize_symbol(raw: str) -> str:
    s = (raw or "").upper().strip()
    s = s.replace("=F", "").replace("=X", "")
    s = s.replace("/", "-")
    if s.startswith("^"): s = s[1:]
    s = s.replace(" ", "")
    FX = {
        "USD","EUR","JPY","GBP","AUD","NZD","CHF","CAD","MXN","CNY","HKD","SGD",
        "SEK","NOK","DKK","ZAR","TRY","PLN","CZK","HUF","ILS","KRW","TWD","THB",
        "INR","BRL","RUB","AED","SAR"
    }
    letters = "".join(ch for ch in s if ch.isalpha())
    is_fx = len(letters) >= 6 and letters[:3] in FX and letters[3:6] in FX
    if is_fx:
        pair6 = letters[:6]
        return pair6 + "-USD"
    core = s.split("-")[0]
    return core + "-USDT"

# ============ SYMBOL UNIVERSE ============
YF_SYMBOLS = [
    # === CRYPTO ===
    "BTC-USD","ETH-USD","SOL-USD","BNB-USD","XRP-USD","ADA-USD","DOGE-USD","AVAX-USD","DOT-USD","LINK-USD",
    "LTC-USD","MATIC-USD","TON-USD","ATOM-USD","NEAR-USD","FIL-USD","AAVE-USD","XMR-USD","LDO-USD","INJ-USD",
    "APT-USD","SUI-USD","ARB-USD","OP-USD","PEPE-USD","SHIB-USD",
    # === FUTURES ===
    "ES=F","NQ=F","YM=F","RTY=F","VX=F","DX=F",
    "GC=F","SI=F","HG=F","PL=F","PA=F","CL=F","BZ=F","NG=F","RB=F","HO=F",
    "ZC=F","ZS=F","ZW=F","KC=F","SB=F","CC=F","6E=F","6J=F","6B=F","6A=F","6C=F","6S=F","BTC=F","ETH=F",
    # === FX ===
    "EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","NZDUSD=X","USDCAD=X","USDCHF=X",
    # === INDICES ===
    "^GSPC","^NDX","^DJI","^RUT","^VIX","^FTSE","^GDAXI","^FCHI","^STOXX50E","^HSI","^N225","^AORD","^SPTSX","^BSESN","^SHCOMP",
    "IMOEX.ME","RTSI.ME",
    # === US STOCKS ===
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK-B","AVGO","JNJ","JPM","V","MA","UNH","HD","LLY","XOM","KO","PEP",
    # === RUSSIAN STOCKS (.ME) ===
    "GAZP.ME","SBER.ME","LKOH.ME","NVTK.ME","ROSN.ME","TATN.ME","ALRS.ME","GMKN.ME","YNDX.ME","POLY.ME",
    "MAGN.ME","MTSS.ME","CHMF.ME","AFLT.ME","PHOR.ME","MOEX.ME","BELU.ME","PIKK.ME","VTBR.ME","IRAO.ME"
]

# ============ FETCHERS ============
HEADERS = {"User-Agent": "Mozilla/5.0"}

# BingX API (PERP)
BINGX_BASE = os.getenv("BINGX_BASE", "https://open-api.bingx.com")
BX_CONTRACTS_EP = f"{BINGX_BASE}/openApi/swap/v2/quote/contracts"
BX_KLINES_EP    = f"{BINGX_BASE}/openApi/swap/v3/quote/klines"
BX_TIMEOUT      = 15
_BX_SYMBOLS: Dict[str, str] = {}  # symbol -> category

def refresh_bingx_contracts() -> None:
    """Обновляем список доступных PERP-контрактов BingX (crypto, index, fx, metal, xstock)."""
    global _BX_SYMBOLS
    _BX_SYMBOLS = {}
    try:
        r = requests.get(BX_CONTRACTS_EP, timeout=BX_TIMEOUT)
        if r.status_code != 200:
            return
        j = r.json()
        data = j.get("data") or j.get("result") or []
        for it in data:
            sym = str(it.get("symbol","")).upper().strip()
            cat = str(it.get("category","")).upper().strip()
            if sym:
                _BX_SYMBOLS[sym] = cat or "UNKNOWN"
    except Exception:
        return

def to_bingx(sym: str) -> Optional[str]:
    """Грубый маппинг Yahoo-стиля в BingX-стиль для PERP."""
    s = (sym or "").upper().strip().replace(" ", "")
    s = s.replace("=F","").replace("=X","").lstrip("^")
    # крипта Yahoo: BTC-USD -> BTC-USDT
    if s.endswith("-USD") and len(s) <= 10:
        return f"{s[:-4]}-USDT"
    # индексы и производные -> US500/US100/...
    idx_map = {
        "ES":"US500","NQ":"US100","YM":"US30","RTY":"US2000","VX":"VIX","DX":"DXY",
        "GSPC":"US500","NDX":"US100","DJI":"US30","RUT":"US2000","VIX":"VIX","DXY":"DXY"
    }
    if s in idx_map:
        return idx_map[s] + "-USDT"
    # металлы
    metal_map = {"GC":"XAU","SI":"XAG","HG":"XCU","PL":"XPT","PA":"XPD"}
    if s in metal_map:
        return metal_map[s] + "-USDT"
    # форекс: EURUSD -> EUR-USD
    FX = {"USD","EUR","JPY","GBP","AUD","NZD","CHF","CAD","MXN","CNY","HKD","SGD","SEK","NOK","DKK","ZAR","TRY","PLN","CZK","HUF","ILS","KRW","TWD","THB","INR","BRL","RUB","AED","SAR"}
    letters = "".join(ch for ch in s if ch.isalpha())
    if len(letters) >= 6 and letters[:3] in FX and letters[3:6] in FX:
        return letters[:3] + "-" + letters[3:6]
    # уже в формате XXX-USDT
    if "-USDT" in s:
        return s
    return None

def fetch_bingx_klines(symbol_bx: str, interval: str, limit: int = 600) -> Optional[List[List[float]]]:
    try:
        params = {"symbol": symbol_bx, "interval": interval, "limit": str(limit)}
        r = requests.get(BX_KLINES_EP, params=params, timeout=BX_TIMEOUT)
        if r.status_code != 200:
            return None
        j = r.json()
        data = j.get("data") or j.get("result") or []
        out=[]
        for k in data:
            ts = int(k[0]); 
            if ts > 10**12: ts //= 1000  # ms -> s
            o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
            if h<=0 or l<=0: continue
            out.append([ts,o,h,l,c])
        return out if out else None
    except Exception:
        return None

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

# ============ ROUTER ============
# Возвращает (klines, src) где src ∈ {"BX","YF",None}
def fetch_klines(sym: str, interval: str) -> Tuple[Optional[List[List[float]]], Optional[str]]:
    bx = to_bingx(sym)
    if bx and _BX_SYMBOLS and (bx in _BX_SYMBOLS):
        data = fetch_bingx_klines(bx, interval)
        if data:
            return data, "BX"
    # фоллбэк на Yahoo, если BingX-символа нет/не дал данные
    data = fetch_yahoo_klines(sym, interval)
    if data:
        return data, "YF"
    return None, None

def format_signal(symbol: str, sig: str, zone: Optional[str], src: str) -> str:
    arrow="🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status="⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    src_tag = "[BX]" if src=="BX" else "[YF]"
    # показываем СЫРОЙ тикер и метку источника, чтобы не путать инструменты
    return f"{symbol} {src_tag} {arrow}{status}"

# ============ CORE ============
def process_symbol(sym: str) -> bool:
    try:
        k4, s4 = fetch_klines(sym, "4h")
        k1, s1 = fetch_klines(sym, "1d")
        if not k4 or not k1: 
            return False

        # приоритет источника в тексте/ключе: если хотя бы один ТФ — BX, считаем src="BX"
        src = "BX" if (s4 == "BX" or s1 == "BX") else "YF"

        d4 = demarker_series(k4, DEM_LEN)
        d1 = demarker_series(k1, DEM_LEN)
        if not d4 or not d1: 
            return False

        v4 = last_closed(d4); v1 = last_closed(d1)
        z4 = zone_of(v4);   z1 = zone_of(v1)

        # open time последних ЗАКРЫТЫХ баров
        open4 = k4[-2][0]
        open1 = k1[-2][0]
        dual_bar_id = max(open4, open1)  # повтор, когда закрывается новый бар на любом ТФ

        # --- LIGHT / L+CAN: обе DeM в одной зоне
        if z4 and z1 and z4 == z1:
            sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
            key = f"{sym}|{src}|{sig}|{z4}|{dual_bar_id}"
            return _broadcast_signal(format_signal(sym, sig, z4, src), key)

        # --- 1TF+CAN: только один ТФ в зоне и есть свечной на этом ТФ
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
        # обновляем доступные PERP-контракты BingX в начале цикла
        refresh_bingx_contracts()

        for s in YF_SYMBOLS:
            process_symbol(s)
            time.sleep(1)

        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()