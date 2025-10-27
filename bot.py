# bot.py
# -*- coding: utf-8 -*-
import os, time, json, logging, requests, math
from typing import List, Tuple, Set

# ---------------------- ENV ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))
OS             = float(os.getenv("DEM_OS", "0.30"))

# Источник данных (Bybit v5)
BYBIT_BASE     = os.getenv("BYBIT_URL", "https://api.bybit.com")
CATEGORY       = os.getenv("BYBIT_CATEGORY", "linear")  # linear | inverse | spot
TICKERS        = [s.strip().upper() for s in os.getenv("TICKERS", "BTCUSDT").split(",") if s.strip()]
TIMEFRAMES     = [s.strip() for s in os.getenv("TIMEFRAMES", "240").split(",") if s.strip()]  # Bybit minutes: 1..1440,10080

KLINE_LIMIT    = int(os.getenv("KLINE_LIMIT", "200"))

# Подтверждения
CONFIRM_MODE   = os.getenv("CONFIRM_MODE", "any2").lower()  # any1 | any2
MESSAGE_MINIMAL = os.getenv("MESSAGE_MINIMAL", "true").lower() == "true"

# «Молния»: какие ТФ сверять (по умолчанию 4h и 1D)
LIGHTNING_FAST_TF = os.getenv("LIGHTNING_FAST_TF", "240")   # 4h
LIGHTNING_SLOW_TF = os.getenv("LIGHTNING_SLOW_TF", "1440")  # 1D

# Состояние (для дедупа)
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# ---------------------- LOGGING ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------------------- HELPERS ----------------------
def send_tg(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("No TELEGRAM_TOKEN or CHAT_ID; skip send.")
        return
    try:
        requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_state(state: dict):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"State save error: {e}")

def bybit_kline(symbol: str, interval: str, limit: int = 200):
    """
    Bybit v5 Kline: /v5/market/kline?category=linear&symbol=BTCUSDT&interval=240&limit=200
    Returns arrays of [open, high, low, close] floats (oldest -> newest)
    """
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data.get('retMsg')}")
    rows = data["result"]["list"]
    rows.sort(key=lambda x: int(x[0]))  # ensure oldest->newest
    o,h,l,c = [],[],[],[]
    for it in rows:
        o.append(float(it[1])); h.append(float(it[2])); l.append(float(it[3])); c.append(float(it[4]))
    return o,h,l,c

# ---------------------- INDICATORS ----------------------
def demarker(high: List[float], low: List[float], length: int) -> List[float]:
    demax, demin = [0.0]*len(high), [0.0]*len(low)
    for i in range(1, len(high)):
        demax[i] = max(high[i] - high[i-1], 0.0)
        demin[i] = max(low[i-1] - low[i], 0.0)
    out = [float("nan")] * len(high)
    for i in range(len(high)):
        if i < length:
            out[i] = float("nan")
        else:
            smax = sum(demax[i-length+1:i+1])
            smin = sum(demin[i-length+1:i+1])
            denom = smax + smin
            out[i] = smax / denom if denom > 0 else 0.5
    return out

def last_closed_dem(high: List[float], low: List[float], length: int) -> float:
    if len(high) < length + 2:  # нужно хотя бы length + 2 бара
        return float("nan")
    dem = demarker(high, low, length)
    return dem[-2]  # строго закрытая свеча

# ---------------------- CANDLE PATTERNS (включая фитильные) ----------------------
def _rng(h,l): return max(h-l, 1e-12)
def _body(o,c): return abs(c-o)
def _upper_wick(h,o,c): return max(h - max(o,c), 0.0)
def _lower_wick(l,o,c): return max(min(o,c) - l, 0.0)

# Пороговые параметры (ENV-переопределяемые)
MIN_BODY_PCT       = float(os.getenv("W_MIN_BODY_PCT", "0.05"))
SMALL_BODY_PCT     = float(os.getenv("W_SMALL_BODY_PCT","0.20"))
DOJI_BODY_PCT      = float(os.getenv("W_DOJI_BODY_PCT","0.05"))
LONG_WICK_RATIO    = float(os.getenv("W_LONG_WICK_RATIO","2.0"))
TINY_WICK_TO_RANGE = float(os.getenv("W_TINY_WICK_TO_RANGE","0.05"))

ENGULF_OVERLAP     = float(os.getenv("ENGULF_OVERLAP","0.10"))
NEED_SWING_WICKS   = os.getenv("W_NEED_SWING","false").lower()=="true"
SWING_LEN_WICKS    = int(os.getenv("W_SWING_LEN","2"))

