# bot.py
import os, time, json, logging
from typing import List, Dict, Tuple
import requests

# ==================== НАСТРОЙКИ ====================
# Telegram
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# Период опроса (сек) – один цикл по всем тикерам
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN = int(os.getenv("DEM_LEN", "28"))
DEM_OB = float(os.getenv("DEM_OB", "0.70"))  # overbought
DEM_OS = float(os.getenv("DEM_OS", "0.30"))  # oversold

# Файл для дедупликации сигналов (чтобы один и тот же бар не слать дважды)
STATE_PATH = os.getenv("STATE_PATH", "state.json")

# Bybit v5 kline endpoint (ЖЁСТКО зафиксирован — без ENV)
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
REQ_HEADERS = {
    "User-Agent": "demarker-telegram-bot/1.0",
    "Accept": "application/json",
}

# ==== ТОЛЬКО РЕАЛЬНЫЕ Bybit USDT-PERP (category=linear) ====
SYMBOLS = [
    "BYBIT:BTCUSDT", "BYBIT:ETHUSDT", "BYBIT:BNBUSDT", "BYBIT:SOLUSDT",
    "BYBIT:XRPUSDT", "BYBIT:DOGEUSDT", "BYBIT:ADAUSDT", "BYBIT:AVAXUSDT",
    "BYBIT:MATICUSDT", "BYBIT:DOTUSDT", "BYBIT:LINKUSDT", "BYBIT:TRXUSDT",
    "BYBIT:LTCUSDT", "BYBIT:UNIUSDT", "BYBIT:ATOMUSDT", "BYBIT:NEARUSDT",
    "BYBIT:APTUSDT", "BYBIT:OPUSDT",  "BYBIT:ARBUSDT",  "BYBIT:INJUSDT",
]

# Ровно эти таймфреймы
INTERVALS = {"4H": 240, "1D": "D"}

# ==================== УТИЛИТЫ ====================
def drop_prefix(sym: str) -> str:
    # "BYBIT:BTCUSDT" -> "BTCUSDT"
    return sym.split(":", 1)[1] if ":" in sym else sym

def load_state() -> Dict[str, int]:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(st: Dict[str, int]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TELEGRAM creds missing; message skipped: %s", text)
        return
    try:
        requests.post(
            TG_API,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
                "disable_notification": True,
            },
            timeout=10,
        )
    except Exception as e:
        logging.exception("Telegram error: %s", e)

# ==================== ЗАПРОСЫ К BYBIT ====================
def bybit_kline(symbol: str, interval, limit: int = 300) -> List[Dict]:
    """
    Возвращает список OHLCV. Последний элемент — текущая формирующаяся свеча,
    поэтому для сигналов используем ПРЕДЫДУЩУЮ (индекс -2).
    """
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    }
    r = requests.get(BYBIT_URL, params=params, headers=REQ_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
    raw = data.get("result", {}).get("list", [])
    rows = sorted(raw, key=lambda x: int(x[0]))  # по времени возрастания
    out = []
    for row in rows:
        out.append({
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]) if len(row) > 5 else 0.0,
        })
    return out

# ==================== ИНДИКАТОРЫ ====================
def sma(series: List[float], length: int) -> List[float]:
    out, s = [], 0.0
    for i, x in enumerate(series):
        s += x
        if i >= length:
            s -= series[i - length]
        out.append(s / length if i >= length - 1 else float("nan"))
    return out

def demarker_from_hl(hl: List[Tuple[float, float]], length: int) -> List[float]:
    """
    DeMarker = SMA(DEMmax, len) / (SMA(DEMmax,len) + SMA(DEMmin,len))
    DEMmax = max(high - prev_high, 0)
    DEMmin = max(prev_low - low, 0)
    """
    demax, demin = [], []
    for i in range(len(hl)):
        if i == 0:
            demax.append(0.0)
            demin.append(0.0)
        else:
            up = max(hl[i][0] - hl[i - 1][0], 0.0)
            dn = max(hl[i - 1][1] - hl[i][1], 0.0)
            demax.append(up)
            demin.append(dn)
    smax = sma(demax, length)
    smin = sma(demin, length)
    res = []
    for i in range(len(hl)):
        den = smax[i] + smin[i]
        res.append(smax[i] / den if den > 0 else 0.5)
    return res

# ==================== ПАТТЕРНЫ СВЕЧЕЙ ====================
def candle_parts(o, h, l, c):
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    red = c < o
    green = c > o
    return body, upper, lower, red, green

