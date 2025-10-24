# DeMarker(28) screener → Telegram (hard mode: only numbers & symbols)
# Bybit linear USDT swaps, TF: 4h & 1d. Python 3.10–3.13, без imghdr.
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
BOT_TOKEN = (os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
             or os.getenv("BOT_TOKEN"))
CHAT_ID   = (os.getenv("TG_CHAT_ID")   or os.getenv("TELEGRAM_CHAT_ID")
             or os.getenv("CHAT_ID"))
if not BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN/BOT_TOKEN не задан")
if not CHAT_ID:
    raise RuntimeError("TG_CHAT_ID/TELEGRAM_CHAT_ID/CHAT_ID не задан")
bot = Bot(token=BOT_TOKEN)

# ─────────────── CONFIG ───────────────
TIMEFRAMES       = ["4h", "1d"]
DEM_PERIOD       = 28
OVERSOLD         = 0.30
OVERBOUGHT       = 0.70
EXT_OVERSOLD     = 0.20     # «жёсткие» экстремы
EXT_OVERBOUGHT   = 0.80
OHLCV_LIMIT      = 300
INTERVAL_SECONDS = 900       # каждые 15 минут
STATE_FILE       = Path("alerts_state.json")

UP = "🟢⬆️"; DOWN = "🔴⬇️"
LGT = "⚡"; DLGT = "⚡⚡"      # одиночная/двойная молния
CANDLE = "🕯"                 # используется только внутр. логикой

# базовые активы для первичного подбора; далее дополним до ~45 символов
DESIRED_BASES = [
    "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","TON","TRX","DOT","AVAX","MATIC","LINK","LTC",
    "BCH","ATOM","XMR","APT","ARB","OP","NEAR","FIL","ETC","ICP","SUI","HBAR","UNI","TIA","XLM",
    "XAU","GOLD","SPX","SP500","NAS100","NDX","DJI","SILVER","XAG"
]

# ───────────── EXCHANGE ───────────────
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})
markets = exchange.load_markets()

def resolve_symbols(desired_bases):
    syms = []
    for m in markets.values():
        if not m.get("swap"):
            continue
        if m.get("linear") is False:
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

# ─────────── Candle patterns (минимум) ───────────
def bullish_engulfing(df):
    try:
        p = df.iloc[-3]; c = df.iloc[-2]
        return (p["close"] < p["open"]) and (c["close"] > c["open"]) and (c["close"] >= p["open"]) and (c["open"] <= p["close"])
    except Exception:
        return False

def hammer(df):
    try:
        c = df.iloc[-2]
        body = abs(c["close"] - c["open"]); rng = c["high"] - c["low"]
        lw = min(c["open"], c["close"]) - c["low"]; uw = c["high"] - max(c["open"], c["close"])
        return (body > 0) and (rng > 0) and (lw > body*2.5) and (uw < body)
    except Exception:
        return False

def bearish_engulfing(df):
    try:
        p = df.iloc[-3]; c = df.iloc[-2]
        return (p["close"] > p["open"]) and (c["close"] < c["open"]) and (c["open"] >= p["close"]) and (c["close"] <= p["open"])
    except Exception:
        return False

def shooting_star(df):
    try:
        c = df.iloc[-2]
        body = abs(c["close"] - c["open"]); rng = c["high"] - c["low"]
        lw = min(c["open"], c["close"]) - c["low"]; uw = c["high"] - max(c["open"], c["close"])
        return (body > 0) and (rng > 0) and (uw > body*2.5) and (lw < body)
    except Exception:
        return False

def bullish_pattern(df):  # сигнал покупки
    return bullish_engulfing(df) or hammer(df)

def bearish_pattern(df):  # сигнал продажи
    return bearish_engulfing(df) or shooting_star(df)

