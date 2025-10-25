import os
import time
import json
import math
import logging
from typing import List, Dict, Tuple
import requests
from datetime import datetime, timezone

# -------------------- Config --------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "BTCUSDT,ETHUSDT").split(",") if t.strip()]
TIMEFRAMES = [tf.strip().lower() for tf in os.getenv("TIMEFRAMES", "15m,1h,4h,1d").split(",") if tf.strip()]
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))  # как часто проверять обновление свечей

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("demarker-bot")

if not BOT_TOKEN or not CHAT_IDS:
    log.error("ENV TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы. Завершаюсь.")
    raise SystemExit(1)

# -------------------- Helpers --------------------
BYBIT_INTERVAL_MAP = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
    "1mo": "M",
}

def utc_ms() -> int:
    return int(time.time() * 1000)

def tf_to_bybit(tf: str) -> str:
    if tf not in BYBIT_INTERVAL_MAP:
        raise ValueError(f"Неподдерживаемый таймфрейм: {tf}")
    return BYBIT_INTERVAL_MAP[tf]

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat in CHAT_IDS:
        try:
            requests.post(url, data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            log.warning(f"sendMessage error chat={chat}: {e}")

def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# -------------------- Market data --------------------
def fetch_ohlcv_bybit(symbol: str, tf: str, limit: int = 200) -> List[Dict]:
    """
    Bybit v5 kline: https://api.bybit.com/v5/market/kline
    category=linear (USDT perpetual), symbol=BTCUSDT, interval in map above.
    Возвращает список от старых к новым (мы сами развернём).
    """
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": tf_to_bybit(tf),
        "limit": str(limit),
    }
    try:
        r = requests.get("https://api.bybit.com/v5/market/kline", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(data.get("retMsg"))
        rows = data["result"]["list"]  # newest first
        rows.reverse()                 # делаем старые -> новые
        ohlcv = []
        for item in rows:
            # [ startTime, open, high, low, close, volume, turnover ]
            ts = int(item[0])
            ohlcv.append({
                "ts": ts,
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
            })
        return ohlcv
    except Exception as e:
        log.warning(f"fetch_ohlcv error {symbol} {tf}: {e}")
        return []

# -------------------- Indicators --------------------
def demarker(values_high: List[float], values_low: List[float], period: int = 28) -> List[float]:
    """
    DeMarker классический: 
    DeMM = MA( max(H_t - H_{t-1}, 0), period )
    DeMR = MA( max(L_{t-1} - L_t, 0), period )
    DeM = DeMM / (DeMM + DeMR)
    """
    n = len(values_high)
    up = [0.0] * n
    down = [0.0] * n
    for i in range(1, n):
        up[i] = max(values_high[i] - values_high[i-1], 0.0)
        down[i] = max(values_low[i-1] - values_low[i], 0.0)

    def sma(arr, p):
        out = [math.nan]*len(arr)
        s = 0.0
        for i, v in enumerate(arr):
            s += v
            if i >= p:
                s -= arr[i-p]
            if i >= p-1:
                out[i] = s / p
        return out

    up_ma = sma(up, period)
    dn_ma = sma(down, period)
    dem = [math.nan]*n
    for i in range(n):
        num = up_ma[i]
        den = up_ma[i] + dn_ma[i] if not math.isnan(up_ma[i]) and not math.isnan(dn_ma[i]) else math.nan
        if den and not math.isnan(den):
            dem[i] = num / den if den != 0 else 0.5
    return dem

def is_bullish_pinbar(o,h,l,c) -> bool:
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    upper = h - max(o,c)
    lower = min(o,c) - l
    # длинная нижняя тень, маленькое тело в верхней части диапазона
    return lower >= 0.6*rng and upper <= 0.2*rng and body <= 0.25*rng

def is_bearish_pinbar(o,h,l,c) -> bool:
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    upper = h - max(o,c)
    lower = min(o,c) - l
    # длинная верхняя тень, маленькое тело в нижней части диапазона
    return upper >= 0.6*rng and lower <= 0.2*rng and body <= 0.25*rng

# -------------------- Strategy --------------------
THRESH_LOW = 0.30
THRESH_HIGH = 0.70
DEM_PERIOD = int(os.getenv("DEM_PERIOD", "28"))

# чтобы не слать дубли
sent_keys: set[Tuple[str,str,int,str]] = set()
# key = (symbol, timeframe, close_ts, direction)

def analyze_symbol(symbol: str, timeframe: str):
    candles = fetch_ohlcv_bybit(symbol, timeframe, limit=max(DEM_PERIOD+50, 120))
    if len(candles) < DEM_PERIOD + 5:
        return

    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    closes = [c["close"] for c in candles]
    opens  = [c["open"] for c in candles]

    dem = demarker(highs, lows, DEM_PERIOD)

    # Берём последнюю ЗАКРЫТУЮ свечу ([-2]), потому что [-1] чаще ещё формируется у некоторых бирж.
    i = len(candles) - 2
    cndl = candles[i]
    d = dem[i]

    if math.isnan(d):
        return

    # pin-bar на этой же закрытой свече
    bull_pin = is_bullish_pinbar(opens[i], highs[i], lows[i], closes[i])
    bear_pin = is_bearish_pinbar(opens[i], highs[i], lows[i], closes[i])

    long_cond = (d < THRESH_LOW and bull_pin)
    short_cond = (d > THRESH_HIGH and bear_pin)

    if long_cond:
        key = (symbol, timeframe, cndl["ts"], "LONG")
        if key not in sent_keys:
            msg = (
                f"<b>{symbol}</b> — <b>LONG</b> ✅\n"
                f"TF: <b>{timeframe}</b>\n"
                f"DeMarker({DEM_PERIOD}) = <b>{d:.3f}</b> (ниже {THRESH_LOW})\n"
                f"Pin-bar: <b>бычий</b>\n"
                f"Свеча закрыта: <code>{fmt_ts(cndl['ts'])}</code>"
            )
            tg_send(msg)
            sent_keys.add(key)
            log.info(f"ALERT LONG {symbol} {timeframe} ts={cndl['ts']} dem={d:.3f}")

    if short_cond:
        key = (symbol, timeframe, cndl["ts"], "SHORT")
        if key not in sent_keys:
            msg = (
                f"<b>{symbol}</b> — <b>SHORT</b> ❌\n"
                f"TF: <b>{timeframe}</b>\n"
                f"DeMarker({DEM_PERIOD}) = <b>{d:.3f}</b> (выше {THRESH_HIGH})\n"
                f"Pin-bar: <b>медвежий</b>\n"
                f"Свеча закрыта: <code>{fmt_ts(cndl['ts'])}</code>"
            )
            tg_send(msg)
            sent_keys.add(key)
            log.info(f"ALERT SHORT {symbol} {timeframe} ts={cndl['ts']} dem={d:.3f}")

# -------------------- Main loop --------------------
def main():
    tg_send("🤖 Demarker bot запущен. Мониторю: " + ", ".join(f"{s}@{tf}" for s in TICKERS for tf in TIMEFRAMES))
    log.info(f"Started. TICKERS={TICKERS} TIMEFRAMES={TIMEFRAMES} DEM_PERIOD={DEM_PERIOD}")
    while True:
        start = time.time()
        try:
            for sym in TICKERS:
                for tf in TIMEFRAMES:
                    analyze_symbol(sym, tf)
        except Exception as e:
            log.exception(f"Loop error: {e}")
        # динамический sleep с учётом времени выполнения
        dt = time.time() - start
        time.sleep(max(1.0, POLL_SECONDS - dt))

if __name__ == "__main__":
    main()