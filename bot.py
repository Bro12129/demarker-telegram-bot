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
OHLCV_LIMIT      = 300
INTERVAL_SECONDS = 900       # каждые 15 минут
STATE_FILE       = Path("alerts_state.json")

UP = "🟢⬆️"; DOWN = "🔴⬇️"
LGT = "⚡"                  # молния (hard)
# внутри строки помечаем свечной паттерн на конкретном ТФ:
CANDLE = "🕯"

# базовые активы для первичного подбора; далее дополним до ~45 символов
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
    """Подбираем реальные USDT-линейные свопы, убираем дубли, дополняем до ~45."""
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
def _key(sym, tag): return f"{sym}:ANY:{tag}"     # минимизируем болтовню
def is_new(state, sym, tag, bar_iso): return state.get(_key(sym, tag)) != bar_iso
def remember(state, sym, tag, bar_iso): state[_key(sym, tag)] = bar_iso

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

# ─────────── Расширенные свечные паттерны ───────────
def bullish_engulfing(df):
    try:
        p = df.iloc[-3]; c = df.iloc[-2]
        return (p["close"] < p["open"]) and (c["close"] > c["open"]) and \
               (c["close"] >= p["open"]) and (c["open"] <= p["close"])
    except Exception:
        return False

def bearish_engulfing(df):
    try:
        p = df.iloc[-3]; c = df.iloc[-2]
        return (p["close"] > p["open"]) and (c["close"] < c["open"]) and \
               (c["open"] >= p["close"]) and (c["close"] <= p["open"])
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

def shooting_star(df):
    try:
        c = df.iloc[-2]
        body = abs(c["close"] - c["open"]); rng = c["high"] - c["low"]
        lw = min(c["open"], c["close"]) - c["low"]; uw = c["high"] - max(c["open"], c["close"])
        return (body > 0) and (rng > 0) and (uw > body*2.5) and (lw < body)
    except Exception:
        return False

def morning_star(df):
    """Три свечи: 1 — длинная медвежья, 2 — маленькая, 3 — бычья, закрытие > середины свечи 1."""
    try:
        a = df.iloc[-4]; b = df.iloc[-3]; c = df.iloc[-2]
        cond1 = a["close"] < a["open"] and (a["open"] - a["close"]) > 0.003 * a["open"]
        cond2 = abs(b["close"] - b["open"]) < 0.004 * b["open"]
        mid_a = (a["open"] + a["close"]) / 2.0
        cond3 = c["close"] > c["open"] and c["close"] > mid_a
        return cond1 and cond2 and cond3
    except Exception:
        return False

def evening_star(df):
    """Обратная к morning star."""
    try:
        a = df.iloc[-4]; b = df.iloc[-3]; c = df.iloc[-2]
        cond1 = a["close"] > a["open"] and (a["close"] - a["open"]) > 0.003 * a["open"]
        cond2 = abs(b["close"] - b["open"]) < 0.004 * b["open"]
        mid_a = (a["open"] + a["close"]) / 2.0
        cond3 = c["close"] < c["open"] and c["close"] < mid_a
        return cond1 and cond2 and cond3
    except Exception:
        return False

def hanging_man(df):
    """Как hammer, но после роста (нестрогое условие тренда)."""
    try:
        p = df.iloc[-3]; c = df.iloc[-2]
        body = abs(c["close"] - c["open"]); rng = c["high"] - c["low"]
        lw = min(c["open"], c["close"]) - c["low"]; uw = c["high"] - max(c["open"], c["close"])
        trend_up = p["close"] > p["open"]
        return trend_up and (body > 0) and (rng > 0) and (lw > body*2.5) and (uw < body)
    except Exception:
        return False

def bullish_pattern(df):  # покупка
    return bullish_engulfing(df) or hammer(df) or morning_star(df)

def bearish_pattern(df):  # продажа
    return bearish_engulfing(df) or shooting_star(df) or evening_star(df) or hanging_man(df)

# ────────────── HELPERS ───────────────
def fts(iso): return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")

