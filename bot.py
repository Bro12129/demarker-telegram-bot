# bot.py
import os, time, json, logging, requests
from typing import List, Dict, Tuple

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

# Bybit v5
BYBIT_KLINE_URL = os.getenv("BYBIT_URL", "https://api.bybit.com/v5/market/kline")
BYBIT_INSTR_URL = "https://api.bybit.com/v5/market/instruments-info"

# Ограничитель — чтобы не ловить 429 на слабых инстансах
MAX_TICKERS    = int(os.getenv("MAX_TICKERS", "40"))
REQ_SLEEP_SEC  = float(os.getenv("REQ_SLEEP_SEC", "0.15"))  # пауза между HTTP-запросами

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
    except Exception as e:
        logging.warning("save_state error: %s", e)

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_* env missing; skipped: %s", text)
        return
    try:
        requests.post(TG_API, json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": True
        }, timeout=10)
    except Exception as e:
        logging.warning("Telegram send error: %s", e)

# ---------------------- BYBIT ----------------------
def fetch_linear_usdt_symbols() -> List[str]:
    params = {"category": "linear"}
    r = requests.get(BYBIT_INSTR_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"instruments-info retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
    items = data.get("result", {}).get("list", [])

    out = []
    for it in items:
        if (it.get("status") == "Trading"
            and it.get("quoteCoin") == "USDT"
            and it.get("contractType") in ("LinearPerpetual", "LinearPerpetualV2", "LinearFutures")):
            out.append(it["symbol"])

    priority = {"BTCUSDT":0,"ETHUSDT":1,"XAUUSDT":2,"XAGUSDT":3,"BNBUSDT":4,"SOLUSDT":5}
    out.sort(key=lambda s: (priority.get(s, 9999), s))
    return out[:MAX_TICKERS]

def bybit_kline(symbol: str, interval: str, limit: int = 300) -> List[Dict]:
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"kline retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
    raw = data.get("result", {}).get("list", [])
    rows = sorted(raw, key=lambda x: int(x[0]))
    return [{
        "t": int(row[0]),
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[5]) if len(row) > 5 else 0.0
    } for row in rows]

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

# ---------------------- ДОПОЛНИТЕЛЬНЫЕ СВЕЧНЫЕ ПАТТЕРНЫ ----------------------
def detect_additional_patterns(ohlc: List[Dict]) -> Dict[str,bool]:
    if len(ohlc) < 3:
        return {}
    a, b, c = ohlc[-3], ohlc[-2], ohlc[-1]
    def body(o, c): return abs(c - o)
    def upper(o, h, c): return h - max(o, c)
    def lower(o, l, c): return min(o, c) - l

    o, h, l, c = b["o"], b["h"], b["l"], b["c"]
    prev_o, prev_c = a["o"], a["c"]
    body_b = body(o, c)
    upper_b = upper(o, h, c)
    lower_b = lower(o, l, c)

    avg_body = sum(abs(x["c"] - x["o"]) for x in ohlc[-11:-1]) / 10

    hammer = lower_b > 2 * body_b and upper_b < body_b * 0.3
    hanging_man = hammer and c < o
    shooting_star = upper_b > 2 * body_b and lower_b < body_b * 0.3
    doji = body_b <= 0.1 * (h - l)
    bullish_engulf = c > o and prev_c < prev_o and o <= prev_c and c >= prev_o
    bearish_engulf = c < o and prev_c > prev_o and o >= prev_c and c <= prev_o
    morning_star = prev_c < prev_o and doji and c > o and c > (prev_o + prev_c) / 2
    evening_star = prev_c > prev_o and doji and c < o and c < (prev_o + prev_c) / 2

    return {
        "hammer": hammer,
        "hanging_man": hanging_man,
        "shooting_star": shooting_star,
        "doji": doji,
        "bullish_engulf": bullish_engulf,
        "bearish_engulf": bearish_engulf,
        "morning_star": morning_star,
        "evening_star": evening_star
    }

# ---------------------- ПАТТЕРНЫ ----------------------
def candle_parts(o,h,l,c):
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    red = c < o
    green = c > o
    return body, upper, lower, red, green

