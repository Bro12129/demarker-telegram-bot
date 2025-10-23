# bot.py
import os, time, math, requests
from typing import List, Tuple, Optional

# === Telegram secrets ===
TG_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT = os.getenv("TG_CHAT_ID")
assert TG_TOKEN and TG_CHAT, "Нужны TG_BOT_TOKEN и TG_CHAT_ID в Secrets"

# === Настройки ===
DEM_PERIOD = 28
TH_UPPER = 0.70  # перекупленность -> 🔻
TH_LOWER = 0.30  # перепроданность -> 🔺

INTERVALS = {"4H": "240", "1D": "D"}  # Bybit интервалы
CATEGORY = "linear"                   # фьючерсы USDT
POLL_SEC = 300                        # опрос каждые 5 минут

# === ДВА API-ДОСТУПА ===
# оригинальный (США блокируется)
BYBIT_ORIGINAL_URL = "https://api.bybit.com/v5/market/kline"
# глобальный альтернативный (работает в Европе, Азии и через VPN)
BYBIT_GLOBAL_URL = "https://api.bybitglobal.com/v5/market/kline"

# используем глобальный по умолчанию:
BYBIT_KLINE_URL = BYBIT_GLOBAL_URL

TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

# === Функция DeMarker ===
def demarker(values: List[float], period: int = 28):
    highs, lows, closes = [], [], []
    for i in range(len(values) - period):
        highs.append(max(values[i:i+period]))
        lows.append(min(values[i:i+period]))
        closes.append(values[i+period-1])
    if not highs or not lows:
        return None
    dem = (sum(highs) - sum(lows)) / (sum(highs) + sum(lows))
    return dem

# === Отправка в Telegram ===
def send_msg(text: str):
    try:
        requests.post(TG_URL, data={"chat_id": TG_CHAT, "text": text})
    except Exception as e:
        print("Ошибка Telegram:", e)

# === Загрузка свечей с Bybit ===
def get_klines(symbol: str, interval: str):
    try:
        url = BYBIT_KLINE_URL
        params = {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": 200}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 403:
            print(f"403: {symbol} — попробуй VPN или Render (регион вне США)")
            return []
        data = r.json()
        if "result" not in data or "list" not in data["result"]:
            print(f"Ошибка Bybit для {symbol}: {data}")
            return []
        return [float(c[4]) for c in data["result"]["list"]]
    except Exception as e:
        print(f"Ошибка при загрузке {symbol}: {e}")
        return []

# === Основная логика ===
TICKERS = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT",
    "XAUUSDT", "XAGUSDT", "US500", "NAS100", "EURUSDT", "GBPUSDT",
    "AUDUSDT", "USDJPY", "USDCUSDT", "ADAUSDT", "DOTUSDT", "AVAXUSDT"
]

while True:
    for ticker in TICKERS:
        for tf, interval in INTERVALS.items():
            closes = get_klines(ticker, interval)
            if not closes: 
                continue

            d_val = demarker(closes[-DEM_PERIOD:], DEM_PERIOD)
            if not d_val:
                continue

            if d_val > TH_UPPER:
                send_msg(f"🔻 {ticker} — перекупленность ({tf})")
            elif d_val < TH_LOWER:
                send_msg(f"🔺 {ticker} — перепроданность ({tf})")

            print(f"{ticker} {tf}: {d_val:.2f}")

    time.sleep(POLL_SEC)
