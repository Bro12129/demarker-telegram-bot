# bot.py — Quiet mode (only 3 startup lines), BingX PERP scanner
# Signals: DeMarker-28 (4H & 1D), Wick≥25%, Engulfing after ≥2 opposite candles.
# Alerts: only symbol text. Types: LIGHT / L+CAN / 1TF+CAN (dedup in state).

import os, time, json, logging, requests
from typing import List, Dict, Tuple, Any

# ===================== ENV =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN",  "28"))
DEM_OB         = float(os.getenv("DEM_OB", "0.70"))
DEM_OS         = float(os.getenv("DEM_OS", "0.30"))

# State (дедуп между рестартами)
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# BingX REST (PERP)
BINGX_BASE     = os.getenv("BINGX_BASE", "https://open-api.bingx.com").rstrip("/")
KLINE_EP       = "/openApi/swap/v3/quote/klines"      # symbol, klineType {'4h','1d',...}, limit
CONTRACTS_EP   = "/openApi/swap/v2/quote/contracts"   # список PERP контрактов

# Таймфреймы
KLINE_4H       = os.getenv("KLINE_4H", "4h")
KLINE_1D       = os.getenv("KLINE_1D", "1d")  # важно: нижний регистр

# Тихое логирование: только WARNING/ERROR
logging.basicConfig(level=logging.WARNING)

# Резервный статический список (фолбэк)
DEFAULT_TICKERS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","BNB-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "XAU-USDT","XAG-USDT",
    "US100-USDT","US500-USDT","US30-USDT","US2000-USDT","VIX-USDT",
    "EUR-USD","GBP-USD","AUD-USD","NZD-USD","USD-JPY","USD-CHF","USD-CAD","USD-CNH","USD-HKD","USD-TRY","USD-MXN","USD-ZAR",
    "AAPL-USDT","TSLA-USDT","NVDA-USDT","AMZN-USDT","MSFT-USDT","META-USDT","COIN-USDT",
    "GOOG-USDT","NFLX-USDT","AMD-USDT","INTC-USDT","SNOW-USDT","SHOP-USDT","BABA-USDT"
]