def _swing_high(highs, i, L):
    # безопасные границы
    if i+L+1 > len(highs)-1 or i-L < 0: return True
    return all(highs[i] >= highs[i-k-1] for k in range(L)) and all(highs[i] > highs[i+k+1] for k in range(L))

def _swing_low(lows, i, L):
    if i+L+1 > len(lows)-1 or i-L < 0: return True
    return all(lows[i] <= lows[i-k-1] for k in range(L)) and all(lows[i] < lows[i+k+1] for k in range(L))

def detect_wick_patterns(o,h,l,c,i) -> Tuple[Set[str], Set[str], Set[str]]:
    O,H,L,C = o[i], h[i], l[i], c[i]
    rng     = _rng(H,L); body=_body(O,C)
    upw     = _upper_wick(H,O,C); loww=_lower_wick(L,O,C)
    bodyP   = body/rng; upwP=upw/rng; lowwP=loww/rng

    bull, bear, allp = set(), set(), set()

    # Doji + варианты
    if bodyP <= DOJI_BODY_PCT:
        allp.add("doji")
        if upwP >= 0.25 and lowwP >= 0.25: allp.add("doji_long_legged")
        if upwP <= 0.10 and lowwP >= 0.60: bull.add("dragonfly_doji"); allp.add("dragonfly_doji")
        if lowwP <= 0.10 and upwP >= 0.60: bear.add("gravestone_doji"); allp.add("gravestone_doji")

    # Spinning top / High-wave
    if bodyP <= SMALL_BODY_PCT and upw >= LONG_WICK_RATIO*body and loww >= LONG_WICK_RATIO*body:
        allp.add("spinning_top"); allp.add("high_wave")

    # Hammer-like (bull)
    if loww >= LONG_WICK_RATIO*body and upw <= body*0.5 and bodyP >= MIN_BODY_PCT:
        bull.add("hammer_like"); allp.add("hammer_family")

    # Shooting-star (bear)
    if upw  >= LONG_WICK_RATIO*body and loww <= body*0.5 and bodyP >= MIN_BODY_PCT:
        bear.add("shooting_star_like"); allp.add("inverted_hammer_family")

    # Marubozu
    if upw/rng <= TINY_WICK_TO_RANGE and loww/rng <= TINY_WICK_TO_RANGE:
        if C > O: bull.add("marubozu_bull"); allp.add("marubozu_bull")
        elif C < O: bear.add("marubozu_bear"); allp.add("marubozu_bear")

    if NEED_SWING_WICKS:
        isSH = _swing_high(h, i, SWING_LEN_WICKS)
        isSL = _swing_low(l, i, SWING_LEN_WICKS)
        if not isSL:
            bull = {p for p in bull if ("hammer" in p) or ("dragonfly" in p) or ("marubozu_bull" in p)}
        if not isSH:
            bear = {p for p in bear if ("shooting_star" in p) or ("gravestone" in p) or ("marubozu_bear" in p)}
    return bull, bear, bull|bear|allp

def detect_body_patterns(o,h,l,c,i) -> Tuple[Set[str], Set[str], Set[str]]:
    bull, bear, allp = set(), set(), set()
    if i-1 < 0: return bull, bear, allp

    O1,H1,L1,C1 = o[i-1], h[i-1], l[i-1], c[i-1]
    O2,H2,L2,C2 = o[i],   h[i],   l[i],   c[i]
    body1 = _body(O1,C1); body2 = _body(O2,C2)

    # Engulfing (перекрытие тел не меньше ENGULF_OVERLAP доли от большего тела)
    overlap = ENGULF_OVERLAP * max(body1, 1e-12)
    if (C2 > O2) and (C1 < O1) and (body2 >= body1) and (O2 <= C1 - overlap) and (C2 >= O1 + overlap):
        bull.add("engulf_bull"); allp.add("engulf")
    if (C2 < O2) and (C1 > O1) and (body2 >= body1) and (O2 >= C1 + overlap) and (C2 <= O1 - overlap):
        bear.add("engulf_bear"); allp.add("engulf")

    # Harami (второе тело внутри первого)
    if min(O1,C1) <= O2 <= max(O1,C1) and min(O1,C1) <= C2 <= max(O1,C1):
        if C1 < O1 and C2 > O2: bull.add("harami_bull"); allp.add("harami")
        if C1 > O1 and C2 < O2: bear.add("harami_bear"); allp.add("harami")

    # Outside bar
    if H2 >= H1 and L2 <= L1:
        if C2 > O2: bull.add("outside_bull"); allp.add("outside")
        if C2 < O2: bear.add("outside_bear"); allp.add("outside")

    return bull, bear, bull|bear|allp

