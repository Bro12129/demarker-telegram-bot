# bot.py — авто-49 тикеров, стабильные сигналы (2025-10-27)

import os
import time
import json
import logging
import requests
from typing import Dict, List

# ---------------------- ENV (оставляем как есть на Render) ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

BYBIT_URL      = os.getenv("BYBIT_URL", "https://api.bybit.com/v5/market/kline")
CATEGORY       = os.getenv("BYBIT_CATEGORY", "linear")   # linear|inverse|spot (используем как приоритет)

# ---------------------- Параметры индикатора ----------------------
DEM_LEN = int(os.getenv("DEM_LEN", "28"))
OB      = float(os.getenv("DEM_OB", "0.70"))
OS      = float(os.getenv("DEM_OS", "0.30"))

# ---------------------- Константы TF ----------------------
TF_4H = "240"
TF_1D = "D"

# ---------------------- Логирование ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# =========================================================
#                      УТИЛИТЫ
# =========================================================
def load_state() -> Dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.error("Telegram credentials missing")
        return False
    for i in range(3):
        try:
            r = requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
            if r.status_code == 200:
                return True
            logging.warning(f"TG send {r.status_code}: {r.text}")
            time.sleep(1 + 2*i)
        except Exception as e:
            logging.exception(f"TG send err: {e}")
            time.sleep(1 + 2*i)
    return False

