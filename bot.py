# bot.py — 4H+1D, DeMarker28, длинные фитили (hammer/shooting-star/…),
# молния ⚡️ при совпадении зон, устойчивый Bybit URL без дублей

import os, time, json, logging, requests, re
from typing import List, Dict
from urllib.parse import urlparse

# ====================== ENV ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))

DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))
OS             = float(os.getenv("DEM_OS", "0.30"))

STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# ====================== BYBIT v5 (устойчивый URL) ======================
def _bybit_base() -> str:
    raw = os.getenv("BYBIT_URL", "https://api.bybit.com")
    u = urlparse(raw if "://" in raw else f"https://{raw}")
    scheme = u.scheme or "https"
    host   = u.netloc or u.path or "api.bybit.com"
    return f"{scheme}://{host}"

BYBIT_BASE      = _bybit_base()
BYBIT_KLINE_URL = f"{BYBIT_BASE}/v5/market/kline"   # один раз, без дублей

def fetch_kline(symbol: str, interval: str, limit: int = 200, category: str = "linear", timeout: int = 20):
    params = {
        "category": category,      # USDT перпеты
        "symbol":   symbol,        # BTCUSDT / ETHUSDT / ...
        "interval": str(interval), # "240", "D"
        "limit":    str(limit),
    }
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ====================== ЛОГИ ======================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ====================== УТИЛИТЫ ======================
def load_state() -> Dict:
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except:
            return {}
    return {}

def save_state(state: Dict):
    try:
        json.dump(state, open(STATE_PATH, "w"))
    except Exception as e:
        logging.error("Save state error: %s", e)