# ---------------------- SIGNAL ENGINE ----------------------
def decide_signal(o,h,l,c):
    """
    Возврат: "long" | "short" | None — только по текущему ТФ, закрытая свеча.
    """
    n = len(c)
    if n < max(DEM_LEN+5, 10): return None
    i = n - 2  # закрытая

    # DeMarker
    dem_val = last_closed_dem(h, l, DEM_LEN)
    dem_long  = (not math.isnan(dem_val)) and dem_val <= OS
    dem_short = (not math.isnan(dem_val)) and dem_val >= OB

    # Patterns
    bull_w, bear_w, _ = detect_wick_patterns(o,h,l,c,i)
    bull_b, bear_b, _ = detect_body_patterns(o,h,l,c,i)

    conf_long  = 0
    conf_short = 0
    if dem_long:  conf_long  += 1
    if dem_short: conf_short += 1
    if bull_w or bull_b: conf_long  += 1
    if bear_w or bear_b: conf_short += 1

    need = 1 if CONFIRM_MODE == "any1" else 2
    go_long  = conf_long  >= need and conf_short == 0
    go_short = conf_short >= need and conf_long  == 0

    if go_long:  return "long"
    if go_short: return "short"
    return None

def lightning_align(sym: str) -> str:
    """
    Проверяет выравнивание DeMarker на двух ТФ (fast=4h, slow=1D).
    Возвращает: "bull" | "bear" | "" (нет совпадения)
    """
    try:
        oF,hF,lF,cF = bybit_kline(sym, LIGHTNING_FAST_TF, KLINE_LIMIT)
        oS,hS,lS,cS = bybit_kline(sym, LIGHTNING_SLOW_TF, KLINE_LIMIT)
    except Exception as e:
        logging.error(f"Lightning kline error {sym}: {e}")
        return ""

    demF = last_closed_dem(hF, lF, DEM_LEN)
    demS = last_closed_dem(hS, lS, DEM_LEN)

    if math.isnan(demF) or math.isnan(demS):
        return ""

    both_bull = (demF <= OS) and (demS <= OS)
    both_bear = (demF >= OB)  and (demS >= OB)

    if both_bull: return "bull"
    if both_bear: return "bear"
    return ""

# ---------------------- MAIN LOOP ----------------------
def main():
    state = load_state()  # ключ: f"{symbol}|{tf}" -> { "last_ts": int, "last_sig": "long/short/none", "last_light": "" }
    while True:
        try:
            for sym in TICKERS:
                # вычислим статус «молнии» один раз на символ
                light_dir = lightning_align(sym)  # "bull"/"bear"/""

                for tf in TIMEFRAMES:
                    try:
                        o,h,l,c = bybit_kline(sym, tf, KLINE_LIMIT)
                    except Exception as e:
                        logging.error(f"Kline error {sym} {tf}: {e}")
                        continue

                    sig = decide_signal(o,h,l,c)  # "long"/"short"/None

                    key = f"{sym}|{tf}"
                    last = state.get(key, {})
                    last_sig  = last.get("last_sig")
                    last_ts   = last.get("last_ts", 0)
                    last_l    = last.get("last_light", "")

                    bar_version = len(c) - 2  # номер закрытого бара

                    if sig:
                        # добавим ⚡, если направления совпадают и оба ТФ (4h/1D) в одной зоне
                        add_light = (light_dir == "bull" and sig == "long") or (light_dir == "bear" and sig == "short")
                        out = "▲" if sig == "long" else "▼"
                        if add_light:
                            out = f"{out}⚡"

                        # анти-дубль: не слать один и тот же результат на тот же закрытый бар
                        if sig != last_sig or bar_version != last_ts or (add_light and last_l == "") or ((not add_light) and last_l != ""):
                            if MESSAGE_MINIMAL:
                                send_tg(out)
                            else:
                                send_tg(out)  # слов не добавляем, оставляем минимализм
                            state[key] = {"last_sig": sig, "last_ts": bar_version, "last_light": ("bull" if add_light and sig=="long" else "bear" if add_light and sig=="short" else "")}
                            save_state(state)

        except Exception as e:
            logging.error(f"Loop error: {e}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()