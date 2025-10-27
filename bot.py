# bot.py — финальная стабильная версия

import os, time, json, logging, requests, re
from typing import List, Dict, Tuple

# ---------------------- ENV ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))
OS             = float(os.getenv("DEM_OS", "0.30"))

STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# ---------------------- BYBIT v5 (фикс URL) ----------------------
BYBIT_BASE = os.getenv("BYBIT_URL", "https://api.bybit.com").rstrip("/")
KLINE_EP   = "/v5/market/kline"
BYBIT_KLINE_URL = f"{BYBIT_BASE}{KLINE_EP}"

def fetch_kline(symbol: str, interval: str, limit: int = 200, category: str = "linear", timeout: int = 20):
    params = {
        "category": category,
        "symbol":   symbol,
        "interval": str(interval),
        "limit":    str(limit),
    }
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------- УТИЛИТЫ ----------------------
def load_state() -> Dict:
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except:
            return {}
    return {}

def save_state(state: Dict):
    json.dump(state, open(STATE_PATH, "w"))

def send_tg(symbol: str, text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    payload = {"chat_id": CHAT_ID, "text": f"{symbol} {text}", "disable_notification": True}
    try:
        requests.post(TG_API, data=payload, timeout=10)
    except Exception as e:
        logging.error(f"TG send error: {e}")

# ---------------------- ТИКЕРЫ ----------------------
def parse_symbols():
    raw = os.getenv("TICKERS", os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"))
    parts = [p.strip().upper() for p in re.split(r"[,\s]+", raw) if p.strip()]
    return list(dict.fromkeys(parts))

SYMBOLS = parse_symbols()

# ---------------------- DEMARKER ----------------------
def demarker(values: List[float], length: int = DEM_LEN):
    if len(values) <= length:
        return None
    high = values[-length:]
    diff_up = sum(max(high[i] - high[i-1], 0) for i in range(1, len(high)))
    diff_dn = sum(max(high[i-1] - high[i], 0) for i in range(1, len(high)))
    if diff_up + diff_dn == 0:
        return 0.5
    return diff_up / (diff_up + diff_dn)

def zone(value: float):
    if value >= OB:
        return "overbought"
    elif value <= OS:
        return "oversold"
    return "neutral"

# ---------------------- ПАТТЕРНЫ ----------------------
def detect_pattern(candles: List[Dict]):
    last = candles[-1]
    open_, close, high, low = map(float, [last['open'], last['close'], last['high'], last['low']])
    body = abs(close - open_)
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low

    # длинный фитиль — паттерн
    long_upper = upper_wick > body * 2
    long_lower = lower_wick > body * 2

    # бычий / медвежий пин-бар
    if long_lower and close > open_:
        return "bullish_pin"
    if long_upper and close < open_:
        return "bearish_pin"
    return None

# ---------------------- ОСНОВНОЙ ЦИКЛ ----------------------
state = load_state()
logging.info(f"Start bot with {len(SYMBOLS)} symbols...")

while True:
    for sym in SYMBOLS:
        try:
            data_4h = fetch_kline(sym, "240")
            data_1d = fetch_kline(sym, "D")

            candles_4h = data_4h["result"]["list"]
            candles_1d = data_1d["result"]["list"]

            # последние цены
            close_4h = [float(c[4]) for c in candles_4h]
            close_1d = [float(c[4]) for c in candles_1d]

            d4 = demarker(close_4h)
            d1 = demarker(close_1d)

            z4, z1 = zone(d4), zone(d1)
            pattern = detect_pattern([{
                "open": candles_4h[-1][1],
                "high": candles_4h[-1][2],
                "low": candles_4h[-1][3],
                "close": candles_4h[-1][4]
            }])

            key = f"{sym}_{z4}_{pattern}"
            if state.get(sym) == key:
                continue

            # сигналы
            signal = None
            if pattern == "bullish_pin" and z4 == "oversold":
                signal = "🟢⬆️"
            elif pattern == "bearish_pin" and z4 == "overbought":
                signal = "🔴⬇️"
            elif z4 == z1 and z4 in ("overbought", "oversold"):
                signal = "⚡️"

            if signal:
                send_tg(sym, signal)
                state[sym] = key

        except Exception as e:
            logging.error(f"{sym}: {e}")
            continue

    save_state(state)
    time.sleep(POLL_SECONDS)