# =========================================================
#             ЗАГРУЗКА СПИСКА ФЬЮЧЕРСНЫХ ТИКЕРОВ
# =========================================================
def fetch_symbols_from_bybit(limit: int = 49) -> List[str]:
    """
    Берём USDT-перпетуалы (Perpetual, Trading) из Bybit v5 instruments-info:
    приоритет linear, затем inverse; фильтруем quoteCoin == USDT.
    Добавляем металлы/индексы, если они есть в выборке биржи.
    """
    url = "https://api.bybit.com/v5/market/instruments-info"
    got = set()

    def pull(cat: str):
        params = {
            "category": cat,            # linear | inverse
            "contractType": "Perpetual",
            "status": "Trading",
            "limit": "1000"
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        items = (r.json().get("result") or {}).get("list") or []
        for it in items:
            sym   = (it.get("symbol") or "").upper()
            quote = (it.get("quoteCoin") or "").upper()
            if quote == "USDT" and sym.endswith("USDT"):
                got.add(sym)

    # основной пул
    pull("linear")
    pull("inverse")  # на всякий случай — некоторые рынки могут лежать здесь

    # Отсортируем и ограничим
    symbols = sorted(got)
    if len(symbols) > limit:
        symbols = symbols[:limit]
    return symbols

# =========================================================
#                       BYBIT KLINES
# =========================================================
def bybit_klines(symbol: str, interval: str, limit: int = 200):
    """
    Берём свечи. Сначала пытаемся в приоритетной CATEGORY, затем fallback: spot -> linear -> inverse.
    Возвращаем ТОЛЬКО закрытые свечи (отрезаем текущую).
    Формат элементов: [openTime, open, high, low, close, volume, ...] (строки)
    """
    order = []
    if CATEGORY:
        order.append(CATEGORY)
    for c in ("spot", "linear", "inverse"):
        if c not in order:
            order.append(c)

    for cat in order:
        params = {"category": cat, "symbol": symbol, "interval": interval, "limit": str(limit)}
        r = requests.get(BYBIT_URL, params=params, timeout=15)
        r.raise_for_status()
        data = (r.json().get("result") or {}).get("list") or []
        if not data:
            continue
        data = sorted(data, key=lambda x: int(x[0]))
        if len(data) >= 2:
            data = data[:-1]  # срезаем текущую незакрытую
        return data
    logging.warning(f"No klines for {symbol} in categories {order}")
    return []

# =========================================================
#                        DEMARKER
# =========================================================
def calc_demarker(closes, highs, lows, length=DEM_LEN):
    up, dn = [], []
    for i in range(1, len(closes)):
        up.append(max(0.0, float(highs[i]) - float(highs[i-1])))
        dn.append(max(0.0, float(lows[i-1]) - float(lows[i])))
    n = min(len(up), len(dn))
    up, dn = up[-n:], dn[-n:]
    dem = []
    for i in range(length, n):
        su = sum(up[i-length:i])
        sd = sum(dn[i-length:i])
        denom = (su + sd) if (su + sd) > 0 else 1e-12
        dem.append(su / denom)
    return dem

# =========================================================
#                    СВЕЧНЫЕ ПАТТЕРНЫ
# =========================================================
def is_pinbar(o, h, l, c, body_ratio=0.33, wick_ratio=2.0) -> bool:
    o, h, l, c = map(float, (o, h, l, c))
    body = abs(c - o)
    rng  = max(1e-12, h - l)
    upper = h - max(c, o)
    lower = min(c, o) - l
    if body / rng > body_ratio:
        return False
    return (upper >= wick_ratio * body) or (lower >= wick_ratio * body)

def candle_hit(o, h, l, c) -> bool:
    # сейчас учитываем пин-бар/длинный фитиль
    return is_pinbar(o, h, l, c)

# =========================================================
#                       ОЦЕНКА СИМВОЛА
# =========================================================
def make_key(symbol: str, tf: str, bar_open_ms: int, kind: str):
    return f"{symbol}|{tf}|{bar_open_ms}|{kind}"

def evaluate_symbol(symbol: str):
    k4 = bybit_klines(symbol, TF_4H, limit=DEM_LEN+50)
    kd = bybit_klines(symbol, TF_1D, limit=DEM_LEN+50)
    if len(k4) < DEM_LEN+2 or len(kd) < DEM_LEN+2:
        return

    # 4H
    o4, h4, l4, c4 = k4[-1][1], k4[-1][2], k4[-1][3], k4[-1][4]
    closes4 = [x[4] for x in k4]; highs4 = [x[2] for x in k4]; lows4 = [x[3] for x in k4]
    dem4 = calc_demarker(closes4, highs4, lows4, DEM_LEN); dem4_last = dem4[-1]

    # 1D
    od, hd, ld, cd = kd[-1][1], kd[-1][2], kd[-1][3], kd[-1][4]
    closesd = [x[4] for x in kd]; highsd = [x[2] for x in kd]; lowsd = [x[3] for x in kd]
    demd = calc_demarker(closesd, highsd, lowsd, DEM_LEN); demd_last = demd[-1]

    # Сигналы DeMarker
    sig4 = "🟢⬆️" if dem4_last <= OS else ("🔴⬇️" if dem4_last >= OB else "")
    sigd = "🟢⬆️" if demd_last <= OS else ("🔴⬇️" if demd_last >= OB else "")

    # Свеча
    cndl4 = "🕯️" if candle_hit(o4, h4, l4, c4) else ""
    cndld = "🕯️" if candle_hit(od, hd, ld, cd) else ""

    # ⚡ — если 4H и 1D в одной зоне
    lightning = ""
    if (dem4_last >= OB and demd_last >= OB) or (dem4_last <= OS and demd_last <= OS):
        lightning = "⚡"

    # правило «минимум два сигнала» (на каждом TF отдельно)
    candidates = []
    pack4 = [x for x in [sig4, cndl4] if x]
    if len(pack4) >= 2:
        candidates.append(("4H", "".join(pack4)))
    packd = [x for x in [sigd, cndld] if x]
    if len(packd) >= 2:
        candidates.append(("1D", "".join(packd)))
    if lightning:
        candidates.append(("⚡", lightning))

    # отправка с дедупом
    state = load_state()
    changed = False
    for tf, tokens in candidates:
        bar_time = int(k4[-1][0]) if tf in ("4H", "⚡") else int(kd[-1][0])
        key = make_key(symbol, tf, bar_time, tokens)
        if key not in state:
            # только символы и эмодзи
            msg = f"{symbol} {tokens}"
            if tg_send(msg):
                state[key] = int(time.time())
                changed = True
    if changed:
        save_state(state)

# =========================================================
#                           MAIN
# =========================================================
def main():
    # Автоподбор 49 перпов с биржи — без изменения ENV
    try:
        symbols = fetch_symbols_from_bybit(49)
        if not symbols:
            symbols = ["BTCUSDT", "ETHUSDT", "PAXGUSDT"]  # фолбэк
        logging.info("Symbols (%d): %s", len(symbols), ",".join(symbols))
    except Exception as e:
        logging.error(f"Symbol fetch failed: {e}")
        symbols = ["BTCUSDT", "ETHUSDT", "PAXGUSDT"]

    logging.info("✅ Bot started. Polling every %s seconds.", POLL_SECONDS)
    while True:
        try:
            for s in symbols:
                evaluate_symbol(s)
                time.sleep(1)  # лёгкий троттлинг между тикерами
        except Exception as e:
            logging.error(f"Main loop error: {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()