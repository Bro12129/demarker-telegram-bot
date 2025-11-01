# bot.py
import os, time, json, logging, requests
from typing import List, Dict, Tuple, Optional

# ============ ENV ============
BINGX_BASE     = os.getenv("BINGX_BASE", "https://open-api.bingx.com")
KLINE_4H       = os.getenv("KLINE_4H", "4h")
KLINE_1D       = os.getenv("KLINE_1D", "1d")

DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
DEM_OB         = float(os.getenv("DEM_OB", "0.70"))
DEM_OS         = float(os.getenv("DEM_OS", "0.30"))

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEBUG_TG       = os.getenv("DEBUG_TG", "0") == "1"
DEBUG_SCAN     = os.getenv("DEBUG_SCAN", "0") == "1"
SELFTEST_PING  = os.getenv("SELFTEST_PING", "0") == "1"

# Новое: версия формата (для обхода дедупа при смене эмодзи)
FORMAT_VER     = os.getenv("FORMAT_VER", "v1")

# ============ LOGGING ============
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
log = logging.getLogger("bot")
def dprint(msg: str):
    if DEBUG_TG or DEBUG_SCAN:
        log.info(msg)

# ============ HTTP ============
def http_get(url: str, params: Dict[str, str], timeout: int = 15, tries: int = 3, pause: float = 0.4) -> Optional[Dict]:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(pause * (i + 1))
    return None

def http_post(url: str, data: Dict = None, json_body: Dict = None, timeout: int = 10) -> Optional[requests.Response]:
    try:
        if json_body is not None:
            return requests.post(url, json=json_body, timeout=timeout)
        return requests.post(url, data=data, timeout=timeout)
    except Exception:
        return None

# ============ STATE ============
def load_state(path: str) -> Dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}, "universe": []}

