# bot.py
import os, time, json, logging, requests, re
from typing import List, Dict, Optional

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

# публикация в приватную группу
GROUP_CHAT_ID  = "-1002963303214"

DEBUG_TG       = os.getenv("DEBUG_TG", "0") == "1"
DEBUG_SCAN     = os.getenv("DEBUG_SCAN", "0") == "1"
SELFTEST_PING  = os.getenv("SELFTEST_PING", "0") == "1"

# формат ключей (для сброса истории при необходимости)
FORMAT_VER     = os.getenv("FORMAT_VER", "v11")

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

# ============ STATIC SEED (расширенный резерв) ============
STATIC_SYMBOLS: List[str] = [
    # === CRYPTO Majors / L1/L2 / DeFi (популярные PERP) ===
    "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TON-USDT",
    "LTC-USDT","TRX-USDT","LINK-USDT","DOT-USDT","AVAX-USDT","MATIC-USDT","OP-USDT","ARB-USDT",
    "APT-USDT","ATOM-USDT","SUI-USDT","NEAR-USDT","ETC-USDT","BCH-USDT","ICP-USDT","FIL-USDT",
    "AAVE-USDT","UNI-USDT","INJ-USDT","RUNE-USDT","XLM-USDT","ALGO-USDT","HBAR-USDT","FTM-USDT",
    "GRT-USDT","PEPE-USDT","WIF-USDT","SEI-USDT","SUI-USDT","TIA-USDT","JUP-USDT","PYTH-USDT",

    # === Privacy & Low-corr (если доступны на BingX) ===
    "XMR-USDT","ZEC-USDT","DASH-USDT",

    # === Stablecoin pairs (если есть perp) ===
    "USDC-USDT","BUSD-USDT","TUSD-USDT","FDUSD-USDT","DAI-USDT","PYUSD-USDT","USDT-USD",

    # === Metals ===
    "XAU-USDT","XAG-USDT","XPT-USDT","XPD-USDT",

    # === US & World Indices (токенизированные / индикативные фьючи) ===
    "US100","US500","US30","US2000","VIX",
    "DE40","DE30","UK100","JP225","FR40","HK50","ES35","IT40","AU200","CN50",

    # === FOREX Majors & Minors ===
    "EUR-USD","GBP-USD","USD-JPY","AUD-USD","NZD-USD","USD-CAD","USD-CHF",
    "EUR-JPY","GBP-JPY","EUR-GBP","AUD-JPY","CHF-JPY","CAD-JPY","NZD-JPY",
    "USD-MXN","USD-TRY","USD-CNH","USD-HKD","USD-SGD","USD-ZAR",

    # === Tokenized Stocks (xStock), если доступны ===
    "AAPL-USDT","MSFT-USDT","NVDA-USDT","GOOGL-USDT","AMZN-USDT","META-USDT","TSLA-USDT",
    "BRK.B-USDT","JPM-USDT","V-USDT","MA-USDT","UNH-USDT","JNJ-USDT","PG-USDT","HD-USDT",
    "NFLX-USDT","ADBE-USDT","AMD-USDT","CRM-USDT","AVGO-USDT","KO-USDT","PEP-USDT"
]

# ============ SYMBOL NORMALIZATION ============
def symbol_variants(sym: str) -> List[str]:
    """Пробуем несколько алиасов для запроса klines."""
    v: List[str] = []
    s = sym.upper().replace("_","-")
    v.append(s)

    # plain TICKER -> TICKER-USDT
    if "-" not in s and not s.endswith("-USDT"):
        v.append(f"{s}-USDT")

    # XXXUSDT -> XXX-USDT
    m = re.fullmatch(r"([A-Z0-9]{3,})USDT", s)
    if m:
        v.append(f"{m.group(1)}-USDT")

    # Индексы часто есть без -USDT
    if s in {"US100","US500","US30","US2000","VIX","DE40","DE30","UK100","JP225","FR40","HK50","ES35","IT40","AU200","CN50"}:
        v.append(f"{s}-USDT")

    # Металлы (спот алиасы)
    if s in {"XAU","XAUUSD","XAU-USDT"}:
        v += ["XAUUSD","XAU-USDT"]
    if s in {"XAG","XAGUSD","XAG-USDT"}:
        v += ["XAGUSD","XAG-USDT"]

    # Уникализация
    seen=set(); out=[]
    for x in v:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def normalize_from_contract(sym: str) -> str:
    """Нормализуем символ из /contracts под привычные запросы klines."""
    return sym.upper().replace("_","-")

