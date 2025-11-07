# bot.py — версия Yahoo DeMarker 28h
import os, time, json, math, logging, requests
from typing import List, Dict, Optional

# ============ CONFIG ============
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEM_LEN  = 28
DEM_OB   = 0.70
DEM_OS   = 0.30
INTERVALS = {"4h": "4h", "1d": "1d"}
POLL_HOURS = 1
POLL_SECONDS = POLL_HOURS * 3600

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

# ============ SYMBOL UNIVERSE ============
YF_SYMBOLS = [
    # === CRYPTO ===
    "BTC-USD","ETH-USD","SOL-USD","BNB-USD","XRP-USD","ADA-USD","DOGE-USD","AVAX-USD","DOT-USD","LINK-USD",
    "LTC-USD","MATIC-USD","TON-USD","ATOM-USD","NEAR-USD","FIL-USD","AAVE-USD","XMR-USD","LDO-USD","INJ-USD",
    "APT-USD","SUI-USD","ARB-USD","OP-USD","PEPE-USD","SHIB-USD",
    # === COMMODITIES ===
    "GC=F","SI=F","CL=F","NG=F","HG=F","PL=F","PA=F",
    # === FX MAJORS ===
    "EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","NZDUSD=X","USDCAD=X","USDCHF=X","EURJPY=X","GBPJPY=X",
    # === INDICES ===
    "^GSPC","^NDX","^DJI","^RUT","^VIX","^FTSE","^GDAXI","^FCHI","^STOXX50E","^HSI","^N225","^AORD","^SPTSX","^BSESN","^SHCOMP",
    # === TOP S&P500 ===
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK-B","AVGO","JNJ","JPM","V","MA","UNH","HD","LLY","XOM","KO","PEP"
]

# нормализация имён для сообщений
def norm_name(sym: str) -> str:
    s = sym.replace("=F","").replace("=X","").replace("^","")
    if "-USD" in s: s = s.replace("-USD","-USDT")
    elif s.endswith("USD"): s = s[:-3] + "-USDT"
    if not s.endswith("-USDT") and not "-" in s:
        s += "-USDT"
    return s.upper()

# ============ TELEGRAM ============
def tg_send_raw(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    try:
        r = requests.post(f"{TG_API}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT, "text": text, "disable_notification": True},
                          timeout=10)
        if r.status_code != 200:
            log.info(f"TG error: {r.text}")
    except Exception as e:
        log.info(f"TG exception: {e}")

def format_signal(symbol: str, sig: str, zone: Optional[str]) -> str:
    arrow = "🟢↑" if zone=="OS" else ("🔴↓" if zone=="OB" else "")
    status = "⚡" if sig=="LIGHT" else ("⚡🕯️" if sig=="L+CAN" else "🕯️")
    return f"{symbol} {arrow}{status}"

# ============ DATA FETCH (Yahoo) ============
HEADERS = {"User-Agent": "Mozilla/5.0"}

def fetch_yahoo_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": "180d"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if r.status_code != 200: return None
        j = r.json().get("chart", {}).get("result", [])[0]
        if not j: return None
        ts = j["timestamp"]; ohlc = j["indicators"]["quote"][0]
        out=[]
        for i in range(len(ts)):
            try:
                out.append([ts[i],
                            float(ohlc["open"][i]),
                            float(ohlc["high"][i]),
                            float(ohlc["low"][i]),
                            float(ohlc["close"][i])])
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
    if not series or len(series)<2: return None
    i=len(series)-2
    while i>=0 and series[i] is None: i-=1
    return series[i] if i>=0 else None

def zone_of(v): 
    if v is None: return None
    if v>=DEM_OB: return "OB"
    if v<=DEM_OS: return "OS"
    return None

def wick_ge_body_pct(ohlc, idx, pct=0.25):
    if not ohlc or not (-len(ohlc)<=idx<len(ohlc)): return False
    o,h,l,c=ohlc[idx][1:5]
    body=abs(c-o)
    if body<=1e-12: return False
    upper=h-max(o,c); lower=min(o,c)-l
    return (upper>=pct*body) or (lower>=pct*body)

def engulfing_with_prior(ohlc, idx):
    if len(ohlc)<4: return False
    o0,h0,l0,c0=ohlc[idx][1:5]; o1,h1,l1,c1=ohlc[idx-1][1:5]
    o2,c2=ohlc[idx-2][1],ohlc[idx-2][4]; o3,c3=ohlc[idx-3][1],ohlc[idx-3][4]
    bull0=c0>=o0; bull2=c2>=o2; bull3=c3>=o3
    if bull0: return (not bull2 and not bull3) and (min(o0,c0)<=min(o1,c1)) and (max(o0,c0)>=max(o1,c1))
    else:     return (bull2 and bull3) and (min(o0,c0)<=min(o1,c1)) and (max(o0,c0)>=max(o1,c1))

def candle_pattern(ohlc): 
    return wick_ge_body_pct(ohlc,-2,0.25) or engulfing_with_prior(ohlc,-2)

# ============ CORE ============
def process_symbol(sym: str):
    try:
        k4=fetch_yahoo_klines(sym,"4h"); k1=fetch_yahoo_klines(sym,"1d")
        if not k4 or not k1: return
        d4=demarker_series(k4,DEM_LEN); d1=demarker_series(k1,DEM_LEN)
        if not d4 or not d1: return
        v4=last_closed(d4); v1=last_closed(d1)
        z4=zone_of(v4); z1=zone_of(v1)
        if (z4 and z1 and z4==z1):
            sig="L+CAN" if (candle_pattern(k4) or candle_pattern(k1)) else "LIGHT"
            key=f"{sym}|{sig}|{z4}|{k1[-2][0]}"
            if not STATE["sent"].get(key):
                tg_send_raw(format_signal(norm_name(sym),sig,z4))
                STATE["sent"][key]=int(time.time())
        elif (z4 and not z1) or (z1 and not z4):
            if z4 and candle_pattern(k4): z,tf=z4,"4H"
            elif z1 and candle_pattern(k1): z,tf=z1,"1D"
            else: return
            key=f"{sym}|1TF+CAN|{z}|{tf}|{k1[-2][0]}"
            if not STATE["sent"].get(key):
                tg_send_raw(format_signal(norm_name(sym),"1TF+CAN",z))
                STATE["sent"][key]=int(time.time())
    except Exception as e:
        log.info(f"ERR {sym}: {e}")

def main():
    log.info(f"INFO: Yahoo scan start — {len(YF_SYMBOLS)} symbols, interval 1h.")
    while True:
        for s in YF_SYMBOLS:
            process_symbol(s)
            time.sleep(1)  # чтобы не банили Yahoo
        save_state(STATE_PATH, STATE)
        log.info("Cycle done. Sleeping 1 hour.")
        time.sleep(POLL_SECONDS)

if __name__=="__main__":
    main()