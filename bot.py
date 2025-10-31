import os, time, json, logging, requests
from typing import List, Dict, Tuple, Any

# ---------------------- ENV ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN",  "28"))
DEM_OB         = float(os.getenv("DEM_OB", "0.70"))
DEM_OS         = float(os.getenv("DEM_OS", "0.30"))

# Состояние (для дедупа сигналов между рестартами)
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# Bybit v5
BYBIT_URL      = os.getenv("BYBIT_URL", "https://api.bybit.com")
KLINE_ENDPOINT = "/v5/market/kline"  # category=linear&symbol=BTCUSDT&interval=240&limit=200

# Если понадобится — можно переопределить список тикеров через ENV TICKERS_CSV
# Иначе используем «основной набор» ~45 линейных перпов USDT/USDC + золото
DEFAULT_TICKERS = [
    # ——— Мейджоры
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT","ADAUSDT","DOGEUSDT","TRXUSDT",
    "LINKUSDT","MATICUSDT","DOTUSDT","AVAXUSDT","ATOMUSDT","LTCUSDT","BCHUSDT","NEARUSDT",
    "APTUSDT","ARBUSDT","OPUSDT","SUIUSDT","SEIUSDT","INJUSDT","FILUSDT","RNDRUSDT",
    "APTUSDC","ARBUSDC","OPUSDC",  # небольшая примесь USDC-линейных перпов (тоже category=linear)
    # ——— Мемы/высоколиквидные «тысячные» контракты
    "PEPEUSDT","1000PEPEUSDT","SHIBUSDT","1000SHIBUSDT",
    # ——— Топ-альты дополним
    "TONUSDT","AAVEUSDT","UNIUSDT","XLMUSDT","ETCUSDT","FTMUSDT","THETAUSDT","GRTUSDT",
    # ——— Токенизированные металлы (перпы)
    "XAUTUSDT","PAXGUSDT",
    # ——— Пара свежих листингов (пример — легко заменить под себя)
    "MNTUSDT","GRASSUSDT","HYPEUSDT"
]

TICKERS_CSV    = os.getenv("TICKERS_CSV", "")
if TICKERS_CSV.strip():
    TICKERS = [s.strip().upper() for s in TICKERS_CSV.split(",") if s.strip()]
else:
    TICKERS = DEFAULT_TICKERS

# ---------------------- LOGGING ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# ===================== УТИЛИТЫ =====================

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

# ----- Bybit Kline -----

