import os
import time
import math
import requests
from datetime import datetime, timezone

# ==========================
# НАСТРОЙКИ
# ==========================
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "DOGEUSDT","TONUSDT","TRXUSDT","LINKUSDT","MATICUSDT",
    "AVAXUSDT","NEARUSDT","DOTUSDT","ATOMUSDT","LTCUSDT",
    "BCHUSDT","UNIUSDT","INJUSDT","APTUSDT","SUIUSDT",
    "OPUSDT","ARBUSDT","SEIUSDT","FILUSDT","AAVEUSDT",
    "IMXUSDT","FTMUSDT","HBARUSDT","PEPEUSDT","SHIBUSDT"
]  # 30 тикеров Bybit (USDT-perp, category=linear)

TIMEFRAME_MIN = 240         # 4H (минуты)
DEM_LEN       = 28          # DeMarker период
OB            = 0.70        # перекупленность
OS            = 0.30        # перепроданность
CYCLE_SECONDS = 300         # пауза между циклами (сек)

# ==========================
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "").strip()
if not TG_BOT_TOKEN or not TG_CHAT_ID:
    raise AssertionError("Нужны TG_BOT_TOKEN и TG_CHAT_ID в секретах")

# ==========================
# КОНСТАНТЫ API
# ==========================
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"

# ==========================
# УТИЛИТЫ
# ==========================
def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    print(f"[{now_utc_str()}] {msg}", flush=True)

def send_telegram(text: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        if r.status_code != 200 or not r.json().get("ok"):
            log(f"Telegram send fail: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"Telegram exception: {e}")

def fetch_klines(symbol: str, interval_min: int, limit: int = 200):
    """
    Bybit V5 kline:
      GET /v5/market/kline?category=linear&symbol=BTCUSDT&interval=240&limit=200
    Возвращает (highs, lows, closes) в порядке от старых к новым.
    """
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": str(interval_min),
        "limit": str(limit),
    }
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=25)
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error for {symbol}: {data.get('retCode')} {data.get('retMsg')}")
    li = data["result"]["list"]  # список свечей: новые -> старые
    li.reverse()                 # делаем старые -> новые
    highs  = [float(x[2]) for x in li]
    lows   = [float(x[3]) for x in li]
    closes = [float(x[4]) for x in li]
    return highs, lows, closes

def demarker(highs, lows, length: int):
    """
    DeMarker:
      deMax[i] = max(high[i] - high[i-1], 0)
      deMin[i] = max(low[i-1] - low[i], 0)
      DeM = SUM(deMax, len) / (SUM(deMax, len) + SUM(deMin, len))
    Возвращает массив DeM от старых к новым (первые length = NaN).
    """
    n = len(highs)
    if n != len(lows):
        raise ValueError("highs/lows length mismatch")
    deMax = [0.0]*n
    deMin = [0.0]*n
    for i in range(1, n):
        dh = highs[i] - highs[i-1]
        dl = lows[i-1] - lows[i]
        deMax[i] = dh if dh > 0 else 0.0
        deMin[i] = dl if dl > 0 else 0.0

    deM = [math.nan]*n
    sMax = 0.0
    sMin = 0.0
    for i in range(n):
        sMax += deMax[i]
        sMin += deMin[i]
        if i >= length:
            sMax -= deMax[i-length]
            sMin -= deMin[i-length]
        if i >= length:
            denom = sMax + sMin
            deM[i] = (sMax/denom) if denom > 0 else 0.5
    return deM

def zone(value: float):
    if math.isnan(value):
        return "mid"
    if value >= OB:
        return "ob"
    if value <= OS:
        return "os"
    return "mid"

# чтобы не спамить повторно
last_zone = {}  # symbol -> "ob"/"os"/"mid"

# ==========================
# ОСНОВНОЙ ЦИКЛ
# ==========================
def run_once():
    for sym in SYMBOLS:
        try:
            highs, lows, closes = fetch_klines(sym, TIMEFRAME_MIN, limit=max(DEM_LEN+50, 120))
            dem = demarker(highs, lows, DEM_LEN)

            # Берём последнюю ЗАКРЫТУЮ свечу 4H: это предпоследний элемент массива
            if len(dem) < 2:
                continue
            curr = dem[-2]
            prev = dem[-3] if len(dem) >= 3 else math.nan

            z_prev = zone(prev)
            z_curr = zone(curr)
            z_last = last_zone.get(sym, "mid")
            last_zone[sym] = z_curr

            enter_ob = (z_prev != "ob" and z_curr == "ob")
            enter_os = (z_prev != "os" and z_curr == "os")

            if enter_ob and z_last != "ob":
                send_telegram(
                    f"📈 <b>{sym}</b> • DeMarker(28) 4H = <b>{curr:.2f}</b>\n"
                    f"Перекупленность (>{OB}). Возможна коррекция."
                )
                log(f"{sym} -> OB ({curr:.3f})")

            if enter_os and z_last != "os":
                send_telegram(
                    f"📉 <b>{sym}</b> • DeMarker(28) 4H = <b>{curr:.2f}</b>\n"
                    f"Перепроданность (<{OS}). Возможен отскок."
                )
                log(f"{sym} -> OS ({curr:.3f})")

            log(f"{sym} DeM={curr:.3f} zone={z_curr}")

        except Exception as e:
            log(f"ERR {sym}: {e}")

def main():
    log("Bot started. DeMarker screener 4H • " + ",".join(SYMBOLS))
    send_telegram("✅ Бот запущен. Мониторю 30 тикеров (4H DeMarker).")
    while True:
        run_once()
        time.sleep(CYCLE_SECONDS)

if __name__ == "__main__":
    main()