# ===================== UTILS =====================
def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(path: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass  # тихий режим

def tg_send_symbol(sym: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(TG_API, json={"chat_id": CHAT_ID, "text": sym}, timeout=10)
    except Exception:
        pass  # тихий режим

def http_get(path: str, params: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    url = f"{BINGX_BASE}{path}"
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ===================== SYMBOL MAP (safety) =====================
def to_bingx(sym: str) -> str:
    """Подстраховка: конвертирует старые форматы в BingX-вид."""
    s = str(sym).strip().upper()
    map_special = {
        "SPXUSDT": "US500-USDT", "NAS100USDT": "US100-USDT", "US30USDT": "US30-USDT",
        "US2000USDT": "US2000-USDT", "VIXUSDT": "VIX-USDT", "XAUUSDT": "XAU-USDT", "XAGUSDT": "XAG-USDT",
    }
    if s in map_special: return map_special[s]
    fx = {"EURUSD","GBPUSD","AUDUSD","NZDUSD","USDJPY","USDCHF","USDCAD","USDCNH","USDHKD","USDTRY","USDMXN","USDZAR"}
    if s in fx: return s[:3] + "-" + s[3:]  # EUR-USD
    if s.endswith("USDT") and "-" not in s: return s[:-4] + "-USDT"
    return s

# ===================== TICKERS SOURCE (auto from BingX) =====================
_STOCK_HINTS = {"AAPL-","AMZN-","MSFT-","NVDA-","META-","TSLA-","GOOG-","GOOGL-","NFLX-","AMD-","INTC-","SNOW-","SHOP-","BABA-","COIN-"}

def _parse_contract_symbol(raw: Any) -> str:
    return str(raw).strip().upper()

def load_tickers() -> List[str]:
    """Берём ВСЕ PERP-контракты и фильтруем: индексы + форекс + xStock + металлы + вся крипта."""
    try:
        data = http_get(CONTRACTS_EP, params={})
    except Exception:
        return DEFAULT_TICKERS[:]

    rows = None
    for key in ("data", "result", "contracts", "symbols"):
        v = data.get(key)
        if isinstance(v, list):
            rows = v
            break
    if rows is None and isinstance(data, list):
        rows = data
    if not rows:
        return DEFAULT_TICKERS[:]

    want: List[str] = []
    for it in rows:
        sym = _parse_contract_symbol(it.get("symbol") or it.get("contractSymbol") or it.get("name") or "")
        if not sym:
            continue
        cat = str(it.get("category") or it.get("contractType") or "").lower()

        is_index = ("index" in cat) or any(k in sym for k in ["US100-","US500-","US30-","US2000-","VIX-"])
        is_fx    = ("forex" in cat) or (sym.count("-") == 1 and all(part.isalpha() for part in sym.split("-")) and len(sym) in (7,8))
        is_metal = sym in {"XAU-USDT","XAG-USDT"}
        is_stock = ("xstock" in cat) or ("stock" in cat) or any(h in sym for h in _STOCK_HINTS)
        is_crypto = (sym.endswith("-USDT") and not (is_index or is_fx or is_stock))

        if is_index or is_fx or is_stock or is_metal or is_crypto:
            want.append(sym)

    want = sorted(set(want))
    return want if want else DEFAULT_TICKERS[:]

# ===================== MARKET =====================
def get_klines(symbol: str, kline_type: str, limit: int = 500) -> List[Dict[str, Any]]:
    """/openApi/swap/v3/quote/klines → [{open,high,low,close,start}, ...]"""
    symbol = to_bingx(symbol)
    params = {"symbol": symbol, "klineType": kline_type, "limit": str(limit)}
    try:
        data = http_get(KLINE_EP, params=params)
        rows = None
        for key in ("data", "result", "klines", "candles"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
            if isinstance(v, dict) and isinstance(v.get("klines"), list):
                rows = v.get("klines")
                break
        if rows is None and isinstance(data, list):
            rows = data

        bars: List[Dict[str, Any]] = []
        for it in rows or []:
            if isinstance(it, list) and len(it) >= 5:
                t = int(it[0]); o = float(it[1]); h = float(it[2]); l = float(it[3]); c = float(it[4])
            elif isinstance(it, dict):
                t = int(it.get("openTime") or it.get("time") or it.get("startTime"))
                o = float(it.get("open")); h = float(it.get("high")); l = float(it.get("low")); c = float(it.get("close"))
            else:
                continue
            bars.append({"open": o, "high": h, "low": l, "close": c, "start": t})
        bars.sort(key=lambda x: x["start"])
        return bars
    except Exception:
        return []

# ===================== INDICATORS & PATTERNS =====================
def demarker_last(highs: List[float], lows: List[float], length: int = 28) -> float | None:
    if len(highs) < length + 2 or len(lows) < length + 2:
        return None
    dem_max, dem_min = [], []
    for i in range(1, len(highs)):
        dem_max.append(max(highs[i] - highs[i-1], 0.0))
    for i in range(1, len(lows)):
        dem_min.append(max(lows[i-1] - lows[i], 0.0))
    m = sum(dem_max[-length:]) / length
    n = sum(dem_min[-length:]) / length
    denom = m + n
    return 0.5 if denom <= 0 else (m / denom)

def wick25_flags(o: float, h: float, l: float, c: float) -> Tuple[bool, bool]:
    rng = max(h - l, 0.0)
    if rng <= 0:
        return (False, False)
    upper_w = h - max(o, c)
    lower_w = min(o, c) - l
    return ((upper_w / rng) >= 0.25, (lower_w / rng) >= 0.25)

def _color(o: float, c: float) -> int:
    return 1 if c > o else (-1 if c < o else 0)

def _engulf(o1: float, c1: float, o2: float, c2: float, bullish: bool) -> bool:
    # Сравнение ТОЛЬКО по телам свечей (open/close)
    if bullish:
        return (c1 > o1) and (c2 < o2) and (o1 <= c2) and (c1 >= o2)
    else:
        return (c1 < o1) and (c2 > o2) and (o1 >= c2) and (c1 <= o2)

def _consecutive_prior_same_color(kl: List[Dict[str, Any]], target: int, start_index: int = -2) -> int:
    cnt, i, n = 0, start_index, len(kl)
    while abs(i) <= n:
        b = kl[i]
        col = _color(float(b["open"]), float(b["close"]))
        if col == target and col != 0:
            cnt += 1
            i -= 1
        else:
            break
    return cnt

def engulfing_after_two_or_more(kl: List[Dict[str, Any]]) -> Tuple[bool, bool]:
    if len(kl) < 4:
        return (False, False)
    o1, c1 = float(kl[-1]["open"]),  float(kl[-1]["close"])
    o2, c2 = float(kl[-2]["open"]),  float(kl[-2]["close"])
    bull_ok = _engulf(o1, c1, o2, c2, bullish=True)  and (_consecutive_prior_same_color(kl, target=-1, start_index=-2) >= 2)
    bear_ok = _engulf(o1, c1, o2, c2, bullish=False) and (_consecutive_prior_same_color(kl, target= 1, start_index=-2) >= 2)
    return (bull_ok, bear_ok)

def zone_flags(d4h: float | None, d1d: float | None, ob: float, os: float) -> Tuple[bool, bool, bool, bool]:
    both_ob = (d4h is not None and d1d is not None) and (d4h >= ob and d1d >= ob)
    both_os = (d4h is not None and d1d is not None) and (d4h <= os and d1d <= os)
    one_ob  = (d4h is not None and d1d is not None) and ((d4h >= ob) ^ (d1d >= ob))
    one_os  = (d4h is not None and d1d is not None) and ((d4h <= os) ^ (d1d <= os))
    return both_ob, both_os, one_ob, one_os

# ===================== CORE PER SYMBOL =====================
def process_symbol(symbol: str, state: Dict[str, Any]) -> None:
    k4h = get_klines(symbol, KLINE_4H, limit=DEM_LEN + 50)
    k1d = get_klines(symbol, KLINE_1D, limit=DEM_LEN + 50)
    if len(k4h) < DEM_LEN + 2 or len(k1d) < DEM_LEN + 2:
        return  # тихо пропускаем

    def highs(kl): return [b["high"] for b in kl]
    def lows(kl):  return [b["low"]  for b in kl]

    dem4h = demarker_last(highs(k4h), lows(k4h), DEM_LEN)
    dem1d = demarker_last(highs(k1d), lows(k1d), DEM_LEN)
    bothOB, bothOS, oneOB, oneOS = zone_flags(dem4h, dem1d, DEM_OB, DEM_OS)

    o4,h4,l4,c4 = k4h[-1]["open"], k4h[-1]["high"], k4h[-1]["low"], k4h[-1]["close"]
    o1,h1,l1,c1 = k1d[-1]["open"], k1d[-1]["high"], k1d[-1]["low"], k1d[-1]["close"]
    u4,lw4 = wick25_flags(o4,h4,l4,c4)
    u1,lw1 = wick25_flags(o1,h1,l1,c1)
    wick_any = (u4 or lw4 or u1 or lw1)

    bull_eng_4h, bear_eng_4h = engulfing_after_two_or_more(k4h)
    bull_eng_1d, bear_eng_1d = engulfing_after_two_or_more(k1d)
    engulf_any = (bull_eng_4h or bear_eng_4h or bull_eng_1d or bear_eng_1d)

    candle_pattern = (wick_any or engulf_any)

    signal_light               = (bothOB or bothOS) and (not candle_pattern)
    signal_light_plus_candle   = (bothOB or bothOS) and candle_pattern
    signal_one_tf_plus_candle  = ((oneOB or oneOS) and candle_pattern)

    def maybe_send(sym: str, key: str):
        if state.get(sym) == key:
            return
        tg_send_symbol(sym)  # ТОЛЬКО символ
        state[sym] = key

    if signal_light_plus_candle:
        maybe_send(symbol, "L+CAN")
    elif signal_light:
        maybe_send(symbol, "LIGHT")
    elif signal_one_tf_plus_candle:
        maybe_send(symbol, "1TF+CAN")

# ===================== STARTUP ANNOUNCE (only once) =====================
def announce_start(tickers: List[str]) -> None:
    # Ровно три строки, с мгновенным выводом:
    print(f"INFO: Symbols loaded: {len(tickers)}", flush=True)
    if tickers:
        print(f"INFO: Loaded {len(tickers)} symbols for scan.", flush=True)
        print(f"INFO: First symbol checked: {tickers[0]}", flush=True)

# ===================== MAIN LOOP =====================
def main():
    tickers = load_tickers()
    announce_start(tickers)  # единственные строки, которые увидишь в логах

    if not tickers:
        return

    state = load_state(STATE_PATH)

    while True:
        t0 = time.time()
        try:
            for sym in tickers:
                process_symbol(sym, state)
            save_state(STATE_PATH, state)
        except Exception:
            pass  # тихо
        dt = time.time() - t0
        time.sleep(max(1, POLL_SECONDS - int(dt)))

if __name__ == "__main__":
    main()