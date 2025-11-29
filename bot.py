# ============================
# bot.py — FINAL VERSION (Bybit PERP ONLY + Lightning-Only Logic)
# ============================

import os, time, json, requests
from typing import List, Dict, Optional
from datetime import datetime

# ================= CONFIG =====================

STATE_PATH       = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API           = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN          = int(os.getenv("DEM_LEN", "28"))

# Порог для 4H
DEM_OB_4H        = float(os.getenv("DEM_OB_4H", "0.70"))
DEM_OS_4H        = float(os.getenv("DEM_OS_4H", "0.30"))
# Порог для 1D
DEM_OB_1D        = float(os.getenv("DEM_OB_1D", "0.71"))
DEM_OS_1D        = float(os.getenv("DEM_OS_1D", "0.29"))

KLINE_4H         = os.getenv("KLINE_4H", "4h")
KLINE_1D         = os.getenv("KLINE_1D", "1d")

POLL_SECONDS     = 60

BYBIT_BASE       = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_KLINES        = f"{BYBIT_BASE}/v5/market/kline"
BB_TIMEOUT       = 15
BB_INST          = f"{BYBIT_BASE}/v5/market/instruments"

# TwelveData
TD_API_KEY       = os.getenv("TWELVEDATA_API_KEY", "")
TD_BASE          = os.getenv("TWELVEDATA_BASE", "https://api.twelvedata.com")
TD_TIMEOUT       = 15

TD_MINUTE_LIMIT  = int(os.getenv("TD_MINUTE_LIMIT", "8"))
TD_DAILY_LIMIT   = int(os.getenv("TD_DAILY_LIMIT", "780"))

TD_REFRESH_4H    = int(os.getenv("TD_REFRESH_4H", "7200"))
TD_REFRESH_1D    = int(os.getenv("TD_REFRESH_1D", "43200"))

TD_CACHE: Dict = {}
TD_RATE = {"minute_start": 0.0, "minute_count": 0}

# ================= STATE =====================

def load_state(path: str) -> Dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return {"sent": {}, "last_debug": 0}

def save_state(path: str, data: Dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except:
        pass

def gc_state(state: Dict, days=21):
    cutoff = int(time.time()) - days*86400
    sent = state.get("sent", {})
    for k, v in list(sent.items()):
        if isinstance(v, int) and v < cutoff:
            del sent[k]
    state["sent"] = sent
    if "last_debug" not in state:
        state["last_debug"] = 0
    if "td_day" not in state:
        state["td_day"] = time.strftime("%Y%m%d", time.gmtime())
    if "td_count" not in state:
        state["td_count"] = 0
    if "plan_idx" not in state:
        state["plan_idx"] = 0

STATE = load_state(STATE_PATH)

# ===================== TELEGRAM ========================

def _chat_tokens() -> List[str]:
    out = []
    if TELEGRAM_CHAT:
        for x in TELEGRAM_CHAT.split(","):
            x = x.strip()
            if x.startswith("-100"):
                out.append(x)
    return out

def tg_send_one(cid: str, text: str) -> bool:
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=10
        )
        return r.status_code == 200
    except:
        return False

def _broadcast_signal(text: str, key: str) -> bool:
    chats = _chat_tokens()
    ts = int(time.time())
    sent_any = False
    for cid in chats:
        k2 = f"{key}|{cid}"
        if STATE["sent"].get(k2):
            continue
        if tg_send_one(cid, text):
            STATE["sent"][k2] = ts
            sent_any = True
    return sent_any

# ===================== HELPERS =========================

def closed_ohlc(ohlc):
    if not ohlc or len(ohlc) < 2:
        return []
    return ohlc[:-1]

# ================= INDICATORS =====================

def demarker_series(o, length):
    if not o or len(o) < length + 1:
        return None
    highs = [x[2] for x in o]
    lows  = [x[3] for x in o]
    up = [0.0]; dn = [0.0]
    for i in range(1, len(o)):
        up.append(max(highs[i] - highs[i-1], 0.0))
        dn.append(max(lows[i-1] - lows[i], 0.0))
    def sma(a, i, n): return sum(a[i-n+1:i+1]) / n
    dem = [None] * len(o)
    for i in range(length, len(o)):
        u = sma(up, i, length); d = sma(dn, i, length)
        dem[i] = u / (u + d) if (u + d) != 0 else 0.5
    return dem

def last_closed(series):
    if not series:
        return None
    i = len(series) - 1
    while i >= 0 and series[i] is None:
        i -= 1
    return series[i] if i >= 0 else None

def zone_of(v, tf: str):
    if v is None:
        return None
    if tf == "4H":
        ob, os_ = DEM_OB_4H, DEM_OS_4H
    else:
        ob, os_ = DEM_OB_1D, DEM_OS_1D
    if v >= ob:
        return "OB"
    if v <= os_:
        return "OS"
    return None

# ================= PATTERNS =====================