def save_state(path: str, data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass

STATE = load_state(STATE_PATH)

# ============ SEED ============
STATIC_SYMBOLS: List[str] = [
    "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT","ADA-USDT","DOGE-USDT",
    "TON-USDT","LTC-USDT","TRX-USDT","LINK-USDT","DOT-USDT","AVAX-USDT",
    "XAU-USDT","XAG-USDT","US100","US500","US30","US2000","VIX",
    "EUR-USD","GBP-USD","USD-JPY","AUD-USD","USD-CAD","USD-CHF"
]

# ============ SYMBOLS ============
def fetch_contracts_dynamic() -> List[str]:
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/contracts"
    data = http_get(url, params={}) or {}
    items = data.get("data") or data.get("symbolList") or []
    out: List[str] = []
    for it in items:
        sym = (it.get("symbol") or it.get("contractId") or "").upper()
        if not sym: continue
        ctype = (it.get("contractType") or it.get("type") or "").upper()
        if "PERP" not in ctype: continue
        cat = (it.get("category") or it.get("assetType") or "").lower()
        s = sym.upper()
        if s in {"US100","US500","US30","US2000","VIX","XAU-USDT","XAG-USDT"}: out.append(s); continue
        if "stock" in cat or "xstock" in cat: out.append(s); continue
        if "-" in s and len(s) == 7 and s[3] == "-": out.append(s); continue  # FX
        if s.endswith("-USDT"): out.append(s); continue                       # crypto
    return sorted(set(out))

def get_symbols() -> List[str]:
    dyn = fetch_contracts_dynamic()
    if dyn:
        universe = sorted(set(dyn) | set(STATIC_SYMBOLS))
        STATE["universe"] = universe
        save_state(STATE_PATH, STATE)
        return universe
    cached = STATE.get("universe") or []
    return cached if cached else STATIC_SYMBOLS[:]

# ============ KLINES ============
def fetch_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    url = f"{BINGX_BASE}/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    data = http_get(url, params=params)
    if not data: return None
    raw = data.get("data") or data.get("klines") or []
    out: List[List[float]] = []
    for k in raw:
        if isinstance(k, dict):
            try:
                t = int(k.get("openTime") or k.get("time") or k.get("t"))
                o = float(k.get("open")); h = float(k.get("high")); l = float(k.get("low")); c = float(k.get("close"))
            except Exception:
                continue
        else:
            try:
                t = int(k[0]); o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
            except Exception:
                continue
        if h <= 0 or l <= 0: continue
        out.append([t,o,h,l,c])
    out.sort(key=lambda x: x[0])
    return out or None

# ============ INDICATORS ============
def demarker_series(ohlc: List[List[float]], length: int) -> Optional[List[Optional[float]]]:
    if not ohlc or len(ohlc) < length + 2: return None
    highs = [x[2] for x in ohlc]; lows = [x[3] for x in ohlc]
    up = [0.0]; dn = [0.0]
    for i in range(1, len(ohlc)):
        up.append(max(highs[i]-highs[i-1], 0.0))
        dn.append(max(lows[i-1]-lows[i], 0.0))
    def sma(arr: List[float], i: int, n: int) -> float:
        s = 0.0
        for k in range(i-n+1, i+1): s += arr[k]
        return s / n
    dem: List[Optional[float]] = [None]*len(ohlc)
    for i in range(DEM_LEN, len(ohlc)):
        up_s = sma(up, i, DEM_LEN); dn_s = sma(dn, i, DEM_LEN)
        denom = up_s + dn_s
        dem[i] = (up_s/denom) if denom != 0 else 0.5
    return dem

# ============ CANDLE PATTERNS ============
def wick_ge_25pct(o: float, h: float, l: float, c: float) -> bool:
    rng = max(h-l, 1e-12)
    upper = h - max(o,c)
    lower = min(o,c) - l
    return (upper >= 0.25*rng) or (lower >= 0.25*rng)

def is_bull(c: float, o: float) -> bool:
    return c >= o

def engulfing_with_prior_opposition(ohlc: List[List[float]]) -> bool:
    if len(ohlc) < 4: return False
    o0,h0,l0,c0 = ohlc[-1][1], ohlc[-1][2], ohlc[-1][3], ohlc[-1][4]
    o1,h1,l1,c1 = ohlc[-2][1], ohlc[-2][2], ohlc[-2][3], ohlc[-2][4]
    o2,c2 = ohlc[-3][1], ohlc[-3][4]; o3,c3 = ohlc[-4][1], ohlc[-4][4]
    bull0 = is_bull(c0,o0); bull2 = is_bull(c2,o2); bull3 = is_bull(c3,o3)
    if bull0:
        if not ((not bull2) and (not bull3)): return False
        return (min(o0,c0) <= min(o1,c1)) and (max(o0,c0) >= max(o1,c1))
    else:
        if not (bull2 and bull3): return False
        return (min(o0,c0) <= min(o1,c1)) and (max(o0,c0) >= max(o1,c1))

def candle_pattern_ok(ohlc: List[List[float]]) -> bool:
    o,h,l,c = ohlc[-1][1], ohlc[-1][2], ohlc[-1][3], ohlc[-1][4]
    return wick_ge_25pct(o,h,l,c) or engulfing_with_prior_opposition(ohlc)

# ============ SIGNAL LOGIC ============
def zone_of(v: Optional[float]) -> Optional[str]:
    if v is None: return None
    if v >= DEM_OB: return "OB"
    if v <= DEM_OS: return "OS"
    return None

def classify_signal(dem4h: Optional[float], dem1d: Optional[float], has_candle: bool) -> Optional[Tuple[str, Optional[str]]]:
    z4 = zone_of(dem4h); z1 = zone_of(dem1d)
    both = (z4 is not None) and (z1 is not None) and (z4 == z1)
    one  = ((z4 is not None) ^ (z1 is not None))
    if both and has_candle:     return ("L+CAN", z4)
    if both and not has_candle: return ("LIGHT", z4)
    if one and has_candle:      return ("1TF+CAN", z4 or z1)
    return None

# ============ TELEGRAM ============
def tg_send_raw(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        dprint("TG: empty token/chat"); return False
    url = f"{TG_API}/sendMessage"
    form = {"chat_id": TELEGRAM_CHAT, "text": text, "disable_notification": True}
    jsn  = {"chat_id": TELEGRAM_CHAT, "text": text, "disable_notification": True}
    for attempt in range(1, 3+1):
        r = http_post(url, data=form)
        ok = False
        if r is not None:
            try: ok = (r.status_code == 200) and (r.json().get("ok") is True)
            except Exception: ok = False
        if ok: return True
        time.sleep(0.4 * attempt)
        r = http_post(url, json_body=jsn)
        ok = False
        if r is not None:
            try: ok = (r.status_code == 200) and (r.json().get("ok") is True)
            except Exception: ok = False
        if ok: return True
        time.sleep(0.4 * attempt)
    return False

# ==== НОВОЕ форматирование: OS -> 🟢↑, OB -> 🔴↓
def format_signal_text(symbol: str, signal_type: str, zone: Optional[str]) -> str:
    arrow = "🟢↑" if zone == "OS" else ("🔴↓" if zone == "OB" else "")
    status = "⚡" if signal_type == "LIGHT" else ("⚡🕯️" if signal_type == "L+CAN" else "🕯️")
    return f"{symbol} {arrow} {status}".strip()

def tg_send_signal(symbol: str, signal_type: str, zone: Optional[str]) -> bool:
    return tg_send_raw(format_signal_text(symbol, signal_type, zone))

# ============ CORE ============
def last_value(series: List[Optional[float]]) -> Optional[float]:
    return series[-1] if series else None

def build_dedup_key(symbol: str, signal_type: str, zone: Optional[str], last_ts: int) -> str:
    # Добавили FORMAT_VER, чтобы новая версия эмодзи не блокировалась старым дедупом
    return f"{FORMAT_VER}|{symbol}|{signal_type}|{zone or '-'}|{last_ts}"

def process_symbol(symbol: str) -> Optional[str]:
    k4 = fetch_klines(symbol, KLINE_4H, limit=max(200, DEM_LEN + 10))
    k1 = fetch_klines(symbol, KLINE_1D, limit=max(200, DEM_LEN + 10))
    if not k4 or not k1: return None

    dem4_series = demarker_series(k4, DEM_LEN)
    dem1_series = demarker_series(k1, DEM_LEN)
    if not dem4_series or not dem1_series: return None

    dem4 = last_value(dem4_series); dem1 = last_value(dem1_series)
    has_candle = candle_pattern_ok(k4)

    cls = classify_signal(dem4, dem1, has_candle)
    if not cls: return None

    sig_type, zone = cls
    last_ts_1d = k1[-1][0]
    key = build_dedup_key(symbol, sig_type, zone, last_ts_1d)
    if STATE["sent"].get(key): return None

    if tg_send_signal(symbol, sig_type, zone):
        STATE["sent"][key] = int(time.time())
        return symbol
    return None

def main_loop():
    symbols = get_symbols()
    if not symbols: symbols = ["BTC-USDT"]

    logging.info(f"INFO: Symbols loaded: {len(symbols)}")
    logging.info(f"INFO: Loaded {len(symbols)} symbols for scan.")
    logging.info(f"INFO: First symbol checked: {symbols[0]}")

    if SELFTEST_PING:
        tg_send_raw("🟢↑⚡🕯️")

    while True:
        sent_any = False
        processed = 0
        for sym in symbols:
            try:
                processed += 1
                if process_symbol(sym):
                    sent_any = True
            except Exception:
                pass
        if sent_any:
            save_state(STATE_PATH, STATE)
        if DEBUG_SCAN:
            dprint(f"SCAN: processed={processed} sent={'1+' if sent_any else '0'}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    while True:
        try:
            main_loop()
        except Exception:
            time.sleep(2)