def detect_patterns(ohlc: List[Dict]) -> Dict[str, bool]:
    """
    Возвращает флаги бычьих/медвежьих паттернов на последней ЗАКРЫТОЙ свече.
    Нужны минимум 3 свечи.
    """
    if len(ohlc) < 3:
        return {"bull": False, "bear": False, "red": False, "green": False}

    a = ohlc[-3]  # предыдущая к закрытой
    b = ohlc[-2]  # закрытая (по ней решаем)

    body_b, upper_b, lower_b, red_b, green_b = candle_parts(b["o"], b["h"], b["l"], b["c"])
    body_a, _, _, red_a, green_a = candle_parts(a["o"], a["h"], a["l"], a["c"])

    # средний размер тела за 10 последних закрытых
    bodies = [abs(x["c"] - x["o"]) for x in ohlc[-11:-1]]
    avg_body = sum(bodies) / len(bodies) if bodies else 0.0
    small_body = (avg_body > 0) and (body_b <= 0.6 * avg_body)

    # Bullish Engulfing
    bull_engulf = green_b and red_a and (b["o"] <= a["c"]) and (b["c"] >= a["o"])
    # Hammer
    hammer = (lower_b >= 2.0 * body_b) and (upper_b <= 0.25 * body_b)
    # Morning Star (упрощённо)
    morning_star = red_a and small_body and green_b and (b["c"] >= (a["o"] + a["c"]) / 2)

    # Bearish Engulfing
    bear_engulf = red_b and green_a and (b["o"] >= a["c"]) and (b["c"] <= a["o"])
    # Shooting Star
    shooting = (upper_b >= 2.0 * body_b) and (lower_b <= 0.25 * body_b)
    # Evening Star
    evening_star = green_a and small_body and red_b and (b["c"] <= (a["o"] + a["c"]) / 2)

    bull = bull_engulf or hammer or morning_star
    bear = bear_engulf or shooting or evening_star
    return {"bull": bull, "bear": bear, "red": red_b, "green": green_b}

# ==================== СИГНАЛЫ ====================
def last_closed_action(ohlc: List[Dict], dem: List[float]) -> Tuple[str, int]:
    """
    Возвращает ('buy'|'sell'|'' , ts_closed_bar)
    BUY:  DeM < OS  и бычий паттерн (и зелёная свеча)
    SELL: DeM > OB  и медвежий паттерн (и красная свеча)
    """
    if len(ohlc) < 3 or len(dem) < 2:
        return "", 0

    i = len(ohlc) - 2  # закрытая
    t = ohlc[i]["t"]
    flags = detect_patterns(ohlc)
    dval = dem[i]

    is_buy = (dval < DEM_OS) and flags["green"] and flags["bull"]
    is_sell = (dval > DEM_OB) and flags["red"] and flags["bear"]

    if is_buy:
        return "buy", t
    if is_sell:
        return "sell", t
    return "", 0

def both_timeframes_zone(sym_api: str) -> Tuple[bool, bool]:
    """True/True если DeMarker на закрытых барах одновременно в зонах на 4H и 1D."""
    k4 = bybit_kline(sym_api, INTERVALS["4H"], limit=DEM_LEN + 10)
    d4 = demarker_from_hl([(x["h"], x["l"]) for x in k4], DEM_LEN)
    i4 = len(k4) - 2 if len(k4) >= 2 else -1

    k1 = bybit_kline(sym_api, INTERVALS["1D"], limit=DEM_LEN + 10)
    d1 = demarker_from_hl([(x["h"], x["l"]) for x in k1], DEM_LEN)
    i1 = len(k1) - 2 if len(k1) >= 2 else -1

    if i4 < 0 or i1 < 0:
        return False, False

    return (d4[i4] < DEM_OS and d1[i1] < DEM_OS,
            d4[i4] > DEM_OB and d1[i1] > DEM_OB)

def fmt_message(ticker: str, action: str, double_flag: bool) -> str:
    # Эмодзи как просил: зелёная/красная стрелка + ⚡ при двойном совпадении
    base = "🟢⬆️" if action == "buy" else "🔴⬇️"
    return f"{base} {ticker}" + (" ⚡" if double_flag else "")

def process_symbol(sym_tv: str, state: Dict[str, int]) -> None:
    sym_api = drop_prefix(sym_tv)

    # 4H
    k4 = bybit_kline(sym_api, INTERVALS["4H"], limit=DEM_LEN + 100)
    dem4 = demarker_from_hl([(x["h"], x["l"]) for x in k4], DEM_LEN)
    act4, ts4 = last_closed_action(k4, dem4)

    # 1D
    k1 = bybit_kline(sym_api, INTERVALS["1D"], limit=DEM_LEN + 100)
    dem1 = demarker_from_hl([(x["h"], x["l"]) for x in k1], DEM_LEN)
    act1, ts1 = last_closed_action(k1, dem1)

    dbl_buy, dbl_sell = both_timeframes_zone(sym_api)

    for tf, action, ts in (("4H", act4, ts4), ("1D", act1, ts1)):
        if not action:
            continue
        key = f"{sym_tv}|{tf}|{action}"
        if ts > state.get(key, 0):  # новый закрытый бар для этого сигнала
            double_flag = (dbl_buy and action == "buy") or (dbl_sell and action == "sell")
            send_telegram(fmt_message(sym_tv, action, double_flag))
            state[key] = ts

# ==================== MAIN ====================
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load_state()
    logging.info("Demarker bot started. Tickers=%d", len(SYMBOLS))
    while True:
        start = time.time()
        for sym in SYMBOLS:
            try:
                process_symbol(sym, state)
            except Exception as e:
                logging.warning("Symbol %s error: %s", sym, e)
        save_state(state)
        time.sleep(max(0.0, POLL_SECONDS - (time.time() - start)))

if __name__ == "__main__":
    main()