# ============ KLINES PARSER ============
def _pick_num(d: Dict, *keys: str) -> Optional[float]:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except Exception:
                continue
    return None

def _parse_klines_payload(raw) -> Optional[List[List[float]]]:
    out: List[List[float]] = []
    for k in raw:
        if isinstance(k, dict):
            try:
                t = int(k.get("openTime") or k.get("time") or k.get("t"))
                o = _pick_num(k, "open","o","openPrice"); h = _pick_num(k, "high","h","highPrice")
                l = _pick_num(k, "low","l","lowPrice");  c = _pick_num(k, "close","c","closePrice")
                if None in (o,h,l,c): continue
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

def fetch_klines_once(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    url = f"{BINGX_BASE}/openApi/swap/v3/quote/klines"
    data = http_get(url, params={"symbol": symbol, "interval": interval, "limit": str(limit)})
    if not data:
        return None
    raw = data.get("data") or data.get("klines") or []
    return _parse_klines_payload(raw)

def fetch_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    for alias in symbol_variants(symbol):
        out = fetch_klines_once(alias, interval, limit)
        if out:
            return out
    return None

# ============ SYMBOLS DISCOVERY ============
def fetch_contracts_dynamic() -> List[str]:
    """Тянем все PERP из BingX и нормализуем."""
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/contracts"
    data = http_get(url, params={}) or {}
    items = data.get("data") or data.get("symbolList") or []
    out: List[str] = []
    for it in items:
        sym = (it.get("symbol") or it.get("contractId") or "").upper()
        if not sym:
            continue
        ctype = (it.get("contractType") or it.get("type") or "").upper()
        if "PERP" not in ctype:
            continue
        out.append(normalize_from_contract(sym))
    return sorted(set(out))

def validate_symbols(cands: List[str], sample_tf: str = "1d") -> List[str]:
    """Лёгкая валидация: у тикера должны быть хотя бы 2 бара на дневке."""
    valid: List[str] = []
    for s in cands:
        try:
            k = fetch_klines(s, sample_tf, limit=5)
            if k and len(k) >= 2:
                valid.append(s)
        except Exception:
            pass
        time.sleep(0.005)
    return valid

def get_symbols() -> List[str]:
    dyn = fetch_contracts_dynamic()
    universe = sorted(set(dyn) | set(STATIC_SYMBOLS))
    valid = validate_symbols(universe, sample_tf=KLINE_1D)
    if valid:
        STATE["universe"] = valid
        save_state(STATE_PATH, STATE)
        return valid
    cached = STATE.get("universe") or []
    return cached if cached else STATIC_SYMBOLS[:]

# ============ INDICATOR ============
def demarker_series(ohlc: List[List[float]], length: int) -> Optional[List[Optional[float]]]:
    if not ohlc or len(ohlc) < length + 2:
        return None
    highs = [x[2] for x in ohlc]; lows  = [x[3] for x in ohlc]
    up = [0.0]; dn = [0.0]
    for i in range(1, len(ohlc)):
        up.append(max(highs[i]-highs[i-1], 0.0))
        dn.append(max(lows[i-1]-lows[i], 0.0))
    def sma(arr, i, n):
        s = 0.0
        for k in range(i-n+1, i+1): s += arr[k]
        return s / n
    dem: List[Optional[float]] = [None]*len(ohlc)
    for i in range(length, len(ohlc)):
        up_s = sma(up, i, length); dn_s = sma(dn, i, length)
        denom = up_s + dn_s
        dem[i] = (up_s/denom) if denom != 0 else 0.5
    return dem

# ======= CLOSED-BAR HELPERS =======
def last_closed_value(series: List[Optional[float]]) -> Optional[float]:
    if not series or len(series) < 2: return None
    i = len(series) - 2
    while i >= 0 and series[i] is None: i -= 1
    return series[i] if i >= 0 else None

def last_closed_ts(ohlc: List[List[float]]) -> Optional[int]:
    if not ohlc or len(ohlc) < 2: return None
    return int(ohlc[-2][0])

# ============ ZONES (СТРОГО, без округлений) ============
def zone_of_closed(v: Optional[float]) -> Optional[str]:
    if v is None: return None
    if v >= DEM_OB: return "OB"
    if v <= DEM_OS: return "OS"
    return None

# ============ CANDLE PATTERNS (строго по закрытой свече -2) ============
def wick_ge_body_pct(ohlc: List[List[float]], idx: int, pct: float = 0.25) -> bool:
    """Pin-bar: любой фитиль >= pct * |тело| на закрытой свече."""
    if not ohlc or not (-len(ohlc) <= idx < len(ohlc)):
        return False
    o,h,l,c = ohlc[idx][1], ohlc[idx][2], ohlc[idx][3], ohlc[idx][4]
    body = abs(c - o)
    if body <= 1e-12:
        return False
    upper = h - max(o, c)
    lower = min(o, c) - l
    thr = pct * body
    return (upper >= thr) or (lower >= thr)

def engulfing_with_prior_opposition_at(ohlc: List[List[float]], base_idx: int) -> bool:
    """Engulfing на закрытой свече -2 + ≥2 свечи противоположного цвета до неё."""
    need = (-base_idx) + 3
    if len(ohlc) < need or not (-len(ohlc) <= base_idx-3 < len(ohlc)):
        return False
    o0,h0,l0,c0 = ohlc[base_idx][1], ohlc[base_idx][2], ohlc[base_idx][3], ohlc[base_idx][4]
    o1,h1,l1,c1 = ohlc[base_idx-1][1], ohlc[base_idx-1][2], ohlc[base_idx-1][3], ohlc[base_idx-1][4]
    o2,c2 = ohlc[base_idx-2][1], ohlc[base_idx-2][4]
    o3,c3 = ohlc[base_idx-3][1], ohlc[base_idx-3][4]
    bull0 = c0 >= o0
    bull2 = c2 >= o2
    bull3 = c3 >= o3
    if bull0:
        if not ((not bull2) and (not bull3)): return False
        return (min(o0,c0) <= min(o1,c1)) and (max(o0,c0) >= max(o1,c1))
    else:
        if not (bull2 and bull3): return False
        return (min(o0,c0) <= min(o1,c1)) and (max(o0,c0) >= max(o1,c1))

def candle_pattern_on_closed(ohlc: List[List[float]]) -> bool:
    """Проверяем паттерн только на закрытой свече (-2)."""
    if not ohlc or len(ohlc) < 4:  # нужно достаточно истории для engulfing
        return False
    base_idx = -2
    return wick_ge_body_pct(ohlc, base_idx, 0.25) or engulfing_with_prior_opposition_at(ohlc, base_idx)

# ============ SIGNAL UTILS ============
def format_signal_text(symbol: str, signal_type: str, zone: Optional[str]) -> str:
    arrow = "🟢↑" if zone == "OS" else ("🔴↓" if zone == "OB" else "")
    status = "⚡" if signal_type == "LIGHT" else ("⚡🕯️" if signal_type == "L+CAN" else "🕯️")
    return f"{symbol} {arrow} {status}".strip()

def tg_send_raw(text: str) -> bool:
    if not TELEGRAM_TOKEN:
        dprint("TG: пустой токен."); return False
    url = f"{TG_API}/sendMessage"
    ok_any = False
    recipients = []
    if TELEGRAM_CHAT:
        recipients.append(TELEGRAM_CHAT)
    recipients.append(GROUP_CHAT_ID)
    for chat_id in recipients:
        form = {"chat_id": chat_id, "text": text, "disable_notification": True}
        jsn  = {"chat_id": chat_id, "text": text, "disable_notification": True}
        delivered = False
        r = http_post(url, data=form)
        if r is not None:
            try: delivered = (r.status_code == 200) and (r.json().get("ok") is True)
            except Exception: delivered = False
        if not delivered:
            r = http_post(url, json_body=jsn)
            if r is not None:
                try: delivered = (r.status_code == 200) and (r.json().get("ok") is True)
                except Exception: delivered = False
        ok_any = ok_any or delivered
        time.sleep(0.05)
    return ok_any

def tg_send_signal(symbol: str, signal_type: str, zone: Optional[str]) -> bool:
    return tg_send_raw(format_signal_text(symbol, signal_type, zone))

# ===== DEDUP KEYS: один сигнал каждого типа на пару закрытых баров =====
def build_dedup_key(symbol: str, signal_type: str, zone: Optional[str],
                    ts_1d: int, ts_4h: int, pat_tf: str = "-") -> str:
    return f"{FORMAT_VER}|{symbol}|{signal_type}|{zone or '-'}|{ts_1d}|{ts_4h}|{pat_tf}"

# ============ CORE ============
def process_symbol(symbol: str) -> Optional[str]:
    k4 = fetch_klines(symbol, KLINE_4H, limit=max(200, DEM_LEN + 10))
    k1 = fetch_klines(symbol, KLINE_1D, limit=max(200, DEM_LEN + 10))
    if not k4 or not k1:
        return None

    dem4_series = demarker_series(k4, DEM_LEN)
    dem1_series = demarker_series(k1, DEM_LEN)
    if not dem4_series or not dem1_series:
        return None

    dem4 = last_closed_value(dem4_series)
    dem1 = last_closed_value(dem1_series)
    z4 = zone_of_closed(dem4)
    z1 = zone_of_closed(dem1)

    ts4 = last_closed_ts(k4)
    ts1 = last_closed_ts(k1)
    if ts4 is None or ts1 is None:
        return None

    # DEBUG строка (без форматных ошибок)
    if DEBUG_SCAN:
        s_dem1 = "nan" if dem1 is None else f"{dem1:.4f}"
        s_dem4 = "nan" if dem4 is None else f"{dem4:.4f}"
        dprint(f"{symbol} dem1={s_dem1} z1={z1} dem4={s_dem4} z4={z4} ts1={ts1} ts4={ts4}")

    # === МОЛНИЯ: обе закрытые свечи в одной зоне (OB или OS) ===
    if (z4 is not None) and (z1 is not None) and (z4 == z1):
        has_can_4 = candle_pattern_on_closed(k4)
        has_can_1 = candle_pattern_on_closed(k1)
        if has_can_4 or has_can_1:
            sig_type = "L+CAN"; zone_for_msg = z4; pat_tf = "4H" if has_can_4 else "1D"
        else:
            sig_type = "LIGHT"; zone_for_msg = z4; pat_tf = "-"
        key = build_dedup_key(symbol, sig_type, zone_for_msg, ts1, ts4, pat_tf)
        if not STATE["sent"].get(key):
            if tg_send_signal(symbol, sig_type, zone_for_msg):
                STATE["sent"][key] = int(time.time())
                return symbol
        return None

    # === 1TF+CAN: ровно один TF в зоне и на нём есть паттерн (без молнии) ===
    if (z4 is not None) ^ (z1 is not None):
        has_can_4 = candle_pattern_on_closed(k4) if z4 is not None else False
        has_can_1 = candle_pattern_on_closed(k1) if z1 is not None else False
        if z4 is not None and has_can_4:
            sig_type = "1TF+CAN"; zone_for_msg = z4; pat_tf = "4H"
        elif z1 is not None and has_can_1:
            sig_type = "1TF+CAN"; zone_for_msg = z1; pat_tf = "1D"
        else:
            return None
        key = build_dedup_key(symbol, sig_type, zone_for_msg, ts1, ts4, pat_tf)
        if not STATE["sent"].get(key):
            if tg_send_signal(symbol, sig_type, zone_for_msg):
                STATE["sent"][key] = int(time.time())
                return symbol
        return None

    return None

def main_loop():
    symbols = get_symbols()
    if not symbols:
        symbols = ["BTC-USDT"]

    # Тихий старт — три строки
    logging.info(f"INFO: Symbols loaded: {len(symbols)}")
    logging.info(f"INFO: Loaded {len(symbols)} symbols for scan.")
    logging.info(f"INFO: First symbol checked: {symbols[0]}")

    if SELFTEST_PING:
        tg_send_raw("🟢↑⚡🕯️")  # разовый тест-формат

    while True:
        sent_any = False
        processed = 0
        for sym in symbols:
            try:
                processed += 1
                if process_symbol(sym):
                    sent_any = True
            except Exception as e:
                if DEBUG_SCAN:
                    dprint(f"ERR {sym}: {e}")
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