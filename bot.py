# bot.py — BingX only, PERP-only universe (no spot). Auto-picks crypto majors, US indices PERP,
# tokenized stocks PERP (AAPLX, NVDAX, etc. if available), FX-PERP, metals. Signals per your spec.

import os, time, json, logging, requests, re
from typing import List, Dict, Tuple, Any, Set

# ===================== ENV =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN",  "28"))
DEM_OB         = float(os.getenv("DEM_OB", "0.70"))
DEM_OS         = float(os.getenv("DEM_OS", "0.30"))

# State (дедуп сигналов между рестартами)
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# BingX REST (PERP)
BINGX_BASE     = os.getenv("BINGX_BASE", "https://open-api.bingx.com")
KLINE_EP       = "/openApi/swap/v3/quote/klines"      # symbol, klineType in {1min..,4h,1D,...}, limit, startTime/endTime
CONTRACTS_EP   = "/openApi/swap/v2/quote/contracts"   # все деривативные контракты

# --------- Управление вселенной тикеров (ТОЛЬКО PERP) ----------
# Жёсткая фиксация состава (перечень символов PERP через запятую) — опционально:
TICKERS_CSV_PERP = os.getenv("TICKERS_CSV_PERP", "").strip()

# Принудительное ДОБАВЛЕНИЕ (если свежие листинги/редкие тикеры)
FORCE_INCLUDE_CSV = os.getenv("FORCE_INCLUDE_CSV", "").strip()   # напр. "SPXUSDT,AAPLXUSDT"

# Принудительное ИСКЛЮЧЕНИЕ (по подстрокам; по умолчанию убираем все '1000' контракты)
FORCE_EXCLUDE_CSV = os.getenv("FORCE_EXCLUDE_CSV", "1000").strip()

# Базовый набор ~30 крипто-мейджоров (USDT-PERP). Можно допилить под себя.
CRYPTO_MAJORS_30 = [s.strip().upper() for s in """
BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ADAUSDT,DOGEUSDT,TRXUSDT,
LINKUSDT,MATICUSDT,DOTUSDT,AVAXUSDT,ATOMUSDT,LTCUSDT,BCHUSDT,NEARUSDT,
APTUSDT,ARBUSDT,OPUSDT,SUIUSDT,SEIUSDT,INJUSDT,FILUSDT,RNDRUSDT,
TONUSDT,UNIUSDT,AAVEUSDT,ETCUSDT,FTMUSDT,THETAUSDT
""".replace("\n","").split(",") if s.strip()]

# Металлы PERP (у BingX есть золото/серебро и т. п. в разных видах; тянем USDT-маржин. формы)
METALS_WHITELIST = [s.strip().upper() for s in os.getenv(
    "METALS_WHITELIST",
    "XAUUSDT,XAGUSDT"   # золото/серебро USDT-маржин PERP на BingX
).split(",") if s.strip()]

# Индексы США (S&P/NASDAQ/DOW/Russell/VIX) — типичные нейминги у BingX: SPXUSDT, NAS100USDT, US30USDT, US2000USDT, VIXUSDT
INDEX_REGEX = os.getenv(
    "INDEX_REGEX",
    r"^(SPXUSDT|SP500USDT|US500USDT|ESUSDT|NAS100USDT|NDXUSDT|US100USDT|NQUSDT|DOWUSDT|DJIUSDT|US30USDT|YMUSDT|RUTUSDT|US2000USDT|RTYUSDT|VIXUSDT)$"
)

# FX-PERP у BingX: EURUSD, GBPUSD, AUDUSD, NZDUSD, USDJPY, USDCHF, USDCAD и т. п. (м.б. без суффикса USDT)
FX_REGEX = os.getenv(
    "FX_REGEX",
    r"^(EURUSD|GBPUSD|AUDUSD|NZDUSD|USDJPY|USDCHF|USDCAD|USDCNH|USDHKD|USDTRY|USDMXN|USDZAR|USDNOK|USDSGD|USDDKK|USDSEK|USDRUB|XAUUSD|XAGUSD)$"
)

# Токенизированные акции/активы в PERP: AAPLXUSDT, NVDAXUSDT, COINXUSDT, AMZNXUSDT, METAXUSDT и т.п.
XSTOCK_PERP_REGEX = os.getenv(
    "XSTOCK_PERP_REGEX",
    r"^[A-Z0-9]+XUSDT$"
)

# Интервалы для свече-анализа
KLINE_4H = os.getenv("KLINE_4H", "4h")
KLINE_1D = os.getenv("KLINE_1D", "1D")

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

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
    except Exception as e:
        logging.warning(f"save_state error: {e}")

