import os, time, json, logging
from typing import List, Dict, Tuple
import requests

# ---------------------- НАСТРОЙКИ ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# Частота цикла (сек)
POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))
OS             = float(os.getenv("DEM_OS", "0.30"))

# Путь для локального состояния (дедуп сигналов)
STATE_PATH     = os.getenv("STATE_PATH", "state.json")

# Bybit v5 kline endpoint
BYBIT_URL      = "https://api.bybit.com/v5/market/kline"

# -------- ТОЛЬКО BYBIT PERP/DERIVATIVES (~30 тикеров) --------
SYMBOLS = [
    # Металл / Доллар / Индексы США (Bybit деривативы/индексные контракты)
    "BYBIT:XAUTUSDT", "BYBIT:XAUUSDT",
    "BYBIT:DXYUSDT", "BYBIT:USDXUSDT",
    "BYBIT:US500", "BYBIT:US100", "BYBIT:US30", "BYBIT:US2000",
    "BYBIT:SPXUSDT", "BYBIT:NDXUSDT", "BYBIT:DJIUSDT",

    # Топ монеты/альты (perp)
    "BYBIT:BTCUSDT", "BYBIT:ETHUSDT", "BYBIT:BNBUSDT", "BYBIT:SOLUSDT",
    "BYBIT:XRPUSDT", "BYBIT:DOGEUSDT", "BYBIT:ADAUSDT", "BYBIT:AVAXUSDT",
    "BYBIT:MATICUSDT", "BYBIT:DOTUSDT", "BYBIT:LINKUSDT", "BYBIT:TRXUSDT",
    "BYBIT:LTCUSDT", "BYBIT:UNIUSDT", "BYBIT:ATOMUSDT", "BYBIT:NEARUSDT",
    "BYBIT:APTUSDT", "BYBIT:OPUSDT", "BYBIT:ARBUSDT", "BYBIT:INJUSDT",
]

# интервалы Bybit API
INTERVALS = {"4H": 240, "1D": "D"}

# ---------------------- УТИЛИТЫ ----------------------
def drop_prefix(sym: str) -> str:
    # "BYBIT:BTCUSDT" -> "BTCUSDT"
    return sym.split(":", 1)[1] if ":" in sym else sym

def load_state() -> Dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_state(st: Dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_* env not set; skipped: %s", text)
        return
    try:
        requests.post(TG_API, json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": True
        }, timeout=10)
    except Exception as e:
        logging.exception("Telegram send error: %s", e)

def bybit_kline(symbol: str, interval, limit: int = 300) -> List[Dict]:
    """
    Возвращает список свечей (последняя — текущая формирующаяся).
    Для сигналов используем ПРЕДЫДУЩУЮ (закрытую) свечу.
    """
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(BYBIT_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
    raw = data.get("result", {}).get("list", [])
    rows = sorted(raw, key=lambda x: int(x[0]))  # по времени возр.
    out = []
    for row in rows:
        out.append({
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]) if len(row) > 5 else 0.0
        })
    return out

def sma(series: List[float], length: int) -> List[float]:
    out, s = [], 0.0
    for i, x in enumerate(series):
        s += x
        if i >= length:
            s -= series[i - length]
        out.append(s / length if i >= length - 1 else float("nan"))
    return out

def demarker(hl: List[Tuple[float,float]], length: int) -> List[float]:
    """
    DeMarker = SMA(DEMmax, len) / (SMA(DEMmax,len) + SMA(DEMmin,len))
    DEMmax = max(high - high[1], 0), DEMmin = max(low[1] - low, 0)
    """
    demax, demin = [], []
    for i in range(len(hl)):
        if i == 0:
            demax.append(0.0); demin.append(0.0)
        else:
            up = max(hl[i][0] - hl[i-1][0], 0.0)
            dn = max(hl[i-1][1] - hl[i][1], 0.0)
            demax.append(up); demin.append(dn)
    smax = sma(demax, length); smin = sma(demin, length)
    res = []
    for i in range(len(hl)):
        den = smax[i] + smin[i]
        res.append(smax[i]/den if den > 0 else 0.5)
    return res

# ---------------------- ПАТТЕРНЫ ----------------------
def candle_flags(o,h,l,c):
    red = c < o
    grn = c > o
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return red, grn, body, upper, lower