def get_klines(symbol: str, interval: str, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Возвращает список закрытых баров (включая последний закрытый) в формате словарей:
    {open, high, low, close, start}
    interval: "240" для 4H, "D" для 1D
    category: linear (USDT/USDC перпы)
    """
    url = f"{BYBIT_URL}{KLINE_ENDPOINT}"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit)
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit retCode {data.get('retCode')}: {data.get('retMsg')}")
        lst = data.get("result", {}).get("list", []) or []
        # Формат элемента: [start, open, high, low, close, volume, turnover]
        bars = []
        for item in reversed(lst):  # Bybit отдает от свежих к старым — развернем в хронологию
            o = float(item[1]); h = float(item[2]); l = float(item[3]); c = float(item[4])
            t = int(item[0])
            bars.append({"open": o, "high": h, "low": l, "close": c, "start": t})
        return bars
    except Exception as e:
        logging.warning(f"Kline fetch error {symbol} {interval}: {e}")
        return []

# ----- DeMarker + свечные правила -----

def demarker_last(highs: List[float], lows: List[float], length: int = 28) -> float:
    """
    DeM = SMA(DeMMAX,len) / (SMA(DeMMAX,len) + SMA(DeMMIN,len))
    где DeMMAX_t = max(High_t - High_{t-1}, 0)
        DeMMIN_t = max(Low_{t-1} - Low_t, 0)
    Возвращает последнее значение по закрытым барам.
    """
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
    """(upperOK, lowerOK): фитиль ≥25% полного диапазона."""
    rng = max(h - l, 0.0)
    if rng <= 0: 
        return (False, False)
    upper_w = h - max(o, c)
    lower_w = min(o, c) - l
    return ((upper_w / rng) >= 0.25, (lower_w / rng) >= 0.25)

def _color(o: float, c: float) -> int:
    # 1 = зелёная, -1 = красная, 0 = doji
    return 1 if c > o else (-1 if c < o else 0)

def _engulf(curr_o: float, curr_c: float, prev_o: float, prev_c: float, bullish: bool) -> bool:
    """
    Классическое поглощение телом (строго по телам):
    bullish: зелёная свеча поглощает предыдущую красную;
    bearish: красная свеча поглощает предыдущую зелёную.
    """
    if bullish:
        return (curr_c > curr_o) and (prev_c < prev_o) and (curr_o <= prev_c) and (curr_c >= prev_o)
    else:
        return (curr_c < curr_o) and (prev_c > prev_o) and (curr_o >= prev_c) and (curr_c <= prev_o)

def _consecutive_prior_same_color(klines: List[Dict[str, Any]], target_color: int, start_index: int = -2) -> int:
    """
    Считает подряд идущие свечи цвета target_color, шагая назад
    начиная с бара start_index (обычно -2, т.е. свеча перед текущей).
    """
    cnt = 0
    i = start_index
    n = len(klines)
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
    """
    Возвращает (bullish_engulf_ok, bearish_engulf_ok) на последней ЗАКРЫТОЙ свече [-1],
    НО только если перед поглощением была серия из ≥2 свечей одного цвета
    ПРОТИВОПОЛОЖНОГО направления.
    """
    if len(klines) < 4:
        return (False, False)

    o1 = float(klines[-1]["open"]);  c1 = float(klines[-1]["close"])
    o2 = float(klines[-2]["open"]);  c2 = float(klines[-2]["close"])

    bull_ok = False
    if _engulf(o1, c1, o2, c2, bullish=True):
        prior_reds = _consecutive_prior_same_color(klines, target_color=-1, start_index=-2)
        bull_ok = (prior_reds >= 2)

    bear_ok = False
    if _engulf(o1, c1, o2, c2, bullish=False):
        prior_greens = _consecutive_prior_same_color(klines, target_color=1, start_index=-2)
        bear_ok = (prior_greens >= 2)

    return (bull_ok, bear_ok)

def zone_flags(d4h: float, d1d: float, ob: float, os: float) -> Tuple[bool, bool, bool, bool]:
    both_ob = (d4h is not None and d1d is not None) and (d4h >= ob and d1d >= ob)
    both_os = (d4h is not None and d1d is not None) and (d4h <= os and d1d <= os)
    one_ob  = (d4h is not None and d1d is not None) and ((d4h >= ob) ^ (d1d >= ob))
    one_os  = (d4h is not None and d1d is not None) and ((d4h <= os) ^ (d1d <= os))
    return both_ob, both_os, one_ob, one_os


# ===================== ЛОГИКА ПО СИМВОЛУ =====================

def process_symbol(symbol: str, state: Dict[str, Any]) -> None:
    """
    Тянем kline для 4H и 1D, считаем DeMarker, свечи,
    формируем сигналы строго по твоим правилам и шлём ТОЛЬКО символ.
    """
    k4h = get_klines(symbol, "240", limit=DEM_LEN + 50)
    k1d = get_klines(symbol, "D",   limit=DEM_LEN + 50)

    if len(k4h) < DEM_LEN + 2 or len(k1d) < DEM_LEN + 2:
        logging.info(f"{symbol}: not enough data")
        return

    def highs(kl): return [b["high"] for b in kl]
    def lows(kl):  return [b["low"]  for b in kl]

    dem4h = demarker_last(highs(k4h), lows(k4h), DEM_LEN)
    dem1d = demarker_last(highs(k1d), lows(k1d), DEM_LEN)
    bothOB, bothOS, oneOB, oneOS = zone_flags(dem4h, dem1d, DEM_OB, DEM_OS)

    # Wick25 на каждой ТФ (последний закрытый бар [-1])
    o4,h4,l4,c4 = k4h[-1]["open"], k4h[-1]["high"], k4h[-1]["low"], k4h[-1]["close"]
    o1,h1,l1,c1 = k1d[-1]["open"], k1d[-1]["high"], k1d[-1]["low"], k1d[-1]["close"]
    u4,lw4 = wick25_flags(o4,h4,l4,c4)
    u1,lw1 = wick25_flags(o1,h1,l1,c1)
    wick_any = (u4 or lw4 or u1 or lw1)

    # Engulfing (только после серии ≥2 противоположных свечей)
    bull_eng_4h, bear_eng_4h = engulfing_after_two_or_more(k4h)
    bull_eng_1d, bear_eng_1d = engulfing_after_two_or_more(k1d)
    engulf_any = (bull_eng_4h or bear_eng_4h or bull_eng_1d or bear_eng_1d)

    candle_pattern = (wick_any or engulf_any)

    # ===== Правила сигналов =====
    # 1) LIGHT — обе DeM в одной зоне, свечного паттерна НЕТ
    signal_light = (bothOB or bothOS) and (not candle_pattern)

    # 2) L+CAN — обе DeM в одной зоне И есть свечной паттерн
    signal_light_plus_candle = (bothOB or bothOS) and candle_pattern

    # 3) 1TF+CAN — только один TF в зоне OB/OS и есть свечной паттерн
    signal_one_tf_plus_candle = ((oneOB or oneOS) and candle_pattern)

    # === Отправка (только символ). Дедуп — по ключу.
    def maybe_send(sym: str, key: str):
        if state.get(sym) == key:
            return
        tg_send_symbol(sym)
        state[sym] = key

    if signal_light_plus_candle:
        maybe_send(symbol, "L+CAN")
    elif signal_light:
        maybe_send(symbol, "LIGHT")
    elif signal_one_tf_plus_candle:
        maybe_send(symbol, "1TF+CAN")


# ===================== MAIN LOOP =====================

def main():
    if not TICKERS:
        logging.error("No tickers configured.")
        return
    state = load_state(STATE_PATH)
    logging.info(f"Tickers: {len(TICKERS)} symbols loaded")
    logging.info(f"DEM_LEN={DEM_LEN} OB={DEM_OB} OS={DEM_OS} poll={POLL_SECONDS}s")

    while True:
        t0 = time.time()
        try:
            for sym in TICKERS:
                process_symbol(sym, state)
            save_state(STATE_PATH, state)
        except Exception as e:
            logging.error(f"loop error: {e}")
        # задержка
        dt = time.time() - t0
        sleep_left = max(1, POLL_SECONDS - int(dt))
        time.sleep(sleep_left)

if __name__ == "__main__":
    main()