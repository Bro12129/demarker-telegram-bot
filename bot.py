# --- SETTINGS HOTFIX ---
USE_CLOSED_ONLY = True
CATEGORY = os.getenv("BYBIT_CATEGORY", "linear")  # linear|inverse|spot
TF_4H = "240"
TF_1D = "D"
MAX_RETRIES_TG = 3

# --- SAFE TELEGRAM SENDER ---
def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.error("Telegram creds missing")
        return False
    for i in range(MAX_RETRIES_TG):
        try:
            r = requests.post(TG_API, json={"chat_id": CHAT_ID, "text": text})
            if r.status_code == 200:
                return True
            logging.warning(f"TG send {r.status_code}: {r.text}")
            # 429 backoff
            time.sleep(1 + i * 2)
        except Exception as e:
            logging.exception(f"TG send err: {e}")
            time.sleep(1 + i * 2)
    return False

# --- BYBIT KLINES (всегда берём закрытую) ---
def bybit_klines(symbol: str, interval: str, limit: int = 200):
    url = f"{BYBIT_KLINE_URL}"
    # Пример: https://api.bybit.com/v5/market/kline
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": str(max(2, limit))
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json().get("result", {}).get("list", [])
    data = sorted(data, key=lambda x: int(x[0]))  # сорт по open time
    if USE_CLOSED_ONLY and len(data) >= 2:
        data = data[:-1]  # срезаем текущую незакрытую
    return data  # элементы формата [openTime, open, high, low, close, volume, ...] (строки)

# --- DEMARKER 28 на закрытых барах ---
def calc_demarker(closes, highs, lows, length=DEM_LEN):
    up, dn = [], []
    for i in range(1, len(closes)):
        up.append(max(0.0, float(highs[i]) - float(highs[i-1])))
        dn.append(max(0.0, float(lows[i-1]) - float(lows[i])))
    # выравниваем длины
    n = min(len(up), len(dn))
    up, dn = up[-n:], dn[-n:]
    dem = []
    for i in range(length, n):
        su = sum(up[i-length:i])
        sd = sum(dn[i-length:i])
        denom = (su + sd) if (su + sd) > 0 else 1e-12
        dem.append(su / denom)
    return dem  # массив по закрытым барам (без последнего незакрытого)

# --- ПИН-БАР/ФИТИЛИ (на закрытом баре) ---
def is_pinbar(o, h, l, c, body_ratio=0.33, wick_ratio=2.0):
    o, h, l, c = map(float, (o, h, l, c))
    body = abs(c - o)
    range_ = max(1e-12, h - l)
    upper = h - max(c, o)
    lower = min(c, o) - l
    # маленькое тело и длинный один фитиль
    if body / range_ > body_ratio:
        return False
    return (upper >= wick_ratio * body) or (lower >= wick_ratio * body)

def detect_candle_signal(o, h, l, c):
    # ↑ зелёная стрелка при бычьем пин-баре, ↓ при медвежьем
    if is_pinbar(o, h, l, c):
        if float(c) > float(o):
            return "🟢⬆️"   # buy-hint
        else:
            return "🔴⬇️"   # sell-hint
    return ""

# --- ДЕДУП КЛЮЧ (символ+TF+время бара+тип) ---
def make_key(symbol: str, tf: str, bar_open_ms: int, kind: str):
    return f"{symbol}|{tf}|{bar_open_ms}|{kind}"

# --- СИГНАЛЫ ---
def evaluate_symbol(symbol: str):
    out_messages = []

    # 4H
    k4 = bybit_klines(symbol, TF_4H, limit=DEM_LEN+50)
    if len(k4) < DEM_LEN+2:
        return out_messages
    open_ms_4 = int(k4[-1][0])
    o4, h4, l4, c4 = k4[-1][1], k4[-1][2], k4[-1][3], k4[-1][4]
    closes4 = [x[4] for x in k4]
    highs4  = [x[2] for x in k4]
    lows4   = [x[3] for x in k4]
    dem4 = calc_demarker(closes4, highs4, lows4, DEM_LEN)
    dem4_last = dem4[-1]

    # 1D
    kd = bybit_klines(symbol, TF_1D, limit=DEM_LEN+50)
    if len(kd) < DEM_LEN+2:
        return out_messages
    open_ms_d = int(kd[-1][0])
    od, hd, ld, cd = kd[-1][1], kd[-1][2], kd[-1][3], kd[-1][4]
    closesd = [x[4] for x in kd]
    highsd  = [x[2] for x in kd]
    lowsd   = [x[3] for x in kd]
    demd = calc_demarker(closesd, highsd, lowsd, DEM_LEN)
    demd_last = demd[-1]

    # свечные сигналы (только закрытые свечи)
    candle4 = detect_candle_signal(o4, h4, l4, c4)
    candled = detect_candle_signal(od, hd, ld, cd)

    # базовые сигналы по DeM
    sig4 = "🟢⬆️" if dem4_last <= OS else ("🔴⬇️" if dem4_last >= OB else "")
    sigd = "🟢⬆️" if demd_last <= OS else ("🔴⬇️" if demd_last >= OB else "")

    # ⚡ если 4H и 1D в одной зоне (обе выше OB или обе ниже OS)
    lightning = ""
    if (dem4_last >= OB and demd_last >= OB) or (dem4_last <= OS and demd_last <= OS):
        lightning = "⚡"

    # комбинируем по твоему правилу «минимум два сигнала»
    candidates = []
    # 4H пакет
    pack4 = [x for x in [sig4, candle4] if x]
    if len(pack4) >= 2:
        candidates.append(("4H", pack4))
    # 1D пакет
    packd = [x for x in [sigd, candled] if x]
    if len(packd) >= 2:
        candidates.append(("1D", packd))
    # ⚡ отдельный
    if lightning:
        candidates.append(("⚡", [lightning]))

    # отправка с дедупом
    state = load_state()  # твоя функция чтения json
    changed = False
    for tf, tokens in candidates:
        # время ключа — время закрытой свечи соответствующего TF
        bar_time = open_ms_4 if tf in ("4H", "⚡") else open_ms_d
        kind = "".join(tokens)
        key = make_key(symbol, tf, bar_time, kind)
        if key not in state:
            # сообщение без слов — только символы
            text = f"{symbol} {''.join(tokens)}"
            if tg_send(text):
                state[key] = int(time.time())
                changed = True
    if changed:
        save_state(state)  # твоя функция записи json

    return out_messages