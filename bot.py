# bot.py
# -*- coding: utf-8 -*-
import os, time, json, math, logging, requests
from typing import List, Dict, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ===================== ENV =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

BYBIT_URL      = os.getenv("BYBIT_URL", "https://api.bybit.com")
CATEGORY       = os.getenv("BYBIT_CATEGORY", "linear")  # linear | inverse | option | spot
SYMBOLS        = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
TF_MIN         = int(os.getenv("TF", "240"))  # 240=H4, 60=H1, 1440=D1
LIMIT          = int(os.getenv("LIMIT", "300"))

# DeMarker
DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))  # перекупленность
OS             = float(os.getenv("DEM_OS", "0.30"))  # перепроданность
EPS            = float(os.getenv("EPS", "1e-4"))

# Подтверждение таймфреймов: none | H4_D1
CONF_REQ       = os.getenv("CONF_REQ", "none").lower()

# Фитили (“стрелы”)
WICK_BODY_RATIO   = float(os.getenv("WICK_BODY_RATIO", "1.8"))    # фитиль >= X * тело
WICK_RANGE_RATIO  = float(os.getenv("WICK_RANGE_RATIO", "0.40"))   # фитиль >= Y * (high-low)
CHECK_LAST_BARS   = int(os.getenv("CHECK_LAST_BARS", "2"))         # проверяем N последних ЗАКРЫТЫХ свечей (обычно 2)

# Пауза между циклами
POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

# Состояние для дедупа
STATE_PATH     = os.getenv("STATE_PATH", "./state.json")

# ===================== UTILS =====================
def load_state() -> Dict[str, str]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict[str, str]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Failed to save state: {e}")

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram creds missing; message not sent.")
        return
    try:
        r = requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            logging.error(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

def interval_from_minutes(tf_min: int) -> str:
    # Bybit v5 intervals: 1,3,5,15,30,60,120,240,360,720,D,W,M
    m = tf_min
    if m in (1,3,5,15,30,60,120,240,360,720):
        return str(m)
    if m == 1440: return "D"
    if m == 10080: return "W"
    if m == 43200: return "M"
    return str(m)

# ===================== BYBIT =====================
def fetch_klines(symbol: str, tf_min: int, limit: int) -> List[Dict]:
    """
    Возвращает список свечей ASC (старые -> новые)
    Поля: start (ms), open, high, low, close (floats), volume (float)
    """
    interval = interval_from_minutes(tf_min)
    url = f"{BYBIT_URL}/v5/market/kline"
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit)
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={data.get('retCode')} {data.get('retMsg')}")
    lst = data["result"]["list"]  # частo newest->oldest
    bars = []
    for row in lst:
        # row = [start, open, high, low, close, volume, turnover]
        start = int(row[0])
        o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
        v = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
        bars.append({"start": start, "open": o, "high": h, "low": l, "close": c, "volume": v})
    bars.sort(key=lambda x: x["start"])  # в ASC
    return bars

def only_closed(bars: List[Dict], tf_min: int) -> List[Dict]:
    """Отбрасывает текущую строящуюся свечу (если она ещё не закрылась)."""
    if not bars:
        return bars
    now_ms = int(time.time() * 1000)
    tf_ms  = tf_min * 60_000
    last = bars[-1]
    if now_ms < last["start"] + tf_ms:
        return bars[:-1]
    return bars

# ===================== INDICATORS =====================
def sma(values: List[float], length: int) -> List[float]:
    out = [math.nan]*len(values)
    if length <= 0 or len(values) < length:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= length:
            s -= values[i-length]
        if i >= length-1:
            out[i] = s/length
    return out

def demarker_from_hl(highs: List[float], lows: List[float], length: int) -> List[float]:
    """
    DeMarker:
      Up[i]   = max( high[i] - high[i-1], 0 )
      Down[i] = max( low[i-1] - low[i], 0 )
      DeM[i]  = SMA(Up, n) / (SMA(Up, n) + SMA(Down, n))
    """
    n = len(highs)
    up = [0.0]*n
    dn = [0.0]*n
    for i in range(1, n):
        uh = highs[i] - highs[i-1]
        up[i] = uh if uh > 0 else 0.0
        dl = lows[i-1] - lows[i]
        dn[i] = dl if dl > 0 else 0.0
    up_sma = sma(up, length)
    dn_sma = sma(dn, length)
    dem = [math.nan]*n
    for i in range(n):
        u = up_sma[i]
        d = dn_sma[i]
        if not math.isnan(u) and not math.isnan(d) and (u + d) > 0:
            dem[i] = u / (u + d)
    return dem

# ===================== PATTERNS (WICKS) =====================
def wick_stats(o: float, h: float, l: float, c: float) -> Tuple[float,float,float,float]:
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    rng   = max(h - l, 1e-9)
    return body, upper, lower, rng

def is_arrow_down(o,h,l,c) -> bool:
    # длинный верхний фитиль — стрелка вниз (SELL)
    body, upper, lower, rng = wick_stats(o,h,l,c)
    return (upper >= WICK_BODY_RATIO * body) and (upper >= WICK_RANGE_RATIO * rng)

