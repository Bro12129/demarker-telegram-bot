# bot.py
import os
import json
import time
import math
import logging
from typing import Dict, List, Tuple, Any

import requests

# ---------------------- LOGGING ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ---------------------- ENV ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))
OS             = float(os.getenv("DEM_OS", "0.30"))

# Состояние (для дедупа сигналов между рестартами)
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# Тикеры и интервалы
# По умолчанию USDT-перпетуалы, как и раньше
TICKERS_ENV    = os.getenv("TICKERS", "BTCUSDT,ETHUSDT")
TICKERS: List[str] = [t.strip().upper() for t in TICKERS_ENV.split(",") if t.strip()]

# 4h и 1d — в минутах для Bybit v5
INTERVALS_MIN  = [240, 1440]

# Bybit v5 — база и путь разделены (фикс 404 при двойном /v5/...)
BYBIT_BASE_URL = os.getenv("BYBIT_URL", "https://api.bybit.com").rstrip("/")
BYBIT_KLINE_URL = f"{BYBIT_BASE_URL}/v5/market/kline"   # <- правильный путь
BYBIT_CATEGORY  = os.getenv("BYBIT_CATEGORY", "linear")  # linear|inverse|spot

# ---------------------- NET UTILS ----------------------
def http_get_json(url: str, params: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    """Обёртка над requests.get с понятными ошибками и ретраями."""
    tries = 3
    last_err = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            logging.error("HTTP GET failed (%s/%s) %s %s", i + 1, tries, url, params)
            time.sleep(1 + i)
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last_err}")

# ---------------------- BYBIT ----------------------
def fetch_klines(symbol: str, interval_minutes: int = 240, limit: int = 300) -> List[Dict[str, Any]]:
    """
    Возвращает список свечей (последняя — текущая, предпоследняя — закрытая).
    Bybit v5: /v5/market/kline
    """
    params = {
        "category": BYBIT_CATEGORY,            # linear по умолчанию как вчера
        "symbol": symbol,
        "interval": str(interval_minutes),     # "240" или "1440"
        "limit": str(limit),
    }
    data = http_get_json(BYBIT_KLINE_URL, params)
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data}")
    rows = data.get("result", {}).get("list", [])
    # Bybit возвращает строки в порядке от новой к старой
    # Преобразуем в удобный формат и реверснем (старые -> новые)
    klines = []
    for row in rows:
        # формат: [startTime, open, high, low, close, volume, turnover]
        ts = int(row[0]) // 1000
        o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
        klines.append({"t": ts, "o": o, "h": h, "l": l, "c": c})
    klines.reverse()
    return klines

# ---------------------- INDICATORS ----------------------
def demarker(high: List[float], low: List[float], length: int) -> List[float]:
    """
    DeMarker:
      DeMax_t = max(high_t - high_{t-1}, 0)
      DeMin_t = max(low_{t-1} - low_t, 0)
      DeM = SMA(DeMax, len) / (SMA(DeMax, len) + SMA(DeMin, len))
    Возвращает список значений той же длины (первые length значений — NaN).
    """
    n = len(high)
    if n != len(low) or n == 0:
        return []
    demax = [0.0] * n
    demin = [0.0] * n
    for i in range(1, n):
        demax[i] = max(high[i] - high[i - 1], 0.0)
        demin[i] = max(low[i - 1] - low[i], 0.0)

    def sma(arr: List[float], m: int) -> List[float]:
        out = [math.nan] * n
        s = 0.0
        for i in range(n):
            s += arr[i]
            if i >= m:
                s -= arr[i - m]
            if i >= m - 1:
                out[i] = s / m
        return out

    demx = sma(demax, length)
    demn = sma(demin, length)
    out = [math.nan] * n
    for i in range(n):
        a = demx[i]; b = demn[i]
        if math.isnan(a) or math.isnan(b) or (a + b) == 0:
            out[i] = math.nan
        else:
            out[i] = a / (a + b)
    return out

def detect_pinbar(c: Dict[str, float]) -> str:
    """
    Простейший пин-бар:
      - длинная тень в 2.5x тела и ≥ 60% всей свечи
      - бычий pin => длинная нижняя тень
      - медвежий pin => длинная верхняя тень
    Возвращает "bull_pin" / "bear_pin" / "".
    """
    o, h, l, cl = c["o"], c["h"], c["l"], c["c"]
    body = abs(cl - o)
    range_ = max(h - l, 1e-12)
    upper = h - max(o, cl)
    lower = min(o, cl) - l

    if body < range_ * 0.4:  # тело не доминирует
        if lower >= max(upper, body * 2.5) and lower >= range_ * 0.6:
            return "bull_pin"
        if upper >= max(lower, body * 2.5) and upper >= range_ * 0.6:
            return "bear_pin"
    return ""

