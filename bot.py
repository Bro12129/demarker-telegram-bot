# bot.py — версия "как вчера" + свечные паттерны
import os, json, time, math, logging
from typing import Any, Dict, List, Tuple
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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

# Тикеры и интервалы
TICKERS_ENV    = os.getenv("TICKERS", "BTCUSDT,ETHUSDT")
TICKERS: List[str] = [t.strip().upper() for t in TICKERS_ENV.split(",") if t.strip()]
INTERVALS_MIN  = [240, 1440]  # 4H и 1D

# === ВАЖНО: как ВЧЕРА ===
# Берём URL ИЗ ENV БЕЗ ДОПОЛНИТЕЛЬНЫХ СКЛЕЕК.
BYBIT_KLINE_URL = os.getenv("BYBIT_URL", "https://api.bybit.com/v5/market/kline").rstrip("/")
BYBIT_CATEGORY  = os.getenv("BYBIT_CATEGORY", "linear")  # linear|inverse|spot

# ---------------------- NET ----------------------
def http_get_json(url: str, params: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    tries, last = 3, None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            logging.error("HTTP GET failed (%s/%s) %s %s", i+1, tries, url, params)
            time.sleep(1 + i)
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")

# ---------------------- BYBIT ----------------------
def fetch_klines(symbol: str, interval_minutes: int = 240, limit: int = 300) -> List[Dict[str, Any]]:
    params = {
        "category": BYBIT_CATEGORY,
        "symbol": symbol,
        "interval": str(interval_minutes),
        "limit": str(limit),
    }
    data = http_get_json(BYBIT_KLINE_URL, params)
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data}")
    rows = data.get("result", {}).get("list", [])
    kl = []
    for row in rows:
        ts = int(row[0]) // 1000
        o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
        kl.append({"t": ts, "o": o, "h": h, "l": l, "c": c})
    kl.reverse()  # старые -> новые
    return kl

# ---------------------- INDICATORS ----------------------
def demarker(high: List[float], low: List[float], length: int) -> List[float]:
    n = len(high)
    if n != len(low) or n == 0:
        return []
    demax = [0.0]*n; demin = [0.0]*n
    for i in range(1, n):
        demax[i] = max(high[i]-high[i-1], 0.0)
        demin[i] = max(low[i-1]-low[i], 0.0)

    def sma(arr: List[float], m: int) -> List[float]:
        out = [math.nan]*n; s = 0.0
        for i in range(n):
            s += arr[i]
            if i >= m: s -= arr[i-m]
            if i >= m-1: out[i] = s/m
        return out

    demx = sma(demax, length); demn = sma(demin, length)
    out = [math.nan]*n
    for i in range(n):
        a, b = demx[i], demn[i]
        out[i] = math.nan if (math.isnan(a) or math.isnan(b) or (a+b)==0) else a/(a+b)
    return out

# ---------------------- CANDLE PATTERNS (для последней ЗАКРЫТОЙ свечи) ----------------------
def detect_patterns(prev: Dict[str,float], cur: Dict[str,float]) -> List[str]:
    p = []
    o1,h1,l1,c1 = prev["o"],prev["h"],prev["l"],prev["c"]
    o2,h2,l2,c2 = cur["o"],cur["h"],cur["l"],cur["c"]

    body2 = abs(c2-o2); rng2 = max(h2-l2, 1e-12)
    upper2 = h2 - max(o2,c2); lower2 = min(o2,c2) - l2

    # Doji
    if body2 <= rng2*0.1: p.append("doji")

    # Hammer / Hanging Man / Shooting Star (по теням)
    if lower2 >= rng2*0.6 and body2 <= rng2*0.3:
        if c2 > o2: p.append("hammer")
        else: p.append("hanging man")
    if upper2 >= rng2*0.6 and body2 <= rng2*0.3:
        p.append("shooting star")

    # Engulfing
    if (c1<o1) and (c2>o2) and (o2<=c1) and (c2>=o1):
        p.append("bullish engulfing")
    if (c1>o1) and (c2<o2) and (o2>=c1) and (c2<=o1):
        p.append("bearish engulfing")

    # Morning/Evening Star (упрощённо: маленькое тело между направленными свечами)
    small1 = abs(c1-o1) <= (max(h1-l1,1e-12))*0.3
    if (c1<o1) and small1 and (c2>o2) and (c2>o1):
        p.append("morning star")
    if (c1>o1) and small1 and (c2<o2) and (c2<o1):
        p.append("evening star")

    return p

