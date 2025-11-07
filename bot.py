# bot.py — DeMarker 28h (мульти-чаты + фикс уведомлений/логов)
import os, time, json, logging, requests
from typing import List, Dict, Optional

# ============ CONFIG ============
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")  # можно: "-1001234567890,@mychannel,123456789"
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN  = 28
DEM_OB   = 0.70
DEM_OS   = 0.30
POLL_SECONDS = 60  # 1 минута для диагностики

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
log = logging.getLogger("bot")

# ============ STATE ============
def load_state(path: str) -> Dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}}

def save_state(path: str, data: Dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass

STATE = load_state(STATE_PATH)

# ============ TG HELPERS ============
def _chat_ids() -> List[str]:
    """
    Поддержка нескольких получателей:
    TELEGRAM_CHAT_ID="-100123...,@mychannel,123456789"
    Допускается numeric (с минусом для супергрупп/каналов) и @username каналов.
    """
    raw = (TELEGRAM_CHAT or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

def tg_send_raw(text: str):
    if not TELEGRAM_TOKEN:
        log.info("TG skipped: missing TELEGRAM_BOT_TOKEN")
        return
    chats = _chat_ids()
    if not chats:
        log.info("TG skipped: missing TELEGRAM_CHAT_ID")
        return
    for cid in chats:
        try:
            r = requests.post(
                f"{TG_API}/sendMessage",
                json={"chat_id": cid, "text": text, "disable_notification": True},
                timeout=10
            )
            if r.status_code != 200:
                log.info(f"TG error [{cid}]: {r.status_code} {r.text}")
            else:
                log.info(f"TG ok -> {cid}")
        except Exception as e:
            log.info(f"TG exception [{cid}]: {e}")

def tg_ping(msg="💰 Нагибатор-достигатор активен. Генератор бабла запущен!"):
    try:
        tg_send_raw(msg)
    except Exception as e:
        log.info(f"TG ping failed: {e}")

# ============ SYMBOLS ============
YF_SYMBOLS = [
    # === CRYPTO ===
    "BTC-USD","ETH-USD","SOL-USD","BNB-USD","XRP-USD","ADA-USD","DOGE-USD","AVAX-USD","DOT-USD","LINK-USD",
    "LTC-USD","MATIC-USD","TON-USD","ATOM-USD","NEAR-USD","FIL-USD","AAVE-USD","XMR-USD","LDO-USD","INJ-USD",
    "APT-USD","SUI-USD","ARB-USD","OP-USD","PЕPE-USD","SHIB-USD".replace("PЕ","PE"),  # страховка от кириллицы в тикере
    # === COMMODITIES ===
    "GC=F","SI=F","CL=F","NG=F","HG=F","PL=F","PA=F",
    # === FX ===
    "EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","NZDUSD=X","USDCAD=X","USDCHF=X","EURJPY=X","GBPJPY=X",
    # === INDICES ===
    "^GSPC","^NDX","^DJI","^RUT","^VIX","^FTSE","^GDAXI","^FCHI","^STOXX50E","^HSI","^N225","^AORD","^SPTSX","^BSESN","^SHCOMP",
    # === TOP S&P500 ===
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK-B","AVGO","JNJ","JPM","V","MA","UNH","HD","LLY","XOM","KO","PEP"
]

INDEX_MAP = {
    "^GSPC":"US500","^NDX":"US100","^DJI":"US30","^RUT":"US2000","^VIX":"VIX",
    "^FTSE":"UK100","^GDAXI":"DE40","^FCHI":"FR40","^STOXX50E":"EU50","^HSI":"HK50",
    "^N225":"JP225","^AORD":"AU200","^SPTSX":"CA60","^BSESN":"IN50","^SHCOMP":"CN50"
}
COMMO_MAP = {
    "GC=F":"XAU-USDT","SI=F":"XAG-USDT","PL=F":"XPT-USDT","PA=F":"XPD-USDT",
    "CL=F":"CL-USDT","NG=F":"NG-USDT","HG=F":"HG-USDT"
}

def norm_name(sym: str) -> str:
    if sym in INDEX_MAP: return INDEX_MAP[sym]
    if sym in COMMO_MAP: return COMMO_MAP[sym]
    if sym.endswith("=X") and len(sym) >= 7:
        pair = sym[:-2]; base, quote = pair[:3], pair[3:]
        return f"{base}-{quote}".upper()
    s = sym.replace("^","")
    if s.endswith("-USD"): return s[:-4] + "-USDT"
    if "-" not in s and not s.endswith("-USDT"): s = s + "-USDT"
    return s.upper()

def format_signal(symbol: str, sig: str, zone: Optional[str]) -> str:
    arrow = "🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status = "⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    return f"{symbol} {arrow}{status}"

# ============ FETCH ============
HEADERS = {"User-Agent": "Mozilla/5.0"}
def fetch_yahoo_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": "180d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200: return None
        chart = r.json().get("chart", {})
        results = chart.get("result", [])
        if not results: return None
        j = results[0]
        ts = j.get("timestamp") or []
        ind = j.get("indicators", {}).get("quote", [])
        if not ts or not ind: return None
        q = ind[0]
        opens, highs, lows, closes = q.get("open"), q.get("high"), q.get("low"), q.get("close")
        if not (opens and highs and lows and closes): return None
        out=[]
        for i in range(min(len(ts), len(opens), len(highs), len(lows), len(closes))):
            try:
                o = float(opens[i]); h = float(highs[i]); l = float(lows[i]); c = float(closes[i])
                if h <= 0 or l <= 0: continue
                out.append([int(ts[i]), o, h, l, c])
            except Exception:
                continue
        return out if out else None
    except Exception:
        return None

# ============ INDICATORS ============
def demarker_series(ohlc: List[List[float]], length: int) -> Optional[List[Optional[float]]]:
    if not ohlc or len(ohlc) < length + 2: return None
    highs=[x[2] for x in ohlc]; lows=[x[3] for x in ohlc]
    up=[0.0]; dn=[0.0]
    for i in range(1,len(ohlc)):
        up.append(max(highs[i]-highs[i-1],0.0))
        dn.append(max(lows[i-1]-lows[i],0.0))
    def sma(a,i,n): return sum(a[i-n+1:i+1])/n
    dem=[None]*len(ohlc)
    for i in range(length,len(ohlc)):
        u=sma(up,i,length); d=sma(dn,i,length)
        dem[i]=u/(u+d) if (u+d)!=0 else 0.5
    return dem

def last_closed(series):
    if not series or len(series) < 2: return None
    i = len(series) - 2
    while i >= 0 and series[i] is None: i -= 1
    return series[i] if i >= 0 else None

def zone_of(v):
    if v is None: return None
    if v >= DEM_OB: return "OB"
    if v <= DEM_OS: return "OS"
    return None

def wick_ge_body_pct(ohlc, idx, pct=0.25):
    if not ohlc or not (-len(ohlc) <= idx < len(ohlc)): return False
    o,h,l,c = ohlc[idx][1:5]; body = abs(c - o)
    if body <= 1e-12: return False
    upper = h - max(o, c); lower = min(o, c) - l
    return (upper >= pct*body) or (lower >= pct*body)

def engulfing_with_prior(ohlc, idx):
    if len(ohlc) < 4: return False
    o0,h0,l0,c0 = ohlc[idx][1:5]; o1,h1,l1,c1 = ohlc[idx-1][1:5]
    o2,c2 = ohlc[idx-2][1], ohlc[idx-2][4]; o3,c3 = ohlc[idx-3][1], ohlc[idx-3][4]
    bull0 = c0 >= o0; bull2 = c2 >= o2; bull3 = c3 >= o3
    if bull0:
        return (not bull2 and not bull3) and (min(o0,c0) <= min(o1,c1)) and (max(o0,c0) >= max(o1,c1))
    else:
        return (bull2 and bull3) and (min(o0,c0) <= min(o1,c1)) and (max(o0,c0) >= max(o1,c1))

def candle_pattern(ohlc):
    if not ohlc or len(ohlc) < 4: return False
    return wick_ge_body_pct(ohlc, -2, 0.25) or engulfing_with_prior(ohlc, -2)

# ============ CORE ============
def process_symbol(sym: str) -> bool:
    triggered = False
    try:
        k4 = fetch_yahoo_klines(sym, "4h"); k1 = fetch_yahoo_klines(sym, "1d")
        if not k4 or not k1: return False
        d4 = demarker_series(k4, DEM_LEN); d1 = demarker_series(k1, DEM_LEN)
        if not d4 or not d1: return False
        v4 = last_closed(d4); v1 = last_closed(d1)
        z4 = zone_of(v4); z1 = zone_of(v1)

        if (z4 and z1 and z4 == z1):
            sig = "L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
            key = f"{sym}|{sig}|{z4}|{k1[-2][0]}"
            if not STATE["sent"].get(key):
                tg_send_raw(format_signal(norm_name(sym), sig, z4))
                STATE["sent"][key] = int(time.time())
                triggered = True
        elif (z4 and not z1) or (z1 and not z4):
            if z4 and candle_pattern(k4): z, tf = z4, "4H"
            elif z1 and candle_pattern(k1): z, tf = z1, "1D"
            else: return False
            key = f"{sym}|1TF+CAN|{z}|{tf}|{k1[-2][0]}"
            if not STATE["sent"].get(key):
                tg_send_raw(format_signal(norm_name(sym), "1TF+CAN", z))
                STATE["sent"][key] = int(time.time())
                triggered = True
    except Exception as e:
        log.info(f"ERR {sym}: {e}")
    return triggered

def main():
    tg_ping()  # 💰 Нагибатор-достигатор активен. Генератор бабла запущен!
    log.info(f"INFO: Scan start — {len(YF_SYMBOLS)} symbols, interval {POLL_SECONDS}s.")
    while True:
        total_signals = 0
        for s in YF_SYMBOLS:
            if process_symbol(s): total_signals += 1
            time.sleep(1)
        save_state(STATE_PATH, STATE)
        log.info(f"Cycle done. Signals: {total_signals}. Sleeping {POLL_SECONDS}s.")
        if total_signals == 0:
            tg_send_raw("ℹ️ Пока тишина — бабло в засаде 💤")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()