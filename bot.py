# bot.py
import os, time, json, logging, requests
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

BYBIT_KLINE_URL = os.getenv("BYBIT_URL", "https://api.bybit.com/v5/market/kline")
BYBIT_INSTR_URL = "https://api.bybit.com/v5/market/instruments-info"

MAX_TICKERS    = int(os.getenv("MAX_TICKERS", "40"))
REQ_SLEEP_SEC  = float(os.getenv("REQ_SLEEP_SEC", "0.15"))

INTERVALS = {"4H": "240", "1D": "D"}

# ---------------------- IO ----------------------
def load_state() -> Dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_state(st: Dict) -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except:
        pass

# ---------------------- BYBIT ----------------------
def fetch_linear_usdt_symbols() -> List[str]:
    params = {"category": "linear"}
    r = requests.get(BYBIT_INSTR_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("result", {}).get("list", [])
    out = []
    for it in items:
        if (it.get("status") == "Trading"
            and it.get("quoteCoin") == "USDT"
            and it.get("contractType") in ("LinearPerpetual", "LinearPerpetualV2", "LinearFutures")):
            out.append(it["symbol"])
    return out[:MAX_TICKERS]

def bybit_kline(symbol: str, interval: str, limit: int = 300) -> List[Dict]:
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    rows = sorted(data.get("result", {}).get("list", []), key=lambda x: int(x[0]))
    return [{"t": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])} for row in rows]

# ---------------------- TA ----------------------
def sma(series: List[float], length: int) -> List[float]:
    out, s = [], 0.0
    for i, x in enumerate(series):
        s += x
        if i >= length:
            s -= series[i - length]
        out.append(s / length if i >= length - 1 else float("nan"))
    return out

def demarker(hl: List[Tuple[float,float]], length: int) -> List[float]:
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

# ---------------------- СВЕЧНЫЕ ПАТТЕРНЫ ----------------------
def detect_patterns(ohlc: List[Dict]) -> Tuple[bool,bool,bool]:
    # возвращаем (bull, bear, candle_flag) — candle_flag=True если найден любой из разворотных паттернов
    if len(ohlc) < 3:
        return False, False, False
    a = ohlc[-3]
    b = ohlc[-2]
    body = abs(b["c"] - b["o"])
    upper = b["h"] - max(b["o"], b["c"])
    lower = min(b["o"], b["c"]) - b["l"]
    red = b["c"] < b["o"]
    green = b["c"] > b["o"]

    prev_red = a["c"] < a["o"]
    prev_green = a["c"] > a["o"]

    bull_engulf = green and prev_red and b["o"] <= a["c"] and b["c"] >= a["o"]
    hammer = lower >= 2 * body and upper <= 0.25 * body
    morning_star = prev_red and green and b["c"] >= (a["o"] + a["c"]) / 2

    bear_engulf = red and prev_green and b["o"] >= a["c"] and b["c"] <= a["o"]
    shooting = upper >= 2 * body and lower <= 0.25 * body
    evening_star = prev_green and red and b["c"] <= (a["o"] + a["c"]) / 2

    bull = bull_engulf or hammer or morning_star
    bear = bear_engulf or shooting or evening_star
    candle_flag = bull or bear or (body <= 0.1 * max(b["h"] - b["l"], 1e-12))  # до́жи тоже считаем флажком

    return bull, bear, candle_flag

# ---------------------- СИГНАЛЫ ----------------------
def last_closed_action(ohlc: List[Dict], dem: List[float]) -> Tuple[str, int, bool]:
    if len(ohlc) < 3 or len(dem) < 2:
        return "", 0, False
    i = len(ohlc) - 2
    t = ohlc[i]["t"]
    bull, bear, candle_flag = detect_patterns(ohlc)
    dval = dem[i]
    is_buy = (dval < OS) and (ohlc[-2]["c"] > ohlc[-2]["o"]) and bull
    is_sell = (dval > OB) and (ohlc[-2]["c"] < ohlc[-2]["o"]) and bear
    if is_buy:  return "buy", t, candle_flag
    if is_sell: return "sell", t, candle_flag
    return "", 0, candle_flag

def both_timeframes_zone(sym: str) -> Tuple[bool,bool]:
    k4 = bybit_kline(sym, INTERVALS["4H"], limit=DEM_LEN+10); time.sleep(REQ_SLEEP_SEC)
    d4 = demarker([(x["h"], x["l"]) for x in k4], DEM_LEN)
    i4 = len(k4) - 2 if len(k4) >= 2 else -1

    k1 = bybit_kline(sym, INTERVALS["1D"], limit=DEM_LEN+10); time.sleep(REQ_SLEEP_SEC)
    d1 = demarker([(x["h"], x["l"]) for x in k1], DEM_LEN)
    i1 = len(k1) - 2 if len(k1) >= 2 else -1

    if i4 < 0 or i1 < 0: return False, False
    return (d4[i4] < OS and d1[i1] < OS, d4[i1] > OB and d1[i1] > OB)  # исправлено: индекс для 1D

def fmt_msg(symbol: str, side: str, dbl: bool, candle: bool) -> str:
    arrow = "🟢⬆️" if side == "buy" else "🔴⬇️"
    s = f"{arrow} {symbol}"
    if candle: s += " 🕯️"
    if dbl:    s += " ⚡"
    return s

def process_symbol(sym: str, state: Dict) -> None:
    # 4H
    k4 = bybit_kline(sym, INTERVALS["4H"], limit=DEM_LEN+100); time.sleep(REQ_SLEEP_SEC)
    d4 = demarker([(x["h"], x["l"]) for x in k4], DEM_LEN)
    a4, t4, c4 = last_closed_action(k4, d4)

    # 1D
    k1 = bybit_kline(sym, INTERVALS["1D"], limit=DEM_LEN+100); time.sleep(REQ_SLEEP_SEC)
    d1 = demarker([(x["h"], x["l"]) for x in k1], DEM_LEN)
    a1, t1, c1 = last_closed_action(k1, d1)

    dbl_buy, dbl_sell = both_timeframes_zone(sym)

    # если оба ТФ совпали — одно сообщение (стрелка + тикер + 🕯️ если есть на любом ТФ) + ⚡
    if a4 and a1 and a4 == a1:
        act = a1; ts = max(t1, t4)
        key = f"{sym}|BOTH|{act}"
        if ts > state.get(key, 0):
            dbl = (dbl_buy and act == "buy") or (dbl_sell and act == "sell")
            candle = c4 or c1
            send_telegram(fmt_msg(sym, act, dbl, candle))
            state[key] = ts
        return

    # иначе — отдельно по каждому ТФ (без упоминания ТФ в тексте)
    for act, ts, candle in ((a4, t4, c4), (a1, t1, c1)):
        if not act: continue
        key = f"{sym}|SINGLE|{act}"
        if ts > state.get(key, 0):
            # dbl-флажок ставим, если зоны совпали, и это тот же акт
            dbl = (dbl_buy and act == "buy") or (dbl_sell and act == "sell")
            send_telegram(fmt_msg(sym, act, dbl, candle))
            state[key] = ts

# ---------------------- MAIN ----------------------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load_state()
    symbols = fetch_linear_usdt_symbols()
    while True:
        start = time.time()
        for sym in symbols:
            try:
                process_symbol(sym, state)
            except Exception:
                pass
        save_state(state)
        time.sleep(max(0.0, POLL_SECONDS - (time.time() - start)))

if __name__ == "__main__":
    main()