def pack_line(sym, side, tfd):
    """Собираем компактную строку: ⚡🟢/🔴 SYMBOL | 4h 0.26🕯 1d 0.31 | price | ts"""
    arrow = UP if side == "BUY" else DOWN
    parts = []
    for tf in ["4h", "1d"]:
        if tf in tfd:
            dem = tfd[tf]["dem"]
            mark = ""
            if side == "BUY" and tfd[tf]["bull"]: mark = CANDLE
            if side == "SELL" and tfd[tf]["bear"]: mark = CANDLE
            parts.append(f"{tf} {dem:.2f}{mark}")
    price_anchor = tfd["1d"]["price"] if "1d" in tfd else tfd["4h"]["price"]
    time_anchor  = tfd["1d"]["bar_iso"] if "1d" in tfd else tfd["4h"]["bar_iso"]
    return f"{LGT}{arrow} {sym} | " + " ".join(parts) + f" | {price_anchor:.4f} | {fts(time_anchor)}"

# ────────────── SCAN & SEND ───────────
def scan_once_and_notify():
    state = load_state()

    # собираем per-symbol/per-tf сводку
    summary = {}   # symbol -> {tf: {"dem":..., "bull":bool, "bear":bool, "price":..., "bar_iso":...}}
    for sym in SYMBOLS:
        per_tf = {}
        for tf in TIMEFRAMES:
            try:
                df = fetch_ohlcv_df(sym, tf)
                df = add_demarker(df)
                last = df.iloc[-2]   # подтверждённая свеча
                dem = float(last["demarker"])
                price = float(last["close"])
                bar_iso = last["time"].replace(tzinfo=timezone.utc).isoformat()
                per_tf[tf] = {
                    "dem": dem,
                    "bull": bullish_pattern(df),
                    "bear": bearish_pattern(df),
                    "price": price,
                    "bar_iso": bar_iso
                }
            except Exception as e:
                print(f"⚠️ {sym} {tf}: {e}")
        if per_tf:
            summary[sym] = per_tf

    lines = []

    for sym, tfd in summary.items():
        has4 = "4h" in tfd; has1 = "1d" in tfd
        if not (has4 or has1):
            continue

        # BUY-кандидаты (считаем флаги)
        buy_flags = []
        if has4 and tfd["4h"]["dem"] <= OVERSOLD: buy_flags.append(("4h","dem"))
        if has1 and tfd["1d"]["dem"] <= OVERSOLD: buy_flags.append(("1d","dem"))
        if has4 and tfd["4h"]["bull"]: buy_flags.append(("4h","candle"))
        if has1 and tfd["1d"]["bull"]: buy_flags.append(("1d","candle"))

        # SELL-кандидаты
        sell_flags = []
        if has4 and tfd["4h"]["dem"] >= OVERBOUGHT: sell_flags.append(("4h","dem"))
        if has1 and tfd["1d"]["dem"] >= OVERBOUGHT: sell_flags.append(("1d","dem"))
        if has4 and tfd["4h"]["bear"]: sell_flags.append(("4h","candle"))
        if has1 and tfd["1d"]["bear"]: sell_flags.append(("1d","candle"))

        # BUY: обязательно хотя бы один DeM в зоне + суммарно флагов >= 2
        buy_zone = any(k=="dem" for _,k in buy_flags)
        if buy_zone and len(buy_flags) >= 2:
            tag = "BUY2"
            bar_iso = (tfd["1d"]["bar_iso"] if has1 else tfd["4h"]["bar_iso"])
            if is_new(state, sym, tag, bar_iso):
                remember(state, sym, tag, bar_iso)
                lines.append(pack_line(sym, "BUY", tfd))

        # SELL: обязательно хотя бы один DeM в зоне + суммарно флагов >= 2
        sell_zone = any(k=="dem" for _,k in sell_flags)
        if sell_zone and len(sell_flags) >= 2:
            tag = "SELL2"
            bar_iso = (tfd["1d"]["bar_iso"] if has1 else tfd["4h"]["bar_iso"])
            if is_new(state, sym, tag, bar_iso):
                remember(state, sym, tag, bar_iso)
                lines.append(pack_line(sym, "SELL", tfd))

    if lines:
        msg = "\n".join(lines)
        # порежем по лимиту Telegram
        chunks = [msg[i:i+3800] for i in range(0, len(msg), 3800)]
        for c in chunks:
            bot.send_message(chat_id=CHAT_ID, text=c)
        save_state(state)
        print(f"✅ Sent {len(lines)} hard lines (min-2 rules)")
    else:
        print("ℹ️ Новых сигналов (min-2) нет")

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