def is_arrow_up(o,h,l,c) -> bool:
    # длинный нижний фитиль — стрелка вверх (BUY)
    body, upper, lower, rng = wick_stats(o,h,l,c)
    return (lower >= WICK_BODY_RATIO * body) and (lower >= WICK_RANGE_RATIO * rng)

# ===================== SIGNAL LOGIC =====================
def dem_zone(v: float) -> str:
    if v >= OB - EPS: return "OB"  # overbought
    if v <= OS + EPS: return "OS"  # oversold
    return "MID"

def align_tf_condition(h4_zone: str, d1_zone: str) -> bool:
    # Совпадение зон для подтверждения H4_D1
    if h4_zone == "OB" and d1_zone == "OB": return True
    if h4_zone == "OS" and d1_zone == "OS": return True
    return False

def build_signal_text(symbol: str, tf_min: int, direction: str, reason: str,
                      price: float, dem_val: float, bar_time_ms: int) -> str:
    tf_label = f"{tf_min}m" if tf_min < 1440 else ("1D" if tf_min==1440 else f"{tf_min}m")
    ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(bar_time_ms/1000))
    return (
        f"<b>{symbol}</b> | <b>{direction}</b> | TF <b>{tf_label}</b>\n"
        f"Price: <code>{price:.2f}</code>\n"
        f"DeMarker: <code>{dem_val:.4f}</code>\n"
        f"Reason: {reason}\n"
        f"Bar close (UTC): <code>{ts}</code>"
    )

def evaluate_symbol(symbol: str, state: Dict[str,str]) -> None:
    # ---- основная ТФ ----
    bars = fetch_klines(symbol, TF_MIN, LIMIT)
    bars = only_closed(bars, TF_MIN)
    if len(bars) < max(DEM_LEN+2, 10):
        logging.info(f"{symbol}: not enough bars")
        return

    highs = [b["high"] for b in bars]
    lows  = [b["low"]  for b in bars]
    closes= [b["close"]for b in bars]
    dem   = demarker_from_hl(highs, lows, DEM_LEN)

    # Для H4/D1 подтверждения — берём D1 зону, если требуется
    d1_zone = None
    if CONF_REQ == "h4_d1":
        d1_bars = fetch_klines(symbol, 1440, max(DEM_LEN+2, 120))
        d1_bars = only_closed(d1_bars, 1440)
        if len(d1_bars) >= DEM_LEN+2:
            d1_highs = [b["high"] for b in d1_bars]
            d1_lows  = [b["low"]  for b in d1_bars]
            d1_dem   = demarker_from_hl(d1_highs, d1_lows, DEM_LEN)
            d1_zone  = dem_zone(d1_dem[-1])

    # --- проверяем последние закрытые свечи: последняя и предпоследняя ---
    n = len(bars)
    look = min(max(CHECK_LAST_BARS, 1), 5)
    idxs = list(range(n - look, n))  # индексы последних закрытых свечей

    for i in idxs:
        b  = bars[i]
        o,h,l,c = b["open"], b["high"], b["low"], b["close"]
        d  = dem[i]
        if math.isnan(d):
            continue

        zone = dem_zone(d)
        if CONF_REQ == "h4_d1" and d1_zone is not None:
            if not align_tf_condition(zone, d1_zone):
                # если требуется H4_D1 и они не совпали — пропускаем
                continue

        # --- стрелы-фитили ---
        sell_by_wick = (zone == "OB") and is_arrow_down(o,h,l,c)
        buy_by_wick  = (zone == "OS") and is_arrow_up(o,h,l,c)

        # --- чистые кроссы DeMarker (оставлено для совместимости, ничего не удалял) ---
        cross_sell = False
        cross_buy  = False
        if i >= 2 and not math.isnan(dem[i-1]):
            prev = dem[i-1]
            cross_sell = (prev < OB - EPS) and (d >= OB - EPS)
            cross_buy  = (prev > OS + EPS) and (d <= OS + EPS)

        # приоритет: фитильные сигналы, затем кроссы
        signal = None
        reason = None
        direction = None
        if sell_by_wick:
            signal = True
            direction = "SELL"
            reason = f"WICK-ARROW ↑ (upper) & DeM≥{OB}"
        elif buy_by_wick:
            signal = True
            direction = "BUY"
            reason = f"WICK-ARROW ↓ (lower) & DeM≤{OS}"
        elif cross_sell:
            signal = True
            direction = "SELL"
            reason = f"DeMarker CROSS into OB (≥{OB})"
        elif cross_buy:
            signal = True
            direction = "BUY"
            reason = f"DeMarker CROSS into OS (≤{OS})"

        if not signal:
            continue

        # дедуп по символу+TF+времени свечи+направлению
        sig_id = f"{symbol}|{TF_MIN}|{b['start']}|{direction}"
        if state.get("last_id") == sig_id:
            continue

        text = build_signal_text(
            symbol=symbol, tf_min=TF_MIN, direction=direction, reason=reason,
            price=c, dem_val=d, bar_time_ms=b["start"]
        )
        send_telegram(text)
        state["last_id"] = sig_id
        save_state(state)

# ===================== MAIN LOOP =====================
def main_loop():
    state = load_state()
    while True:
        try:
            for sym in SYMBOLS:
                try:
                    evaluate_symbol(sym, state)
                except Exception as e:
                    logging.error(f"{sym} error: {e}")
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            logging.info("Stopped by user.")
            break
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main_loop()