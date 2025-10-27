# bot.py — финальная версия (~70 тикеров: crypto + metals + indices + stocks)

import os, time, json, logging, requests, re
from typing import List, Dict
from urllib.parse import urlparse

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))
DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
OB             = float(os.getenv("DEM_OB", "0.70"))
OS             = float(os.getenv("DEM_OS", "0.30"))
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

# ===== BYBIT URL =====
def _bybit_base():
    raw = os.getenv("BYBIT_URL", "https://api.bybit.com")
    u = urlparse(raw if "://" in raw else f"https://{raw}")
    return f"{u.scheme or 'https'}://{u.netloc or u.path or 'api.bybit.com'}"

BYBIT_BASE = _bybit_base()
BYBIT_KLINE_URL = f"{BYBIT_BASE}/v5/market/kline"

def fetch_kline(symbol, interval, limit=200, category="linear", timeout=20):
    params = {"category": category, "symbol": symbol, "interval": str(interval), "limit": str(limit)}
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ===== UTIL =====
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except:
            return {}
    return {}

def save_state(state):
    try: json.dump(state, open(STATE_PATH, "w"))
    except Exception as e: logging.error("Save state error: %s", e)

def send_tg(symbol, text):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(TG_API, data={"chat_id": CHAT_ID, "text": f"{symbol} {text}".strip(),
                                    "disable_notification": True}, timeout=10)
    except Exception as e: logging.error("TG send error: %s", e)

# ===== TICKERS (≈70 total) =====
def parse_symbols():
    default = (
        # --- Crypto (40) ---
        "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,PEPEUSDT,"
        "TONUSDT,SHIBUSDT,LTCUSDT,LINKUSDT,TRXUSDT,MATICUSDT,DOTUSDT,APTUSDT,ARBUSDT,"
        "OPUSDT,SUIUSDT,NEARUSDT,ATOMUSDT,SEIUSDT,XLMUSDT,ETCUSDT,INJUSDT,TIAUSDT,"
        "AAVEUSDT,UNIUSDT,MKRUSDT,IMXUSDT,FILUSDT,BLURUSDT,GALAUSDT,THETAUSDT,"
        "ICPUSDT,SANDUSDT,MANAUSDT,FTMUSDT,EOSUSDT,"
        # --- Metals & commodities ---
        "PAXGUSDT,SILVERUSDT,PLATUSDT,PALLUSDT,COPPERUSDT,WTIUSDT,BRENTUSDT,NATGASUSDT,URANIUMUSDT,"
        # --- Indices ---
        "SP500USDT,NAS100USDT,DJ30USDT,RUS2000USDT,DXYUSDT,VIXUSDT,"
        # --- Tokenized stocks ---
        "AAPLUSDT,TSLAUSDT,NVDAUSDT,METAUSDT,MSFTUSDT,GOOGUSDT,AMZNUSDT"
    )
    raw = os.getenv("TICKERS", os.getenv("SYMBOLS", default))
    parts = [p.strip().upper() for p in re.split(r"[,\s]+", raw) if p.strip()]
    return list(dict.fromkeys(parts))

SYMBOLS = parse_symbols()

# ===== DeMarker =====
def demarker_from_candles(candles, length=DEM_LEN):
    if len(candles) < length + 1: return 0.5
    highs = [float(c[2]) for c in candles][- (length + 1):]
    lows  = [float(c[3]) for c in candles][- (length + 1):]
    up = dn = 0.0
    for i in range(1, len(highs)):
        up += max(highs[i] - highs[i-1], 0)
        dn += max(lows[i-1] - lows[i], 0)
    return up / (up + dn) if up + dn else 0.5

def zone(val):
    if val >= OB: return "overbought"
    if val <= OS: return "oversold"
    return "neutral"

# ===== Candle patterns =====
def classify_long_wick_patterns_last_closed(candles):
    if not candles: return []
    o,h,l,c = map(float, candles[-1][1:5])
    body = abs(c - o) or 1e-8; rng = max(h - l, 1e-8)
    upper = max(h - max(o, c), 0.0); lower = max(min(o, c) - l, 0.0)
    b, u, d = body/rng, upper/rng, lower/rng
    SMALL_BODY, LONG_WICK, DOJI_BODY = 0.3, 2.0, 0.1
    labels=[]
    if lower>LONG_WICK*body and c>o: labels.append("bullish_pin")
    if upper>LONG_WICK*body and c<o: labels.append("bearish_pin")
    if b<=SMALL_BODY and lower>=LONG_WICK*body: labels.append("hammer_hanging")
    if b<=SMALL_BODY and upper>=LONG_WICK*body: labels.append("star_inverted")
    if b<=DOJI_BODY:
        if d>=0.6 and u<=0.15: labels.append("dragonfly_doji")
        elif u>=0.6 and d<=0.15: labels.append("gravestone_doji")
        elif u>=0.35 and d>=0.35: labels.append("long_legged_doji")
    return labels

def signals_from_patterns(labels, z):
    out=[]
    for lb in labels:
        if lb in {"bullish_pin","dragonfly_doji"} and z=="oversold": out.append("🟢⬆️")
        elif lb in {"bearish_pin","gravestone_doji"} and z=="overbought": out.append("🔴⬇️")
        elif lb in {"hammer_hanging","star_inverted","long_legged_doji"}:
            if z=="oversold": out.append("🟢⬆️")
            elif z=="overbought": out.append("🔴⬇️")
    return out

# ===== MAIN LOOP =====
state=load_state()
logging.info("Start bot with %d symbols...",len(SYMBOLS))

while True:
    for sym in SYMBOLS:
        try:
            r4=fetch_kline(sym,"240",limit=200,category="linear")
            r1=fetch_kline(sym,"D",limit=200,category="linear")
            kl4=r4.get("result",{}).get("list",[]) or []
            kl1=r1.get("result",{}).get("list",[]) or []
            if len(kl4)<DEM_LEN+1 or len(kl1)<DEM_LEN+1: continue

            d4=demarker_from_candles(kl4); d1=demarker_from_candles(kl1)
            z4,z1=zone(d4),zone(d1)
            labs4=classify_long_wick_patterns_last_closed(kl4)
            labs1=classify_long_wick_patterns_last_closed(kl1)

            k4=f"{sym}:{kl4[-1][0]}:4h"; k1=f"{sym}:{kl1[-1][0]}:1d"
            kL=f"{sym}:{kl4[-1][0]}:{kl1[-1][0]}:lightning"
            parts=[]

            if z4==z1 and z4 in ("overbought","oversold") and state.get(kL)!=1:
                parts.append("⚡️"); state[kL]=1

            for lb in labs4:
                kk=f"{k4}:{lb}"
                if state.get(kk)==1: continue
                for s in signals_from_patterns([lb],z4): parts.append(s); state[kk]=1

            for lb in labs1:
                kk=f"{k1}:{lb}"
                if state.get(kk)==1: continue
                for s in signals_from_patterns([lb],z1): parts.append(s); state[kk]=1

            if parts: send_tg(sym,"".join(parts))

        except requests.HTTPError as e:
            logging.error("%s: HTTP error: %s",sym,e)
        except Exception as e:
            logging.error("%s: %s",sym,e)

    save_state(state)
    time.sleep(POLL_SECONDS)