def send_tg(symbol: str, text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    payload = {"chat_id": CHAT_ID, "text": f"{symbol} {text}".strip(), "disable_notification": True}
    try:
        requests.post(TG_API, data=payload, timeout=10)
    except Exception as e:
        logging.error("TG send error: %s", e)

# ====================== ТИКЕРЫ ======================
def parse_symbols():
    raw = os.getenv("TICKERS", os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"))
    parts = [p.strip().upper() for p in re.split(r"[,\s]+", raw) if p.strip()]
    return list(dict.fromkeys(parts))

SYMBOLS = parse_symbols()

# ====================== DeMarker ======================
def demarker_from_candles(candles: List[List[str]], length: int = DEM_LEN) -> float:
    # Bybit v5 list: [ t, open, high, low, close, volume, turnover ]
    if len(candles) < length + 1:
        return 0.5
    highs = [float(c[2]) for c in candles][- (length + 1):]
    lows  = [float(c[3]) for c in candles][- (length + 1):]

    up = dn = 0.0
    for i in range(1, len(highs)):
        up += max(highs[i] - highs[i-1], 0.0)
        dn += max(lows[i-1] - lows[i], 0.0)

    if up + dn == 0:
        return 0.5
    return up / (up + dn)

def zone(val: float) -> str:
    if val >= OB: return "overbought"
    if val <= OS: return "oversold"
    return "neutral"

# ====================== Свечные паттерны (длинные фитили) ======================
def classify_long_wick_patterns_last_closed(candles: List[List[str]]) -> List[str]:
    """
    Возвращает список ярлыков паттернов на последней ЗАКРЫТОЙ свече.
    Покрытие:
      - bullish_pin / bearish_pin
      - hammer / hanging_man
      - shooting_star / inverted_hammer
      - dragonfly_doji / gravestone_doji / long_legged_doji
    """
    if not candles:
        return []
    c = candles[-1]
    o = float(c[1]); h = float(c[2]); l = float(c[3]); cl = float(c[4])

    body = abs(cl - o)
    rng  = max(h - l, 1e-8)
    body = max(body, 1e-8)  # защита от деления на 0

    upper = max(h - max(o, cl), 0.0)
    lower = max(min(o, cl) - l, 0.0)

    body_share   = body / rng
    upper_share  = upper / rng
    lower_share  = lower / rng

    # Пороговые метрики (консервативные, но популярные)
    SMALL_BODY     = 0.3      # тело <= 30% диапазона
    LONG_WICK_K    = 2.0      # длинный фитиль >= 2x тело
    VERY_LONG_WICK = 0.6      # очень длинный фитиль >= 60% диапазона
    DOJI_BODY      = 0.1      # почти дожи

    labels = []

    # --- Pin bars ---
    if lower > LONG_WICK_K * body and cl > o:
        labels.append("bullish_pin")
    if upper > LONG_WICK_K * body and cl < o:
        labels.append("bearish_pin")

    # --- Hammer / Hanging Man (малое тело, длинный нижний фитиль) ---
    if body_share <= SMALL_BODY and lower >= LONG_WICK_K * body and (max(o, cl) > l + 0.6 * rng):
        # тело ближе к верху диапазона
        # bull context чаще hammer; bear контекст — hanging man (решим по зоне позже)
        labels.append("hammer_hanging")

    # --- Shooting Star / Inverted Hammer (малое тело, длинный верхний фитиль) ---
    if body_share <= SMALL_BODY and upper >= LONG_WICK_K * body and (min(o, cl) < l + 0.4 * rng):
        # тело ближе к низу диапазона
        labels.append("star_inverted")

    # --- Doji варианты ---
    if body_share <= DOJI_BODY:
        if lower_share >= 0.6 and upper_share <= 0.15:
            labels.append("dragonfly_doji")
        elif upper_share >= 0.6 and lower_share <= 0.15:
            labels.append("gravestone_doji")
        elif upper_share >= 0.35 and lower_share >= 0.35:
            labels.append("long_legged_doji")

    return labels

# ====================== Маппинг паттернов -> сигналов ======================
BULLISH_LABELS = {"bullish_pin", "hammer_hanging", "star_inverted", "dragonfly_doji", "long_legged_doji"}
BEARISH_LABELS = {"bearish_pin", "star_inverted", "hammer_hanging", "gravestone_doji", "long_legged_doji"}
# Примечание: "star_inverted" и "hammer_hanging" уточняем по зоне:
#   в oversold → трактуем как бычий; в overbought → как медвежий.
# long_legged_doji — нейтральный, решаем по зоне.

def signals_from_patterns(labels: List[str], z: str) -> List[str]:
    out = []
    for lb in labels:
        if lb in {"bullish_pin", "dragonfly_doji"} and z == "oversold":
            out.append("🟢⬆️")
        elif lb in {"bearish_pin", "gravestone_doji"} and z == "overbought":
            out.append("🔴⬇️")
        elif lb in {"hammer_hanging", "star_inverted", "long_legged_doji"}:
            if z == "oversold":
                out.append("🟢⬆️")
            elif z == "overbought":
                out.append("🔴⬇️")
    return out

# ====================== Основной цикл ======================
state = load_state()
logging.info("Start bot with %d symbols...", len(SYMBOLS))

while True:
    for sym in SYMBOLS:
        try:
            # Фьючерсы USDT → category='linear'
            r4 = fetch_kline(sym, "240", limit=200, category="linear")
            r1 = fetch_kline(sym, "D",   limit=200, category="linear")

            kl4 = r4.get("result", {}).get("list", []) or []
            kl1 = r1.get("result", {}).get("list", []) or []

            if len(kl4) < DEM_LEN + 1 or len(kl1) < DEM_LEN + 1:
                continue

            # DeMarker
            d4 = demarker_from_candles(kl4, DEM_LEN)
            d1 = demarker_from_candles(kl1, DEM_LEN)
            z4, z1 = zone(d4), zone(d1)

            # Паттерны (длинные фитили) — 4H и 1D
            labs4 = classify_long_wick_patterns_last_closed(kl4)
            labs1 = classify_long_wick_patterns_last_closed(kl1)

            # Ключи дедупа
            k4_base = f"{sym}:{kl4[-1][0]}:4h"
            k1_base = f"{sym}:{kl1[-1][0]}:1d"
            kL      = f"{sym}:{kl4[-1][0]}:{kl1[-1][0]}:lightning"

            parts = []

            # ⚡️ молния — совпадение зон 4H и 1D (перекуп/перепрод)
            if z4 == z1 and z4 in ("overbought", "oversold") and state.get(kL) != 1:
                parts.append("⚡️")
                state[kL] = 1

            # 4H — сигналы из паттернов по зоне
            for lb in labs4:
                kk = f"{k4_base}:{lb}"
                if state.get(kk) == 1:
                    continue
                for s in signals_from_patterns([lb], z4):
                    parts.append(s)
                    state[kk] = 1

            # 1D — сигналы из паттернов по зоне
            for lb in labs1:
                kk = f"{k1_base}:{lb}"
                if state.get(kk) == 1:
                    continue
                for s in signals_from_patterns([lb], z1):
                    parts.append(s)
                    state[kk] = 1

            if parts:
                # Склеиваем: при нескольких паттернах на свече придут несколько эмодзи
                send_tg(sym, "".join(parts))

        except requests.HTTPError as e:
            logging.error("%s: HTTP error: %s", sym, e)
        except Exception as e:
            logging.error("%s: %s", sym, e)

    save_state(state)
    time.sleep(POLL_SECONDS)