def pinbar_by_zone(o, idx, zone, pct=0.30):
    if zone not in ("OB", "OS"):
        return False
    if not o or not (-len(o) <= idx < len(o)):
        return False

    o_, h_, l_, c_ = o[idx][1:5]
    body = abs(c_ - o_)
    if body <= 0:
        return False

    upper = h_ - max(o_, c_)
    lower = min(o_, c_) - l_

    if zone == "OB":
        return upper >= pct * body
    else:
        return lower >= pct * body

def engulfing_with_prior4(o):
    if not o or len(o) < 3:
        return False

    o2, _, _, c2 = o[-1][1:5]
    o3, _, _, c3 = o[-2][1:5]
    o4, _, _, c4 = o[-3][1:5]

    bull2 = c2 >= o2
    bull3 = c3 >= o3
    bull4 = c4 >= o4

    cover = (min(o2, c2) <= min(o3, c3)) and (max(o2, c2) >= max(o3, c3))

    bull = bull2 and (not bull3) and (not bull4) and cover
    bear = (not bull2) and bull3 and bull4 and cover

    return bull or bear

def pyramidal_pattern(o, idx, zone, pct=0.85):
    if zone not in ("OB", "OS"):
        return False
    if not o or not (-len(o) <= idx < len(o)):
        return False

    o_, h_, l_, c_ = o[idx][1:5]
    total = h_ - l_
    if total <= 0:
        return False

    upper = h_ - max(o_, c_)
    lower = min(o_, c_) - l_

    if zone == "OB":
        return upper >= pct * total
    else:
        return lower >= pct * total

def color_flip_pattern_1d(o, zone):
    if zone not in ("OB", "OS"):
        return False
    if not o or len(o) < 2:
        return False

    o_prev, _, _, c_prev = o[-2][1:5]
    o_last, _, _, c_last = o[-1][1:5]

    prev_green = c_prev >= o_prev
    last_green = c_last >= o_last

    if zone == "OS":
        return (not prev_green) and last_green
    else:
        return prev_green and (not last_green)

def candle_pattern(o, zone):
    if not o or len(o) < 3:
        return False
    if zone not in ("OB", "OS"):
        return False

    has_pin = pinbar_by_zone(o, -1, zone, 0.40)
    has_eng = engulfing_with_prior4(o)
    return has_pin or has_eng

def lightning_has_pattern(k4, z4, k1, z1) -> bool:
    if z4 and k4 and len(k4) >= 3 and engulfing_with_prior4(k4):
        return True
    if z1 and k1 and len(k1) >= 3 and engulfing_with_prior4(k1):
        return True
    if z1 and k1 and pinbar_by_zone(k1, -1, z1, 0.50):
        return True
    if z1 and k1 and color_flip_pattern_1d(k1, z1):
        return True
    if z4 and k4 and pyramidal_pattern(k4, -1, z4):
        return True
    if z1 and k1 and pyramidal_pattern(k1, -1, z1):
        return True
    return False


# ================= BYBIT PERPETUAL DISCOVERY =====================

def get_bybit_perp_symbols():
    """Load ALL perpetual contracts from Bybit (linear + inverse)."""
    out = set()
    for cat in ("linear", "inverse"):
        try:
            r = requests.get(
                BB_INST,
                params={"category": cat},
                timeout=10
            )
            j = r.json()
            lst = (j.get("result") or {}).get("list") or []
            for it in lst:
                if it.get("status") != "Trading":
                    continue
                ctype = it.get("contractType", "")
                if "Perpetual" not in ctype:
                    continue
                sym = it.get("symbol")
                if sym:
                    out.add(sym)
        except:
            continue
    return sorted(list(out))


# ================= TwelveData helpers =====================

def fx_to_td(sym: str) -> str:
    s = sym.upper()
    if len(s) == 6 and s[:3].isalpha() and s[3:].isalpha():
        return s[:3] + "/" + s[3:]
    return s

def ru_to_td(sym: str):
    u = sym.upper()
    if u.endswith(".ME"):
        base = u.split(".")[0]
        return f"{base}:MOEX"
    return u

# ================= FORMAT =====================

def is_fx_sym(sym: str) -> bool:
    s = "".join(ch for ch in sym.upper() if ch.isalpha())
    return len(s) >= 6

def to_display(sym: str) -> str:
    s = sym.upper()
    if s.endswith(".ME"):
        return s
    if s.endswith("USDT"):
        return s[:-4] + "-USDT"
    return s

def format_signal(symbol, sig, zone, src):
    arrow = "🔴↓" if zone == "OB" else ("🟢↑" if zone == "OS" else "")
    status = "⚡" if sig == "LIGHT" else ""
    return f"{to_display(symbol)} [{src}] {arrow}{status}"

# ================= BYBIT FETCH =====================

def fetch_bybit_klines(symbol, interval):
    iv = "240" if interval == "4h" else "D"
    try:
        r = requests.get(
            BB_KLINES,
            params={"category": "linear", "symbol": symbol, "interval": iv, "limit": 600},
            timeout=10
        )
        j = r.json()
        lst = (j.get("result") or {}).get("list") or []
        out = []
        for k in lst:
            ts = int(k[0])
            ts = ts // 1000 if ts > 10**12 else ts
            o = float(k[1]); h = float(k[2])
            l = float(k[3]); c = float(k[4])
            if h <= 0 or l <= 0:
                continue
            out.append([ts, o, h, l, c])
        out.sort(key=lambda x: x[0])
        return out
    except:
        return None