# ---------------------- STATE ----------------------
def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(path: str, state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        # не валим процесс, просто лог
        logging.error("Failed to save state: %s", e)

# ---------------------- TG ----------------------
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM creds empty; skip")
        return
    try:
        r = requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logging.error("Telegram send failed: %s", e)

# ---------------------- SIGNALS ----------------------
def analyze_symbol(symbol: str) -> List[Tuple[str,str]]:
    out: List[Tuple[str,str]] = []
    data_by_tf: Dict[int, List[Dict[str,Any]]] = {}
    dem_by_tf: Dict[int, List[float]] = {}

    for tf in INTERVALS_MIN:
        try:
            kl = fetch_klines(symbol, tf, limit=max(DEM_LEN+50,300))
            if len(kl) < DEM_LEN+2: continue
            data_by_tf[tf] = kl
            dem_by_tf[tf] = demarker([x["h"] for x in kl],[x["l"] for x in kl], DEM_LEN)
        except Exception as e:
            logging.error("%s %s fetch/analyze failed: %s", symbol, tf, e)

    if not data_by_tf: return out

    def tf_name(m:int)->str: return "4H" if m==240 else "1D" if m==1440 else f"{m}m"

    status: Dict[int, Dict[str,Any]] = {}
    for tf, kl in data_by_tf.items():
        dems = dem_by_tf[tf]
        if not dems or math.isnan(dems[-2]): continue
        prev_candle = kl[-3] if len(kl)>=3 else kl[-2]
        last = kl[-2]
        zone = "OB" if dems[-2] >= OB else "OS" if dems[-2] <= OS else "NEUTRAL"
        patterns = detect_patterns(prev_candle, last)
        status[tf] = {"ts": last["t"], "dem": dems[-2], "zone": zone, "patterns": patterns}

    # Индивидуальные сигналы (как вчера: по зонам DeMarker)
    for tf, st in status.items():
        if st["zone"] == "OB":
            side = "SELL"
        elif st["zone"] == "OS":
            side = "BUY"
        else:
            continue
        pats = f"\nPatterns: {', '.join(st['patterns'])}" if st["patterns"] else ""
        msg = (f"🔔 {symbol} {side} — {tf_name(tf)}\n"
               f"DeM({DEM_LEN})={st['dem']:.3f} [{st['zone']}], OB={OB:.2f}/OS={OS:.2f}"
               f"{pats}")
        key = f"{symbol}:{tf}:{st['ts']}:{side}"
        out.append((key, msg))

    # Комбинированный (если 4H и 1D совпали) — без изменения логики
    if 240 in status and 1440 in status:
        z4, z1 = status[240]["zone"], status[1440]["zone"]
        if z4 in ("OB","OS") and z4 == z1:
            side = "SELL" if z4=="OB" else "BUY"
            pats4 = ", ".join(status[240]["patterns"]) if status[240]["patterns"] else "-"
            pats1 = ", ".join(status[1440]["patterns"]) if status[1440]["patterns"] else "-"
            msg = (f"⚡ {symbol} {side} — 4H & 1D согласованы\n"
                   f"4H DeM={status[240]['dem']:.3f} [{z4}] | 1D DeM={status[1440]['dem']:.3f} [{z1}]\n"
                   f"4H patterns: {pats4}\n1D patterns: {pats1}")
            key = f"{symbol}:combo:{status[240]['ts']}:{status[1440]['ts']}:{side}"
            out.append((key, msg))

    return out

# ---------------------- MAIN ----------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
    state = load_state(STATE_PATH); sent = state.setdefault("sent", {})
    logging.info("Worker started | TICKERS=%s | CAT=%s | URL=%s", TICKERS, BYBIT_CATEGORY, BYBIT_KLINE_URL)

    while True:
        try:
            for sym in TICKERS:
                try:
                    sigs = analyze_symbol(sym)
                    for key, msg in sigs:
                        if key in sent: continue
                        tg_send(msg)
                        sent[key] = int(time.time())
                        if len(sent) > 5000:
                            # чистим старые
                            for k, _ in sorted(sent.items(), key=lambda x: x[1])[:1000]:
                                sent.pop(k, None)
                except Exception as e:
                    logging.error("%s analyze failed: %s", sym, e)
            save_state(STATE_PATH, state)
        except Exception as e:
            logging.error("Main loop error: %s", e)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()