def tg_send_symbol(sym: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram ENV not set; skip send.")
        return
    try:
        requests.post(TG_API, json={"chat_id": CHAT_ID, "text": sym}, timeout=10)
    except Exception as e:
        logging.warning(f"Telegram send error for {sym}: {e}")

def http_get(path: str, params: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    url = f"{BINGX_BASE}{path}"
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ===================== BINGX MARKET =====================
def fetch_all_perp_symbols() -> Set[str]:
    """
    Тянем все деривативные контракты BingX (swap), возвращаем множество символов, которые:
    - активны,
    - являются perpetual (без экспирации),
    - маржин. линейные (USDT/USDC/и т.п.).
    """
    symbols: Set[str] = set()
    try:
        data = http_get(CONTRACTS_EP, params={})
        # формат Zendesk: /openApi/swap/v2/quote/contracts — базовая таблица деривативов
        # Ожидаем в result список с полями symbol, status, contractType и т.п.
        result = data.get("data") or data.get("result") or data.get("res") or data
        lst = result.get("contracts") if isinstance(result, dict) else None
        if lst is None and isinstance(result, list):
            lst = result

        if not lst:
            # некоторые окружения отдают сразу list в data
            lst = data.get("data") if isinstance(data.get("data"), list) else []

        for it in (lst or []):
            sym  = str(it.get("symbol","")).upper()
            stat = str(it.get("status","") or it.get("state","")).lower()  # trading / online ...
            ctyp = (it.get("contractType") or it.get("type") or "").lower()  # LinearPerpetual / Perpetual ...
            if not sym:
                continue
            if stat and stat not in ("trading","online","tradable","on"):
                continue
            if "perpetual" not in ctyp and "swap" not in ctyp:
                # пропускаем dated futures / нестандарт
                continue
            symbols.add(sym)
    except Exception as e:
        logging.warning(f"contracts fetch error: {e}")
    return symbols

def apply_force_filters(symbols: List[str]) -> List[str]:
    excl = [s.strip().upper() for s in FORCE_EXCLUDE_CSV.split(",") if s.strip()]
    if excl:
        symbols = [s for s in symbols if all(ex not in s for ex in excl)]
    incl = [s.strip().upper() for s in FORCE_INCLUDE_CSV.split(",") if s.strip()]
    for s in incl:
        if s not in symbols:
            symbols.append(s)
    return symbols

def resolve_perp_universe() -> List[str]:
    """
    Конечная вселенная ТОЛЬКО PERP (без spot):
      1) Жёсткий CSV → используем как есть (плюс force include/exclude).
      2) Иначе: пересечение
         • ~30 крипто-мейджоров,
         • металлы (XAUUSDT, XAGUSDT),
         • индексы США PERP (по INDEX_REGEX),
         • FX-PERP (по FX_REGEX),
         • токенизированные акции PERP (по XSTOCK_PERP_REGEX)
       с реальным списком контрактов BingX.
    """
    if TICKERS_CSV_PERP:
        base = [s.strip().upper() for s in TICKERS_CSV_PERP.split(",") if s.strip()]
        return apply_force_filters(list(dict.fromkeys(base)))

    have = fetch_all_perp_symbols()
    if not have:
        # fallback — хотя бы крипто-мейджоры + металлы (часто есть везде)
        logging.warning("Could not fetch BingX PERP universe; fallback to majors+metals only.")
        base = list(dict.fromkeys(CRYPTO_MAJORS_30 + METALS_WHITELIST))
        return apply_force_filters(base)

    idx_re    = re.compile(INDEX_REGEX, re.IGNORECASE)
    fx_re     = re.compile(FX_REGEX, re.IGNORECASE)
    xstock_re = re.compile(XSTOCK_PERP_REGEX, re.IGNORECASE)

    majors_ok  = [s for s in CRYPTO_MAJORS_30 if s in have]
    metals_ok  = [s for s in METALS_WHITELIST if s in have]
    indices_ok = [s for s in have if idx_re.match(s)]
    fx_ok      = [s for s in have if fx_re.match(s)]
    xstock_ok  = [s for s in have if xstock_re.match(s)]

    base = list(dict.fromkeys(majors_ok + metals_ok + indices_ok + fx_ok + xstock_ok))
    if not base:
        logging.warning("Empty universe after filters; fallback to majors+metals.")
        base = list(dict.fromkeys([s for s in (CRYPTO_MAJORS_30 + METALS_WHITELIST) if s in have]))
    return apply_force_filters(base)

def get_klines(symbol: str, kline_type: str, limit: int = 500) -> List[Dict[str, Any]]:
    """
    BingX candles (PERP): /openApi/swap/v3/quote/klines
    klineType: '4h' / '1D' и др. Возвращаем последние ЗАКРЫТЫЕ свечи в хронологическом порядке.
    """
    params = {"symbol": symbol, "klineType": kline_type, "limit": str(limit)}
    try:
        data = http_get(KLINE_EP, params=params)
        # форматы различаются по окружениям; ищем массив свечей в data / result
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

        bars = []
        for it in rows or []:
            # ожидаемый формат: [openTime, open, high, low, close, volume, ...] либо dict
            if isinstance(it, list) and len(it) >= 6:
                o = float(it[1]); h = float(it[2]); l = float(it[3]); c = float(it[4]); t = int(it[0])
            elif isinstance(it, dict):
                t = int(it.get("openTime") or it.get("time") or it.get("startTime"))
                o = float(it.get("open")); h = float(it.get("high")); l = float(it.get("low")); c = float(it.get("close"))
            else:
                continue
            bars.append({"open": o, "high": h, "low": l, "close": c, "start": t})
        # Хронология: гарантируем от старой к новой
        bars.sort(key=lambda x: x["start"])
        return bars
    except Exception as e:
        logging.warning(f"Kline fetch error {symbol} {kline_type}: {e}")
        return []

# ===================== INDICATORS & PATTERNS =====================
def demarker_last(highs: List[float], lows: List[float], length: int = 28) -> float:
    if len(highs) < length + 2 or len(lows) < length + 2:
        return None
    dem_max, dem_min = [], []
    for i in range(1, len(highs)):
        dem_max.append(max(highs[i] - highs[i-1], 0.0))
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
    return 1 if c > o else (-1 if c < o else 0)  # 1=green, -1=red, 0=doji

def _engulf(curr_o: float, curr_c: float, prev_o: float, prev_c: float, bullish: bool) -> bool:
    if bullish:
        return (curr_c > curr_o) and (prev_c < prev_o) and (curr_o <= prev_c) and (curr_c >= prev_o)
    else:
        return (curr_c < curr_o) and (prev_c > prev_o) and (curr_o >= prev_c) and (curr_c <= prev_o)

def _consecutive_prior_same_color(klines: List[Dict[str, Any]], target_color: int, start_index: int = -2) -> int:
    cnt, i, n = 0, start_index, len(klines)
    while abs(i) <= n:
        b = klines[i]
        col = _color(float(b["open"]), float(b["close"]))
        if col == target_color and col != 0:
            cnt += 1
            i -= 1
        else:
            break
    return cnt

def engulfing_after_two_or_more(klines: List[Dict[str, Any]]) -> Tuple[bool, bool]:
    if len(klines) < 4:
        return (False, False)
    o1 = float(klines[-1]["open"]);  c1 = float(klines[-1]["close"])
    o2 = float(klines[-2]["open"]);  c2 = float(klines[-2]["close"])
    bull_ok = False
    if _engulf(o1, c1, o2, c2, bullish=True):
        bull_ok = (_consecutive_prior_same_color(klines, target_color=-1, start_index=-2) >= 2)
    bear_ok = False
    if _engulf(o1, c1, o2, c2, bullish=False):
        bear_ok = (_consecutive_prior_same_color(klines, target_color=1,  start_index=-2) >= 2)
    return (bull_ok, bear_ok)

def zone_flags(d4h: float, d1d: float, ob: float, os: float) -> Tuple[bool, bool, bool, bool]:
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
        logging.info(f"{symbol}: not enough data")
        return

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

    # ===== Правила сигналов =====
    signal_light               = (bothOB or bothOS) and (not candle_pattern)     # тип 1
    signal_light_plus_candle   = (bothOB or bothOS) and candle_pattern           # тип 2
    signal_one_tf_plus_candle  = ((oneOB or oneOS) and candle_pattern)           # тип 3

    def maybe_send(sym: str, key: str):
        if state.get(sym) == key:
            return
        tg_send_symbol(sym)  # ТОЛЬКО символ
        state[sym] = key

    # Приоритеты: L+CAN > LIGHT > 1TF+CAN
    if signal_light_plus_candle:
        maybe_send(symbol, "L+CAN")
    elif signal_light:
        maybe_send(symbol, "LIGHT")
    elif signal_one_tf_plus_candle:
        maybe_send(symbol, "1TF+CAN")

# ===================== MAIN LOOP =====================
def main():
    tickers = resolve_perp_universe()
    if not tickers:
        logging.error("No PERP symbols resolved on BingX. Adjust ENV or regex filters.")
        return

    state = load_state(STATE_PATH)
    logging.info(f"Symbols loaded: {len(tickers)}")
    logging.info(f"DEM_LEN={DEM_LEN} OB={DEM_OB} OS={DEM_OS} poll={POLL_SECONDS}s")

    while True:
        t0 = time.time()
        try:
            for sym in tickers:
                process_symbol(sym, state)
            save_state(STATE_PATH, state)
        except Exception as e:
            logging.error(f"loop error: {e}")
        dt = time.time() - t0
        time.sleep(max(1, POLL_SECONDS - int(dt)))

if __name__ == "__main__":
    main()