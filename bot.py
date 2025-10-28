# bot.py — стабильная версия 2025-10-27

import os
import time
import json
import logging
import requests
from typing import List, Dict, Tuple

# ---------------------- ENV ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

BYBIT_URL      = os.getenv("BYBIT_URL", "https://api.bybit.com/v5/market/kline")
CATEGORY       = os.getenv("BYBIT_CATEGORY", "linear")

DEM_LEN = int(os.getenv("DEM_LEN", "28"))
OB      = float(os.getenv("DEM_OB", "0.70"))
OS      = float(os.getenv("DEM_OS", "0.30"))

# ---------------------- LOGGING ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ---------------------- UTILS ----------------------
def load_state() -> Dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state: Dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.error("Telegram credentials missing.")
        return False
    for i in range(3):
        try:
            r = requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text})
            if r.status_code == 200:
                return True
            logging.warning(f"TG send failed {r.status_code}: {r.text}")
            time.sleep(1 + i * 2)
        except Exception as e:
            logging.error(f"TG send error: {e}")
            time.sleep(1 + i * 2)
    return False

# ---------------------- BYBIT ----------------------
def bybit_klines(symbol: str, interval: str, limit: int = 200):
    params = {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(BYBIT_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json().get("result", {}).get("list", [])
    data = sorted(data, key=lambda x: int(x[0]))
    if len(data) >= 2:
        data = data[:-1]  # берем только закрытые свечи
    return data

# ---------------------- DEMARKER ----------------------
def calc_demarker(closes, highs, lows, length=DEM_LEN):
    up, dn = [], []
    for i in range(1, len(closes)):
        up.append(max(0.0, float(highs[i]) - float(highs[i - 1])))
        dn.append(max(0.0, float(lows[i - 1]) - float(lows[i])))
    n = min(len(up), len(dn))
    up, dn = up[-n:], dn[-n:]
    dem = []
    for i in range(length, n):
        su = sum(up[i - length:i])
        sd = sum(dn[i - length:i])
        denom = (su + sd) if (su + sd) > 0 else 1e-12
        dem.append(su / denom)
    return dem

# ---------------------- CANDLE PATTERNS ----------------------
def is_pinbar(o, h, l, c, body_ratio=0.33, wick_ratio=2.0):
    o, h, l, c = map(float, (o, h, l, c))
    body = abs(c - o)
    rng = max(1e-12, h - l)
    upper = h - max(c, o)
    lower = min(c, o) - l
    if body / rng > body_ratio:
        return False
    return (upper >= wick_ratio * body) or (lower >= wick_ratio * body)

def detect_candle_signal(o, h, l, c):
    if is_pinbar(o, h, l, c):
        return "🟢⬆️" if float(c) > float(o) else "🔴⬇️"
    return ""

# ---------------------- MAIN EVAL ----------------------
def make_key(symbol: str, tf: str, bar_open_ms: int, kind: str):
    return f"{symbol}|{tf}|{bar_open_ms}|{kind}"

def evaluate_symbol(symbol: str):
    TF_4H, TF_1D = "240", "D"
    out_messages = []

    k4 = bybit_klines(symbol, TF_4H, limit=DEM_LEN + 50)
    kd = bybit_klines(symbol, TF_1D, limit=DEM_LEN + 50)
    if len(k4) < DEM_LEN + 2 or len(kd) < DEM_LEN + 2:
        return out_messages

    o4, h4, l4, c4 = k4[-1][1], k4[-1][2], k4[-1][3], k4[-1][4]
    od, hd, ld, cd = kd[-1][1], kd[-1][2], kd[-1][3], kd[-1][4]

    closes4 = [x[4] for x in k4]; highs4 = [x[2] for x in k4]; lows4 = [x[3] for x in k4]
    closesd = [x[4] for x in kd]; highsd = [x[2] for x in kd]; lowsd = [x[3] for x in kd]

    dem4 = calc_demarker(closes4, highs4, lows4, DEM_LEN)
    demd = calc_demarker(closesd, highsd, lowsd, DEM_LEN)
    dem4_last, demd_last = dem4[-1], demd[-1]

    sig4 = "🟢⬆️" if dem4_last <= OS else ("🔴⬇️" if dem4_last >= OB else "")
    sigd = "🟢⬆️" if demd_last <= OS else ("🔴⬇️" if demd_last >= OB else "")

    candle4 = detect_candle_signal(o4, h4, l4, c4)
    candled = detect_candle_signal(od, hd, ld, cd)

    lightning = ""
    if (dem4_last >= OB and demd_last >= OB) or (dem4_last <= OS and demd_last <= OS):
        lightning = "⚡"

    candidates = []
    if len([x for x in [sig4, candle4] if x]) >= 2:
        candidates.append(("4H", [sig4, candle4]))
    if len([x for x in [sigd, candled] if x]) >= 2:
        candidates.append(("1D", [sigd, candled]))
    if lightning:
        candidates.append(("⚡", [lightning]))

    state = load_state()
    changed = False
    for tf, tokens in candidates:
        bar_time = int(k4[-1][0]) if tf in ("4H", "⚡") else int(kd[-1][0])
        kind = "".join(tokens)
        key = make_key(symbol, tf, bar_time, kind)
        if key not in state:
            text = f"{symbol} {''.join(tokens)}"
            if tg_send(text):
                state[key] = int(time.time())
                changed = True
    if changed:
        save_state(state)

# ---------------------- LOOP ----------------------
def main():
    symbols = ["BTCUSDT", "ETHUSDT", "PAXGUSDT"]  # можешь дописать свои тикеры
    logging.info("✅ Bot started. Polling every %s seconds.", POLL_SECONDS)
    while True:
        try:
            for s in symbols:
                evaluate_symbol(s)
                time.sleep(1)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()