def detect_patterns(ohlc: List[Dict]) -> Dict[str,bool]:
    """
    Флаги бычьих/медвежьих паттернов на ПОСЛЕДНЕЙ ЗАКРЫТОЙ.
    Нужны минимум 3 свечи.
    """
    n = len(ohlc)
    if n < 3:
        return dict(bull=False, bear=False, red=False, grn=False)

    a = ohlc[-3]  # предыдущая
    b = ohlc[-2]  # последняя закрытая

    red, grn, body, upper, lower = candle_flags(b["o"], b["h"], b["l"], b["c"])

    last_bodies = [abs(x["c"] - x["o"]) for x in ohlc[-11:-1]]
    avg_body10 = sum(last_bodies)/len(last_bodies) if last_bodies else 0.0
    small_body = body <= avg_body10 * 0.6 if avg_body10 > 0 else False

    # Bullish Engulfing
    prev_red = (a["c"] < a["o"])
    bull_engulf  = grn and prev_red and (b["o"] <= a["c"]) and (b["c"] >= a["o"])

    # Hammer
    hammer       = lower >= 2.0 * body and upper <= 0.25 * body

    # Morning Star (упрощ.)
    morning_star = prev_red and small_body and grn and (b["c"] >= (a["o"] + a["c"]) / 2)

    # Bearish Engulfing
    prev_grn = (a["c"] > a["o"])
    bear_engulf  = red and prev_grn and (b["o"] >= a["c"]) and (b["c"] <= a["o"])

    # Shooting Star
    shooting     = upper >= 2.0 * body and lower <= 0.25 * body

    # Evening Star
    evening_star = prev_grn and small_body and red and (b["c"] <= (a["o"] + a["c"]) / 2)

    bull = bull_engulf or hammer or morning_star
    bear = bear_engulf or shooting or evening_star
    return dict(bull=bull, bear=bear, red=red, grn=grn)

# ---------------------- СИГНАЛЫ ----------------------
def last_closed_signal(ohlc: List[Dict], dem: List[float]) -> Tuple[str, int]:
    """
    ('buy'|'sell'|'' , ts_closed_bar)
      BUY:  DeM<OS  & зелёная свеча & бычий паттерн
      SELL: DeM>OB  & красная свеча & медвежий паттерн
    """
    if len(ohlc) < 3 or len(dem) < 2:
        return "", 0

    i = len(ohlc) - 2  # индекс последней закрытой
    o,h,l,c,t = ohlc[i]["o"], ohlc[i]["h"], ohlc[i]["l"], ohlc[i]["c"], ohlc[i]["t"]
    flags = detect_patterns(ohlc)
    dval = dem[i]

    is_buy  = (dval < OS) and flags["grn"] and flags["bull"]
    is_sell = (dval > OB) and flags["red"] and flags["bear"]

    if is_buy:  return "buy", t
    if is_sell: return "sell", t
    return "", 0

def check_double_signal(sym_api: str) -> Tuple[bool, bool]:
    """
    Возвращает (double_buy, double_sell) — DeMarker одновременно в зонах
    на 4H и 1D (по последней закрытой).
    """
    # 4H
    k4 = bybit_kline(sym_api, INTERVALS["4H"], limit=DEM_LEN+10)
    dem4 = demarker([(x["h"], x["l"]) for x in k4], DEM_LEN)
    i4 = len(k4) - 2 if len(k4) >= 2 else -1
    # 1D
    k1 = bybit_kline(sym_api, INTERVALS["1D"], limit=DEM_LEN+10)
    dem1 = demarker([(x["h"], x["l"]) for x in k1], DEM_LEN)
    i1 = len(k1) - 2 if len(k1) >= 2 else -1

    if i4 < 0 or i1 < 0:
        return (False, False)

    double_buy  = (dem4[i4] < OS) and (dem1[i1] < OS)
    double_sell = (dem4[i4] > OB) and (dem1[i1] > OB)
    return (double_buy, double_sell)

def fmt_message(ticker: str, action: str, double_flag: bool) -> str:
    base = "🟢⬆️" if action == "buy" else "🔴⬇️"
    return f"{base} {ticker}" + (" ⚡" if double_flag else "")

def process_symbol(sym_tv: str, state: Dict) -> None:
    """
    На каждый тикер:
      - считаем сигнал на 4H и 1D (по последней закрытой)
      - отправляем сообщение, если новый бар и выполнены условия
      - если оба ТФ дают один и тот же сигнал — добавляем ⚡
    """
    sym_api = drop_prefix(sym_tv)

    # 4H
    k4 = bybit_kline(sym_api, INTERVALS["4H"], limit=DEM_LEN+100)
    dem4 = demarker([(x["h"], x["l"]) for x in k4], DEM_LEN)
    act4, ts4 = last_closed_signal(k4, dem4)

    # 1D
    k1 = bybit_kline(sym_api, INTERVALS["1D"], limit=DEM_LEN+100)
    dem1 = demarker([(x["h"], x["l"]) for x in k1], DEM_LEN)
    act1, ts1 = last_closed_signal(k1, dem1)

    dbl_buy, dbl_sell = check_double_signal(sym_api)

    for tf, action, ts in (("4H", act4, ts4), ("1D", act1, ts1)):
        if not action:
            continue
        key = f"{sym_tv}|{tf}|{action}"
        last_ts = state.get(key, 0)
        if ts > last_ts:
            double_flag = (dbl_buy and action=="buy") or (dbl_sell and action=="sell")
            msg = fmt_message(sym_tv, action, double_flag)
            send_telegram(msg)
            state[key] = ts

# ---------------------- MAIN ----------------------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load_state()
    logging.info("Bot started. Tickers=%d", len(SYMBOLS))
    while True:
        start = time.time()
        for sym in SYMBOLS:
            try:
                process_symbol(sym, state)
            except Exception as e:
                logging.warning("Symbol %s error: %s", sym, e)
                continue
        save_state(state)
        sleep_left = max(0.0, POLL_SECONDS - (time.time() - start))
        time.sleep(sleep_left)

if __name__ == "__main__":
    main()