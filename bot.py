import os
import time
import math
import logging
from typing import List, Dict, Tuple
from datetime import datetime, timezone

import requests

# ======================= ENV =======================
# допускаем оба названия переменных
def env_any(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None:
            return v
    return default

BOT_TOKEN = env_any("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN").strip()
CHAT_RAW  = env_any("TELEGRAM_CHAT_ID", "TG_CHAT_ID").strip()
CHAT_IDS  = [c.strip() for c in CHAT_RAW.split(",") if c.strip()]

TICKERS    = [t.strip().upper() for t in os.getenv("TICKERS", "BTCUSDT,ETHUSDT").split(",") if t.strip()]
TIMEFRAMES = [tf.strip().lower() for tf in os.getenv("TIMEFRAMES", "15m,1h,4h,1d").split(",") if tf.strip()]

LOG_LEVEL     = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_SECONDS  = int(os.getenv("POLL_SECONDS", "30"))
DEM_PERIOD    = int(os.getenv("DEM_PERIOD", "28"))
THRESH_LOW    = float(os.getenv("THRESH_LOW", "0.30"))
THRESH_HIGH   = float(os.getenv("THRESH_HIGH", "0.70"))
FORCE_TEST    = os.getenv("FORCE_TEST", "0") == "1"
HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MIN", "0"))  # 0 = выкл

# Bybit только деривативы (USDT-перпетуалы). Второй хост — официальное зеркало.
BYBIT_BASES = [
    os.getenv("BYBIT_BASE", "https://api.bybit.com").rstrip("/"),
    "https://api.bytick.com",
]
BYBIT_HEADERS = {
    "User-Agent": "demarker-derivs-bot/1.0 (render)",
    "Accept": "application/json",
    "Connection": "close",
}

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("demarker-bot")

if not BOT_TOKEN or not CHAT_IDS:
    log.error("ENV TELEGRAM_BOT_TOKEN/TG_BOT_TOKEN и TELEGRAM_CHAT_ID/TG_CHAT_ID должны быть заданы.")
    raise SystemExit(1)

# =================== HELPERS =======================
BYBIT_INTERVAL = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W", "1mo": "M",
}

def tf_to_bybit(tf: str) -> str:
    v = BYBIT_INTERVAL.get(tf)
    if not v:
        raise ValueError(f"Неподдерживаемый таймфрейм: {tf}")
    return v

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat in CHAT_IDS:
        try:
            requests.post(url, data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            log.warning(f"sendMessage({chat}) error: {e}")

def ts_fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# =================== DATA ==========================
def fetch_ohlcv_bybit(symbol: str, tf: str, limit: int = 200) -> List[Dict]:
    """Только деривативы Bybit v5: /v5/market/kline?category=linear"""
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": tf_to_bybit(tf),
        "limit": str(limit),
    }
    last_err = None
    for base in BYBIT_BASES:
        url = f"{base}/v5/market/kline"
        try:
            r = requests.get(url, params=params, headers=BYBIT_HEADERS, timeout=15)
            if r.status_code == 403:
                last_err = f"403 on {base}"
                log.warning(f"fetch_ohlcv: 403 {symbol} {tf} on {base}, try next mirror")
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("retCode") != 0:
                raise RuntimeError(data.get("retMsg"))
            rows = data["result"]["list"]  # newest-first
            rows.reverse()                 # oldest-first
            out = []
            for it in rows:
                out.append({
                    "ts": int(it[0]),
                    "open": float(it[1]),
                    "high": float(it[2]),
                    "low":  float(it[3]),
                    "close":float(it[4]),
                })
            return out
        except Exception as e:
            last_err = e
            log.warning(f"fetch_ohlcv try {base} {symbol} {tf}: {e}")
            continue
    log.warning(f"fetch_ohlcv error {symbol} {tf}: {last_err}")
    return []

# ================= INDICATORS ======================
def sma(arr: List[float], p: int) -> List[float]:
    out = [math.nan]*len(arr)
    s = 0.0
    for i, v in enumerate(arr):
        s += v
        if i >= p:
            s -= arr[i-p]
        if i >= p-1:
            out[i] = s/p
    return out

