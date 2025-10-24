# DeMarker(28) screener 4h + 1d → Telegram
# Перпетуалы (swap) Bybit/USDT. Render/Py3.13 совместимо. Без imghdr.
import os, time, json
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import ccxt
from dotenv import load_dotenv
from telegram import Bot

# ───────────────── ENV ─────────────────
load_dotenv()
BOT_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("TG_CHAT_ID")   or os.getenv("TELEGRAM_CHAT_ID")   or os.getenv("CHAT_ID")
if not BOT_TOKEN: raise RuntimeError("TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN не задан")
if not CHAT_ID:   raise RuntimeError("TG_CHAT_ID/TELEGRAM_CHAT_ID не задан")
bot = Bot(token=BOT_TOKEN)

# ─────────────── CONFIG ───────────────
TIMEFRAMES       = ["4h", "1d"]       # два ТФ
DEM_PERIOD       = 28
OVERSOLD         = 0.30
OVERBOUGHT       = 0.70
OHLCV_LIMIT      = 300
INTERVAL_SECONDS = 900                # цикл каждые 15 минут
STATE_FILE       = Path("alerts_state.json")

UP = "🟢⬆️"; DOWN = "🔴⬇️"; CANDLE = "🕯"

# Желаемые базы (подбираем реальные своп-символы автоматически)
DESIRED_BASES = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","TRX","DOT","AVAX","MATIC","LINK","LTC",
    "BCH","ATOM","XMR","APT","ARB","OP","NEAR","FIL","ETC","ICP","SUI","HBAR","UNI","TIA","XLM",
    "XAU","GOLD","SPX","SP500","NAS100","NDX","DJI","SILVER","XAG"
]

# ───────────── EXCHANGE ───────────────
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}   # перпетуалы
})
markets = exchange.load_markets()

def resolve_symbols(desired_bases):
    syms = []
    for m in markets.values():
        if not m.get("swap"):                 # только перпы
            continue
        if m.get("linear") is False:          # предпочитаем линейные USDT
            continue
        base = str(m.get("base","")).upper()
        symbol = m.get("symbol","")
        for want in desired_bases:
            w = want.upper()
            if base == w or w in symbol.upper():
                syms.append(symbol); break
    uniq = []
    for s in syms:
        if s not in uniq: uniq.append(s)
    if len(uniq) < 30:
        extra = [m["symbol"] for m in markets.values()
                 if m.get("swap") and m.get("linear") and "/USDT" in m["symbol"]]
        for s in extra:
            if s not in uniq: uniq.append(s)
            if len(uniq) >= 45: break
    return uniq[:45]

SYMBOLS = resolve_symbols(DESIRED_BASES)

# ─────────────── STATE ────────────────
def load_state():
    if not STATE_FILE.exists(): return {}
    try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception: return {}

def save_state(state): STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
def _key(sym, tf, tag): return f"{sym}:{tf}:{tag}"
def is_new(state, sym, tf, tag, bar_iso): return state.get(_key(sym, tf, tag)) != bar_iso
def remember(state, sym, tf, tag, bar_iso): state[_key(sym, tf, tag)] = bar_iso