# ────────────── SIGNALS (per TF) ───────────
def tf_signals_for_symbol(symbol, timeframe):
    """
    Возвращает список:
      - COMBO_BUY / COMBO_SELL (жёсткие одиночные)
      - SUMMARY (для кросс-ТФ совпадений и экстремов)
    """
    out = []
    df = fetch_ohlcv_df(symbol, timeframe)
    df = add_demarker(df)

    last = df.iloc[-2]  # подтверждённая свеча
    bar_iso = last["time"].replace(tzinfo=timezone.utc).isoformat()
    dem = float(last["demarker"])
    price = float(last["close"])

    dem_sig = "BUY" if dem <= OVERSOLD else ("SELL" if dem >= OVERBOUGHT else None)
    bull = bullish_pattern(df)
    bear = bearish_pattern(df)

    # одиночный «жёсткий» COMBO
    if dem_sig == "BUY" and bull:
        out.append({"tag":"COMBO_BUY","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})
    if dem_sig == "SELL" and bear:
        out.append({"tag":"COMBO_SELL","symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem})

    # SUMMARY для агрегации
    out.append({
        "tag":"SUMMARY",
        "symbol":symbol,"tf":timeframe,"bar_iso":bar_iso,"price":price,"dem":dem,
        "dem_side": dem_sig, "bull": bull, "bear": bear
    })
    return out

# ────────────── Formatting ─────────────
def fts(iso): return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")

def make_line_single(symbol, tf, side, dem, price, bar_iso):
    arrow = UP if side == "BUY" else DOWN
    return f"{LGT}{arrow} {symbol} {tf} | {dem:.2f} | {price:.4f} | {fts(bar_iso)}"

def make_line_double(symbol, side, dem4, dem1, price, bar_iso):
    arrow = UP if side == "BUY" else DOWN
    return f"{DLGT}{arrow} {symbol} 4h&1d | {dem4:.2f}/{dem1:.2f} | {price:.4f} | {fts(bar_iso)}"

# ────────────── SCAN & SEND ───────────
def scan_once_and_notify():
    state = load_state()

    per_sym = {}     # {sym: {tf: summary}}
    single_lines = []

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                sigs = tf_signals_for_symbol(sym, tf)
                for s in sigs:
                    if s["tag"].startswith("COMBO"):
                        tag = s["tag"]
                        if is_new(state, sym, tf, tag, s["bar_iso"]):
                            remember(state, sym, tf, tag, s["bar_iso"])
                            side = "BUY" if "BUY" in tag else "SELL"
                            single_lines.append(make_line_single(sym, tf, side, s["dem"], s["price"], s["bar_iso"]))
                    elif s["tag"] == "SUMMARY":
                        per_sym.setdefault(sym, {})[tf] = s
            except Exception as e:
                print(f"⚠️ {sym} {tf}: {e}")

    # кросс-ТФ совпадения + экстремы
    double_lines = []
    for sym, tfd in per_sym.items():
        if "4h" in tfd and "1d" in tfd:
            s4, s1 = tfd["4h"], tfd["1d"]
            # Совпадение направления по DeMarker (BUY/SELL)
            if s4["dem_side"] and s4["dem_side"] == s1["dem_side"]:
                tag = f"DOUBLE_{s4['dem_side']}"
                bar_iso = s1["bar_iso"]  # дневка как якорь
                if is_new(state, sym, "4h&1d", tag, bar_iso):
                    remember(state, sym, "4h&1d", tag, bar_iso)
                    double_lines.append(
                        make_line_double(sym, s4["dem_side"], s4["dem"], s1["dem"], s1["price"], bar_iso)
                    )
            # Экстремальные уровни как отдельный «жёсткий» триггер
            if (s4["dem"] <= EXT_OVERSOLD or s4["dem"] >= EXT_OVERBOUGHT or
                s1["dem"] <= EXT_OVERSOLD or s1["dem"] >= EXT_OVERBOUGHT):
                side = "BUY" if (s4["dem"]<=EXT_OVERSOLD or s1["dem"]<=EXT_OVERSOLD) else "SELL"
                tag = f"EXT_{side}"; bar_iso = s1["bar_iso"]
                if is_new(state, sym, "EXT", tag, bar_iso):
                    remember(state, sym, "EXT", tag, bar_iso)
                    # одна молния (отличается от двойного совпадения)
                    line = make_line_double(sym, side, s4["dem"], s1["dem"], s1["price"], bar_iso).replace(DLGT, LGT)
                    double_lines.append(line)

    # Итог: отправляем ТОЛЬКО «жёсткие» сигналы
    lines = double_lines + single_lines  # двойные — первыми
    if lines:
        msg = "\n".join(lines)
        chunks = [msg[i:i+3800] for i in range(0, len(msg), 3800)]
        for c in chunks:
            bot.send_message(chat_id=CHAT_ID, text=c)
        save_state(load_state() | {**load_state()})  # no-op write safety
        print(f"✅ Sent {len(lines)} hard lines")
    else:
        print("ℹ️ Новых жёстких сигналов нет")

# ───────────────── MAIN LOOP ───────────
def main():
    print("🚀 DeMarker bot (hard mode) started")
    print(f"Symbols ({len(SYMBOLS)}): {', '.join(SYMBOLS[:15])}{' ...' if len(SYMBOLS)>15 else ''}")
    while True:
        print(f"⏱  Scan at {datetime.utcnow().isoformat()}Z")
        scan_once_and_notify()
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()