# ---------------------- STATE ----------------------
def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(path: str, state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logging.error("Failed to save state: %s", e)

# ---------------------- TELEGRAM ----------------------
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM credentials are empty; skip send")
        return
    try:
        resp = requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error("Telegram send failed: %s", e)

# ---------------------- SIGNALS ----------------------
def analyze_symbol(symbol: str) -> List[Tuple[str, str]]:
    """
    Возвращает список сигналов в формате [(dedup_key, message), ...]
    Генерируем:
      1) Сигналы по каждому ТФ отдельно (4h, 1d)
      2) Комбинированный сигнал (если оба ТФ в одинаковой зоне OB/OS)
      3) Усиление сигналом пин-бара (если есть на соответствующем ТФ)
    """
    signals: List[Tuple[str, str]] = []

    data_by_tf: Dict[int, List[Dict[str, Any]]] = {}
    dem_by_tf: Dict[int, List[float]] = {}

    # Загружаем свечи и ДеМаркер
    for tf in INTERVALS_MIN:
        try:
            kl = fetch_klines(symbol, tf, limit=max(DEM_LEN + 50, 300))
            if len(kl) < DEM_LEN + 2:
                continue
            data_by_tf[tf] = kl
            dem_by_tf[tf] = demarker([x["h"] for x in kl], [x["l"] for x in kl], DEM_LEN)
        except Exception as e:
            logging.error("%s %s fetch/analyze failed: %s", symbol, tf, e)

    if not data_by_tf:
        return signals

    def fmt_tf(tf_m: int) -> str:
        return "4H" if tf_m == 240 else "1D" if tf_m == 1440 else f"{tf_m}m"

    # Последняя ЗАКРЫТАЯ свеча = индекс -2
    status: Dict[int, Dict[str, Any]] = {}
    for tf, kl in data_by_tf.items():
        dems = dem_by_tf[tf]
        if not dems or math.isnan(dems[-2]):
            continue
        last = kl[-2]
        pb = detect_pinbar(last)
        zone = "OB" if dems[-2] >= OB else "OS" if dems[-2] <= OS else "NEUTRAL"
        status[tf] = {"ts": last["t"], "dem": dems[-2], "zone": zone, "pin": pb, "bar": last}

    # Индивидуальные сигналы
    for tf, st in status.items():
        if st["zone"] == "OB":
            sig = "SELL"
        elif st["zone"] == "OS":
            sig = "BUY"
        else:
            continue

        extras = []
        if st["pin"] == "bear_pin" and sig == "SELL":
            extras.append("bear pin")
        if st["pin"] == "bull_pin" and sig == "BUY":
            extras.append("bull pin")

        msg = (
            f"🔔 {symbol} {sig} — {fmt_tf(tf)}\n"
            f"DeM({DEM_LEN})={st['dem']:.3f} [{st['zone']}], "
            f"OB={OB:.2f} / OS={OS:.2f}"
            + (f"\nCandle: {', '.join(extras)}" if extras else "")
        )
        key = f"{symbol}:{tf}:{st['ts']}:{sig}"
        signals.append((key, msg))

    # Комбинированный сигнал (оба ТФ совпали)
    if 240 in status and 1440 in status:
        zone4 = status[240]["zone"]
        zone1d = status[1440]["zone"]
        if zone4 in ("OB", "OS") and zone4 == zone1d:
            sig = "SELL" if zone4 == "OB" else "BUY"
            msg = (
                f"⚡ {symbol} {sig} — 4H & 1D согласованы\n"
                f"4H DeM={status[240]['dem']:.3f} [{zone4}] | "
                f"1D DeM={status[1440]['dem']:.3f} [{zone1d}]\n"
                f"OB={OB:.2f} / OS={OS:.2f}"
            )
            key = f"{symbol}:combo:{status[240]['ts']}:{status[1440]['ts']}:{sig}"
            signals.append((key, msg))

    return signals

# ---------------------- MAIN LOOP ----------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")

    state = load_state(STATE_PATH)
    if "sent" not in state:
        state["sent"] = {}
    sent = state["sent"]

    logging.info("Worker started | TICKERS=%s | CAT=%s | URL=%s", TICKERS, BYBIT_CATEGORY, BYBIT_KLINE_URL)

    while True:
        try:
            for sym in TICKERS:
                try:
                    sigs = analyze_symbol(sym)
                    for key, msg in sigs:
                        if key in sent:
                            continue
                        tg_send(msg)
                        sent[key] = int(time.time())
                        # ограничиваем размер памяти дедупа
                        if len(sent) > 5000:
                            # удалим самые старые
                            to_del = sorted(sent.items(), key=lambda x: x[1])[:1000]
                            for k, _ in to_del:
                                sent.pop(k, None)
                except Exception as e:
                    logging.error("%s analyze failed: %s", sym, e)

            save_state(STATE_PATH, state)
        except Exception as e:
            logging.error("Main loop error: %s", e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()