def demarker(highs: List[float], lows: List[float], p: int) -> List[float]:
    n = len(highs)
    up = [0.0]*n
    dn = [0.0]*n
    for i in range(1, n):
        up[i] = max(highs[i] - highs[i-1], 0.0)
        dn[i] = max(lows[i-1] - lows[i], 0.0)
    upm = sma(up, p)
    dnm = sma(dn, p)
    out = [math.nan]*n
    for i in range(n):
        if not math.isnan(upm[i]) and not math.isnan(dnm[i]):
            den = upm[i] + dnm[i]
            out[i] = (upm[i]/den) if den != 0 else 0.5
    return out

def bull_pin(o,h,l,c) -> bool:
    rng = max(h-l, 1e-12)
    body = abs(c-o)
    upper = h - max(o,c)
    lower = min(o,c) - l
    return lower >= 0.6*rng and upper <= 0.2*rng and body <= 0.25*rng

def bear_pin(o,h,l,c) -> bool:
    rng = max(h-l, 1e-12)
    body = abs(c-o)
    upper = h - max(o,c)
    lower = min(o,c) - l
    return upper >= 0.6*rng and lower <= 0.2*rng and body <= 0.25*rng

# =============== STRATEGY/ENGINE ===================
sent: set[Tuple[str,str,int,str]] = set()  # (symbol, tf, ts, side)

def analyze(symbol: str, tf: str):
    candles = fetch_ohlcv_bybit(symbol, tf, limit=max(DEM_PERIOD+60, 150))
    if len(candles) < DEM_PERIOD + 5:
        return

    highs  = [x["high"]  for x in candles]
    lows   = [x["low"]   for x in candles]
    opens  = [x["open"]  for x in candles]
    closes = [x["close"] for x in candles]

    dem = demarker(highs, lows, DEM_PERIOD)

    i = len(candles) - 2            # последняя ЗАКРЫТАЯ свеча
    if i < 0 or math.isnan(dem[i]):
        return

    ts = candles[i]["ts"]
    d  = dem[i]
    bp = bull_pin(opens[i], highs[i], lows[i], closes[i])
    sp = bear_pin(opens[i], highs[i], lows[i], closes[i])

    if d < THRESH_LOW and bp:
        key = (symbol, tf, ts, "LONG")
        if key not in sent:
            tg_send(
                f"<b>{symbol}</b> — <b>LONG</b> ✅\n"
                f"TF: <b>{tf}</b>\n"
                f"DeM({DEM_PERIOD})=<b>{d:.3f}</b> (<{THRESH_LOW})\n"
                f"Pin-bar: бычий\n"
                f"Свеча: <code>{ts_fmt(ts)}</code>"
            )
            sent.add(key)
            log.info(f"ALERT LONG {symbol} {tf} ts={ts} dem={d:.3f}")

    if d > THRESH_HIGH and sp:
        key = (symbol, tf, ts, "SHORT")
        if key not in sent:
            tg_send(
                f"<b>{symbol}</b> — <b>SHORT</b> ❌\n"
                f"TF: <b>{tf}</b>\n"
                f"DeM({DEM_PERIOD})=<b>{d:.3f}</b> (>{THRESH_HIGH})\n"
                f"Pin-bar: медвежий\n"
                f"Свеча: <code>{ts_fmt(ts)}</code>"
            )
            sent.add(key)
            log.info(f"ALERT SHORT {symbol} {tf} ts={ts} dem={d:.3f}")

def main():
    tg_send("🤖 Demarker bot запускается…")
    if FORCE_TEST:
        tg_send("✅ Тест: бот жив, ENV читаются.")
    log.info(f"Started TICKERS={TICKERS} TF={TIMEFRAMES} DEM={DEM_PERIOD} "
             f"LOW={THRESH_LOW} HIGH={THRESH_HIGH}")
    last_hb = time.time()
    while True:
        t0 = time.time()
        try:
            for s in TICKERS:
                for tf in TIMEFRAMES:
                    analyze(s, tf)
        except Exception as e:
            log.exception(f"loop error: {e}")

        if HEARTBEAT_MIN > 0 and (time.time() - last_hb) >= HEARTBEAT_MIN*60:
            tg_send("⏱️ Heartbeat: бот работает.")
            last_hb = time.time()

        time.sleep(max(1.0, POLL_SECONDS - (time.time() - t0)))

if __name__ == "__main__":
    main()