# ─────────── DATA & INDICATORS ─────────
def fetch_ohlcv_df(symbol, timeframe):
    data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=OHLCV_LIMIT)
    df = pd.DataFrame(data, columns=["time","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df

def add_demarker(df, period=DEM_PERIOD):
    h, l = df["high"], df["low"]
    demax = np.where(h > h.shift(1), h - h.shift(1), 0.0)
    demin = np.where(l < l.shift(1), l.shift(1) - l, 0.0)
    a = pd.Series(demax, index=df.index).rolling(period).mean()
    b = pd.Series(demin, index=df.index).rolling(period).mean()
    df["demarker"] = a / (a + b)
    return df

# ─────────── Candle patterns ───────────
def bullish_engulfing(df):
    p = df.iloc[-3]; c = df.iloc[-2]
    return (p["close"] < p["open"]) and (c["close"] > c["open"]) and (c["close"] >= p["open"]) and (c["open"] <= p["close"])

def hammer(df):
    c = df.iloc[-2]
    body = abs(c["close"] - c["open"]); rng = c["high"] - c["low"]
    lw = min(c["open"], c["close"]) - c["low"]; uw = c["high"] - max(c["open"], c["close"])
    return (body > 0) and (rng > 0) and (lw > body*2.5) and (uw < body)

def bearish_engulfing(df):
    p = df.iloc[-3]; c = df.iloc[-2]
    return (p["close"] > p["open"]) and (c["close"] < c["open"]) and (c["open"] >= p["close"]) and (c["close"] <= p["open"])

def shooting_star(df):
    c = df.iloc[-2]
    body = abs(c["close"] - c["open"]); rng = c["high"] - c["low"]
    lw = min(c["open"], c["close"]) - c["low"]; uw = c["high"] - max(c["open"], c["close"])
    return (body > 0) and (rng > 0) and (uw > body*2.5) and (lw < body)

def bullish_pattern(df):  # для покупок
    try: return bullish_engulfing(df) or hammer(df)
    except Exception: return False

def bearish_pattern(df):  # для продаж
    try: return bearish_engulfing(df) or shooting_star(df)
    except Exception: return False

# ────────────── SIGNAL LOGIC ───────────
def tf_signals_for_symbol(symbol, timeframe):
    """
    Возвращает список словарей сигналов для символа на конкретном ТФ.
    tag: DEM_BUY/DEM_SELL, CANDLE_BUY/CANDLE_SELL, COMBO_BUY/COMBO_SELL
    """
    out = []
    df = fetch_ohlcv_df(symbol, timeframe)
    df = add_demarker(df)

    last = df.iloc[-2]  # подтверждаем по закрытой свече
    bar_time = last["time"]; bar_iso = bar_time.replace(tzinfo=timezone.utc).isoformat()
    dem = float(last["demarker"]); price = float(last["close"])

    dem_sig = "BUY" if dem <= OVERSOLD else ("SELL" if dem >= OVERBOUGHT else None)
    bull_candle = bullish_pattern(df)   # бычьи (молот/поглощение)
    bear_candle = bearish_pattern(df)   # медвежьи (звезда/поглощение)

    # 1) чистый DeMarker
    if dem_sig == "BUY":
        out.append({"tag":"DEM_BUY","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})
    elif dem_sig == "SELL":
        out.append({"tag":"DEM_SELL","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})

    # 2) чистые свечи — независимо от DeMarker, как отдельные сигналы
    if bull_candle:
        out.append({"tag":"CANDLE_BUY","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})
    if bear_candle:
        out.append({"tag":"CANDLE_SELL","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})

    # 3) совместные (комбо): дем и свеча совпали по направлению
    if dem_sig == "BUY" and bull_candle:
        out.append({"tag":"COMBO_BUY","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})
    if dem_sig == "SELL" and bear_candle:
        out.append({"tag":"COMBO_SELL","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})

    return out

def format_line(s):
    ts = datetime.fromisoformat(s["bar_iso"]).strftime("%Y-%m-%d %H:%M UTC")
    sym, tf, dem, price, tag = s["symbol"], s["tf"], s["dem"], s["price"], s["tag"]
    if tag == "DEM_BUY":   return f"{UP} {sym} | TF {tf} | DeM ≤ {OVERSOLD:.2f} (={dem:.2f}) | Close {price:.4f} | {ts}"
    if tag == "DEM_SELL":  return f"{DOWN} {sym} | TF {tf} | DeM ≥ {OVERBOUGHT:.2f} (={dem:.2f}) | Close {price:.4f} | {ts}"
    if tag == "CANDLE_BUY":  return f"{CANDLE}{UP} {sym} | TF {tf} | Bullish candle | DeM {dem:.2f} | {price:.4f} | {ts}"
    if tag == "CANDLE_SELL": return f"{CANDLE}{DOWN} {sym} | TF {tf} | Bearish candle | DeM {dem:.2f} | {price:.4f} | {ts}"
    if tag == "COMBO_BUY":   return f"{CANDLE}{UP} {sym} | TF {tf} | COMBO: Bullish candle + DeM≤{OVERSOLD:.2f} | DeM {dem:.2f} | {price:.4f} | {ts}"
    if tag == "COMBO_SELL":  return f"{CANDLE}{DOWN} {sym} | TF {tf} | COMBO: Bearish candle + DeM≥{OVERBOUGHT:.2f} | DeM {dem:.2f} | {price:.4f} | {ts}"
    return f"{sym} {tf} {tag} | {ts}"

def scan_once_and_notify():
    state = load_state()
    lines = []

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                sigs = tf_signals_for_symbol(sym, tf)
                for s in sigs:
                    if is_new(state, s["symbol"], s["tf"], s["tag"], s["bar_iso"]):
                        remember(state, s["symbol"], s["tf"], s["tag"], s["bar_iso"])
                        lines.append(format_line(s))
            except Exception as e:
                print(f"⚠️ {sym} {tf}: {e}")

    if lines:
        header = "📊 DeMarker(28) 4h/1d — подтверждённые сигналы по закрытым свечам"
        msg = header + "\n\n" + "\n".join(lines)
        # делим на части, чтобы не превысить лимит Telegram
        chunks = [msg[i:i+3800] for i in range(0, len(msg), 3800)]
        for c in chunks:
            bot.send_message(chat_id=CHAT_ID, text=c)
        save_state(state)
        print(f"✅ Sent {len(lines)} lines")
    else:
        print("ℹ️ Новых сигналов нет")

# ───────────────── MAIN LOOP ───────────
def main():
    print("🚀 DeMarker bot started")
    print(f"Symbols ({len(SYMBOLS)}): {', '.join(SYMBOLS[:15])} {'...' if len(SYMBOLS)>15 else ''}")
    while True:
        print(f"⏱  Scan at {datetime.utcnow().isoformat()}Z")
        scan_once_and_notify()
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()