def detect_patterns(ohlc: List[Dict]) -> Dict[str,bool]:
    if len(ohlc) < 3:
        return {"bull": False, "bear": False, "red": False, "green": False}
    a = ohlc[-3]
    b = ohlc[-2]
    body_b, upper_b, lower_b, red_b, green_b = candle_parts(b["o"], b["h"], b["l"], b["c"])

    bodies = [abs(x["c"] - x["o"]) for x in ohlc[-11:-1]]
    avg_body = sum(bodies)/len(bodies) if bodies else 0.0
    small_body = (avg_body > 0) and (body_b <= 0.6 * avg_body)

    prev_red = (a["c"] < a["o"])
    prev_green = (a["c"] > a["o"])

    bull_engulf  = green_b and prev_red   and (b["o"] <= a["c"]) and (b["c"] >= a["o"])
    hammer       = (lower_b >= 2.0 * body_b) and (upper_b <= 0.25 * body_b)
    morning_star = prev_red and small_body and green_b and (b["c"] >= (a["o"] + a["c"]) / 2)

    bear_engulf  = red_b and prev_green    and (b["o"] >= a["c"]) and (b["c"] <= a["o"])
    shooting     = (upper_b >= 2.0 * body_b) and (lower_b <= 0.25 * body_b)
    evening_star = prev_green and small_body and red_b and (b["c"] <= (a["o"] + a["c"]) / 2)

    bull = bull_engulf or hammer or morning_star
    bear = bear_engulf or shooting or evening_star

    # просто добавляем обнаруженные паттерны (внутренне)
    _extra = detect_additional_patterns(ohlc)

    return {"bull": bull, "bear": bear, "red": red_b, "green": green_b, **_extra}

# ---------------------- СИГНАЛЫ ----------------------
def last_closed_action(ohlc: List[Dict], dem: List[float]) -> Tuple[str, int]:
    if len(ohlc) < 3 or len(dem) < 2:
        return "", 0
    i = len(ohlc) - 2
    t = ohlc[i]["t"]
    flags = detect_patterns(ohlc)
    dval = dem[i]
    is_buy  = (dval < OS) and flags["green"] and flags["bull"]
    is_sell = (dval > OB) and flags["red"]   and flags["bear"]
    if is_buy:  return "buy", t
    if is_sell: return "sell", t
    return "", 0

def both_timeframes_zone(sym: str) -> Tuple[bool,bool]:
    k4 = bybit_kline(sym, INTERVALS["4H"], limit=DEM_LEN+10); time.sleep(REQ_SLEEP_SEC)
    d4 = demarker([(x["h"], x["l"]) for x in k4], DEM_LEN)
    i4 = len(k4) - 2 if len(k4) >= 2 else -1

    k1 = bybit_kline(sym, INTERVALS["1D"], limit=DEM_LEN+10); time.sleep(REQ_SLEEP_SEC)
    d1 = demarker([(x["h"], x["l"]) for x in k1], DEM_LEN)
    i1 = len(k1) - 2 if len(k1) >= 2 else -1

    if i4 < 0 or i1 < 0: return False, False
    return (d4[i4] < OS and d1[i1] < OS, d4[i4] > OB and d1[i1] > OB)

def msg(ticker: str, action: str, tf: str, dbl: bool) -> str:
    base = "🟢⬆️" if action == "buy" else "🔴⬇️"
    return f"{base} {ticker} @{tf}" + (" ⚡" if dbl else "")

def process_symbol(sym: str, state: Dict) -> None:
    k4 = bybit_kline(sym, INTERVALS["4H"], limit=DEM_LEN+100); time.sleep(REQ_SLEEP_SEC)
    d4 = demarker([(x["h"], x["l"]) for x in k4], DEM_LEN)
    a4, t4 = last_closed_action(k4, d4)

    k1 = bybit_kline(sym, INTERVALS["1D"], limit=DEM_LEN+100); time.sleep(REQ_SLEEP_SEC)
    d1 = demarker([(x["h"], x["l"]) for x in k1], DEM_LEN)
    a1, t1 = last_closed_action(k1, d1)

    dbl_buy, dbl_sell = both_timeframes_zone(sym)

    if a4 and a1 and a4 == a1:
        act = a1; ts = max(t1, t4)
        key = f"{sym}|BOTH|{act}"
        if ts > state.get(key, 0):
            dbl = (dbl_buy and act == "buy") or (dbl_sell and act == "sell")
            send_telegram(msg(sym, act, "1D", dbl))
            state[key] = ts
        return

    for tf, act, ts in (("4H", a4, t4), ("1D", a1, t1)):
        if not act: continue
        key = f"{sym}|{tf}|{act}"
        if ts > state.get(key, 0):
            dbl = (dbl_buy and act == "buy") or (dbl_sell and act == "sell")
            send_telegram(msg(sym, act, tf, dbl))
            state[key] = ts

# ---------------------- MAIN ----------------------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load_state()
    symbols = fetch_linear_usdt_symbols()
    logging.info("Linear USDT symbols loaded: %d", len(symbols))
    while True:
        start = time.time()
        for sym in symbols:
            try:
                process_symbol(sym, state)
            except Exception as e:
                logging.warning("Symbol %s error: %s", sym, e)
        save_state(state)
        time.sleep(max(0.0, POLL_SECONDS - (time.time() - start)))

if __name__ == "__main__":
    main()