import os, time, math, requests 
from datetime import datetime, timezone

# ================== CONFIG ==================
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT",
    "DOGEUSDT","TONUSDT","TRXUSDT","LINKUSDT","MATICUSDT","DOTUSDT",
    "AVAXUSDT","SHIBUSDT","LTCUSDT","BCHUSDT","ATOMUSDT","XLMUSDT",
    "APTUSDT","SUIUSDT","ARBUSDT","OPUSDT","NEARUSDT","INJUSDT",
    "RUNEUSDT","AAVEUSDT","EGLDUSDT","FILUSDT","ETCUSDT","UNIUSDT"
]  # 30 тикеров

TIMEFRAMES = ["240", "D"]                  # 240 = 4H, D = 1D
TF_LABEL    = {"240": "4H", "D": "1D"}

DEM_LEN = 28
OB = 0.70                                   # перекупленность -> 🔻
OS = 0.30                                   # перепроданность -> 🔺
SLEEP_SECONDS = 300                         # цикл раз в 5 минут

CATEGORY = "spot"                           # для Bybit v5 (можешь сменить на "linear")
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"

# ================== ENV ==================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TG_CHAT_ID", "").strip()
ADMIN_CHAT = os.getenv("ADMIN_CHAT_ID", TG_CHAT).strip()
HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MINUTES", "0"))

assert TG_TOKEN and TG_CHAT, "Нужны TG_BOT_TOKEN и TG_CHAT_ID"

# ================== HELPERS ==================
def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"[{now()}] TG send err: {e}", flush=True)

def tg_notify(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"[{now()}] TG notify err: {e}", flush=True)

def fetch_klines(symbol: str, tf: str, limit: int = 200):
    """Возвращает (highs, lows) от старых к новым для Bybit v5."""
    r = requests.get(
        BYBIT_KLINE_URL,
        params={"category": CATEGORY, "symbol": symbol, "interval": tf, "limit": str(limit)},
        timeout=20
    )
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit {symbol} {tf}: {data.get('retCode')} {data.get('retMsg')}")
    rows = data["result"]["list"]           # новые -> старые
    rows.reverse()                          # старые -> новые
    highs = [float(x[2]) for x in rows]
    lows  = [float(x[3]) for x in rows]
    return highs, lows

def demarker(highs, lows, length: int):
    n = len(highs)
    deMax = [0.0]*n
    deMin = [0.0]*n
    for i in range(1, n):
        dh = highs[i] - highs[i-1]
        dl = lows[i-1] - lows[i]
        deMax[i] = dh if dh > 0 else 0.0
        deMin[i] = dl if dl > 0 else 0.0

    out = [math.nan]*n
    sMax = 0.0; sMin = 0.0
    for i in range(n):
        sMax += deMax[i]; sMin += deMin[i]
        if i >= length:
            sMax -= deMax[i-length]; sMin -= deMin[i-length]
        if i >= length:
            denom = sMax + sMin
            out[i] = (sMax/denom) if denom > 0 else math.nan
    return out

def zone(v: float) -> str:
    if math.isnan(v): return "mid"
    if v >= OB: return "ob"   # перекупленность -> 🔻
    if v <= OS: return "os"   # перепроданность -> 🔺
    return "mid"

# чтобы не дублировать сигналы
last_zone = {}  # ключ: (tf, symbol) -> "ob"/"os"/"mid"

# ================== CORE ==================
def run_cycle():
    for tf in TIMEFRAMES:
        hits = []  # сообщения по данному ТФ
        for sym in SYMBOLS:
            try:
                highs, lows = fetch_klines(sym, tf, limit=max(DEM_LEN+50, 120))
                if len(highs) < DEM_LEN + 2:
                    continue
                dem = demarker(highs, lows, DEM_LEN)
                # последняя ЗАКРЫТАЯ свеча — предпоследний элемент
                v_now = dem[-2]
                v_prev = dem[-3] if len(dem) >= 3 else math.nan

                z_now = zone(v_now)
                z_prev = zone(v_prev)
                key = (tf, sym)
                was = last_zone.get(key, "mid")
                last_zone[key] = z_now

                enter_ob = (z_prev != "ob" and z_now == "ob")  # вход в перекупленность
                enter_os = (z_prev != "os" and z_now == "os")  # вход в перепроданность

                if enter_ob and was != "ob":
                    hits.append(f"🔻 <b>{sym}</b> {v_now:.3f}")  # красная стрелка вниз — OB
                if enter_os and was != "os":
                    hits.append(f"🔺 <b>{sym}</b> {v_now:.3f}")  # зелёная стрелка вверх — OS

            except Exception as e:
                print(f"[{now()}] ERR {sym} {tf}: {e}", flush=True)

        if hits:
            tg_send(f"📊 DeMarker(28) — <b>{TF_LABEL[tf]}</b>\n" + "\n".join(hits))

def main_loop():
    tg_notify("✅ Старт бота. Таймфреймы: 4H и 1D. Сигналы: 🔻 OB (≥0.70), 🔺 OS (≤0.30).")
    last_heartbeat = time.time()
    while True:
        run_cycle()
        if HEARTBEAT_MIN and (time.time() - last_heartbeat) >= HEARTBEAT_MIN * 60:
            tg_notify("✅ Бот активен.")
            last_heartbeat = time.time()
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    while True:
        try:
            main_loop()
        except Exception as e:
            # авто-рестарт после любой ошибки
            tg_notify(f"❌ Критическая ошибка: {type(e).__name__}: {e}\nПерезапускаюсь через 15 сек.")
            time.sleep(15)
            continue