# ================= PLAN BUILDER =====================

CORE_CRYPTO = ["BTC", "ETH", "SOL", "XRP"]

def build_plan():
    plan = []
    # 1) Core crypto (original logic)
    for x in CORE_CRYPTO:
        plan.append(("CRYPTO_CORE", x))

    # 2) All Bybit PERP contracts (new logic)
    all_perp = get_bybit_perp_symbols()
    for sym in all_perp:
        # Skip symbols like BTCUSDT we already handle manually
        if sym.startswith("BTC") or sym.startswith("ETH") or sym.startswith("SOL") or sym.startswith("XRP"):
            continue
        plan.append(("BYBIT_PERP", sym))

    return plan


# ================= PROCESS SYMBOL =====================

def process_symbol(kind, name):
    if kind == "CRYPTO_CORE":
        # Fetch standard linear symbol (BTC → BTCUSDT)
        bb_sym = name + "USDT"
        k4 = fetch_bybit_klines(bb_sym, KLINE_4H)
        k1 = fetch_bybit_klines(bb_sym, KLINE_1D)
        src = "BB"
    else:
        # BYBIT_PERP — full symbol already
        bb_sym = name
        k4 = fetch_bybit_klines(bb_sym, KLINE_4H)
        k1 = fetch_bybit_klines(bb_sym, KLINE_1D)
        src = "BB"

    if not k4 and not k1:
        return False

    k4c = closed_ohlc(k4) if k4 else None
    k1c = closed_ohlc(k1) if k1 else None

    have4 = bool(k4c)
    have1 = bool(k1c)

    d4 = demarker_series(k4c, DEM_LEN) if have4 else None
    d1 = demarker_series(k1c, DEM_LEN) if have1 else None

    v4 = last_closed(d4) if d4 else None
    v1 = last_closed(d1) if d1 else None

    z4 = zone_of(v4, "4H")
    z1 = zone_of(v1, "1D")

    # ========== CORE crypto (old logic) ==========
    if kind == "CRYPTO_CORE":
        sent = False

        pat4 = candle_pattern(k4c, z4) if have4 and z4 else False
        pat1 = candle_pattern(k1c, z1) if have1 and z1 else False

        open4 = k4c[-1][0] if have4 else None
        open1 = k1c[-1][0] if have1 else None
        dual  = max([x for x in (open4, open1) if x]) if (open4 or open1) else None

        # ⚡ same-zone lightning
        if z4 and z1 and z4 == z1:
            if lightning_has_pattern(k4c, z4, k1c, z1):
                key = f"{bb_sym}|LIGHT|{z4}|{dual}|BB"
                if _broadcast_signal(format_signal(bb_sym, "LIGHT", z4, src), key):
                    return True

        # 1TF4H
        if have4 and z4 and pat4 and not (z1 and z1 == z4):
            key = f"{bb_sym}|1TF4H|{z4}|{open4}|BB"
            if _broadcast_signal(format_signal(bb_sym, "1TF4H", z4, src), key):
                return True

        # 1TF1D
        if have1 and z1 and pat1 and not (z4 and z4 == z1):
            key = f"{bb_sym}|1TF1D|{z1}|{open1}|BB"
            if _broadcast_signal(format_signal(bb_sym, "1TF1D", z1, src), key):
                return True

        return False

    # ========== BYBIT PERP LOGIC (new lightning-only) ==========

    # Only lightning:
    if z4 == "OB" and z1 == "OB":
        if lightning_has_pattern(k4c, z4, k1c, z1):
            # SELL ONLY
            key = f"{bb_sym}|LIGHT|OB|{int(time.time())}|BB"
            return _broadcast_signal(format_signal(bb_sym, "LIGHT", "OB", src), key)

    return False


# ================= MAIN LOOP =====================

def main():
    plan = build_plan()
    n = len(plan)
    print(f"INFO: Symbols loaded: {n}", flush=True)
    if plan:
        print(f"Loaded {n} symbols for scan.", flush=True)
        print(f"First symbol checked: {plan[0][1]}", flush=True)

    idx = STATE.get("plan_idx", 0)
    if idx >= n:
        idx = 0

    while True:
        start = time.time()
        plan = build_plan()
        n = len(plan)
        if idx >= n:
            idx = 0

        processed = 0
        while processed < TD_MINUTE_LIMIT:
            kind, name = plan[idx]
            process_symbol(kind, name)
            idx = (idx + 1) % n
            processed += 1
            time.sleep(5)

        STATE["plan_idx"] = idx
        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)

        elapsed = time.time() - start
        sleep_left = 60.0 - elapsed
        if sleep_left > 0:
            time.sleep(sleep_left)

if __name__ == "__main__":
    main()