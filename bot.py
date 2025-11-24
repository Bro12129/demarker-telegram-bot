# bot.py — Bybit + TwelveData (резерв с лимитами)
# Closed candles only, DeMarker(28), pin-bar (wick>=30% с направлением)
# Signals:
#   ⚡        — 4H & 1D same zone + один из 4 спец-паттернов для молнии
#   1TF4H    — зона + свечной паттерн (pin-bar или engulfing) только на 4H
#   1TF1D    — зона + свечной паттерн (pin-bar или engulfing) только на 1D

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

POLL_SECONDS     = 60  # сейчас не используется как раньше, но оставлен для совместимости

BYBIT_BASE       = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BB_KLINES        = f"{BYBIT_BASE}/v5/market/kline"
BB_TIMEOUT       = 15

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
    # индекс обхода плана тикеров для циклического опроса
    if "plan_idx" not in state:
        state["plan_idx"] = 0

STATE = load_state(STATE_PATH)

def _init_td_state():
    if "td_day" not in STATE or "td_count" not in STATE:
        STATE["td_day"] = time.strftime("%Y%m%d", time.gmtime())
        STATE["td_count"] = 0

def _td_can_request() -> bool:
    if not TD_API_KEY:
        return False
    _init_td_state()
    now = time.time()
    ms = TD_RATE["minute_start"]
    if (now - ms) >= 60:
        TD_RATE["minute_start"] = now
        TD_RATE["minute_count"] = 0
    if TD_RATE["minute_count"] >= TD_MINUTE_LIMIT:
        return False
    cur_day = time.strftime("%Y%m%d", time.gmtime())
    if STATE.get("td_day") != cur_day:
        STATE["td_day"] = cur_day
        STATE["td_count"] = 0
    if STATE.get("td_count", 0) >= TD_DAILY_LIMIT:
        return False
    return True

def _td_mark_request():
    now = time.time()
    if TD_RATE["minute_start"] == 0:
        TD_RATE["minute_start"] = now
        TD_RATE["minute_count"] = 0
    TD_RATE["minute_count"] += 1
    cur_day = time.strftime("%Y%m%d", time.gmtime())
    if STATE.get("td_day") != cur_day:
        STATE["td_day"] = cur_day
        STATE["td_count"] = 0
    STATE["td_count"] = STATE.get("td_count", 0) + 1

def _td_parse_time(s: str) -> Optional[int]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except:
            continue
    return None

def fetch_td_candles(symbol: str, interval: str):
    if not TD_API_KEY:
        return None
    key = (symbol, interval)
    now = time.time()
    refresh = TD_REFRESH_4H if interval == "4h" else TD_REFRESH_1D
    if key in TD_CACHE:
        ts0, data = TD_CACHE[key]
        if (now - ts0) < refresh:
            return data
    if not _td_can_request():
        if key in TD_CACHE:
            return TD_CACHE[key][1]
        return None
    try:
        params = {
            "symbol": symbol,
            "interval": "4h" if interval == "4h" else "1day",
            "outputsize": 600,
            "apikey": TD_API_KEY,
            "timezone": "Etc/UTC",
        }
        r = requests.get(f"{TD_BASE}/time_series", params=params, timeout=TD_TIMEOUT)
        if r.status_code != 200:
            return None
        j = r.json()
        if j.get("status") != "ok":
            return None
        values = j.get("values") or []
        out = []
        for v in values:
            ts = _td_parse_time(v.get("datetime"))
            if not ts:
                continue
            o = float(v["open"]); h = float(v["high"])
            l = float(v["low"]);  c = float(v["close"])
            if h <= 0 or l <= 0:
                continue
            out.append([ts, o, h, l, c])
        if not out:
            return None
        out.sort(key=lambda x: x[0])
        TD_CACHE[key] = (now, out)
        _td_mark_request()
        return out
    except:
        return None

# ================= TELEGRAM =====================

def _chat_tokens() -> List[str]:
    out = []
    if not TELEGRAM_CHAT:
        return out
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

# ================= CLOSED BARS =====================

def closed_ohlc(ohlc: Optional[List[List[float]]]):
    """
    Возвращаем только закрытые свечи:
    убираем последнюю (нулевую, потенциально незакрытую) свечу.
    """
    if not ohlc or len(ohlc) < 2:
        return []
    return ohlc[:-1]

# ================= INDICATORS =====================

def demarker_series(o, length):
    """
    DeMarker считается по массиву уже закрытых свечей (минус первые и далее).
    """
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
    """
    Зона по последнему значению DeM:
    v — значение DeM на последней закрытой свече (минус первая).
    """
    if v is None:
        return None
    if tf == "4H":
        ob, os_ = DEM_OB_4H, DEM_OS_4H
    else:
        ob, os_ = DEM_OB_1D, DEM_OS_1D
    if v >= ob:
        return "OB"  # зона перекупленности
    if v <= os_:
        return "OS"  # зона перепроданности
    return None

# ========== PIN-BAR (wick с направлением) ==========

def pinbar_by_zone(o, idx, zone, pct=0.30):
    """
    Проверка pin-bar на уже закрытой свече:
      - idx = -1 => всегда минус первая свеча в массиве закрытых свечей.
      - zone = "OB" → верхний фитиль >= pct * body
      - zone = "OS" → нижний фитиль >= pct * body
    """
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
    if zone == "OS":
        return lower >= pct * body
    return False

# ========== ENGULFING (последовательность из трёх свечей) ==========

def engulfing_with_prior4(o):
    """
    Engulfing по закрытым свечам:
      - используем три последние закрытые свечи: -3, -2, -1.
      - -3 и -2 одного цвета,
      - -1 противоположного цвета,
      - тело -1 полностью покрывает тело -2 (cover).
    """
    if not o or len(o) < 3:
        return False

    # -1 (последняя закрытая)
    o2, h2, l2, c2 = o[-1][1:5]
    # -2
    o3, h3, l3, c3 = o[-2][1:5]
    # -3
    o4, h4, l4, c4 = o[-3][1:5]

    bull2 = c2 >= o2
    bull3 = c3 >= o3
    bull4 = c4 >= o4

    # тело -1 накрывает тело -2
    cover = (min(o2, c2) <= min(o3, c3)) and (max(o2, c2) >= max(o3, c3))

    # бычий engulfing: -3 и -2 медвежьи, -1 бычий
    bull = bull2 and (not bull3) and (not bull4) and cover
    # медвежий engulfing: -3 и -2 бычьи, -1 медвежий
    bear = (not bull2) and bull3 and bull4 and cover
    return bull or bear

# ========== PYRAMIDAL (фитиль ≥85%) ==========

def pyramidal_pattern(o, idx, zone, pct=0.85):
    """
    Пирамидальный паттерн:
      - фитиль по направлению зоны ≥ pct от полной высоты свечи (high-low).
      - тело маленькое (автоматически, если фитиль ≥85% общей длины).
      - zone = "OS": длинный нижний фитиль,
      - zone = "OB": длинный верхний фитиль.
    """
    if zone not in ("OB", "OS"):
        return False
    if not o or not (-len(o) <= idx < len(o)):
        return False

    o_, h_, l_, c_ = o[idx][1:5]
    total = h_ - l_
    if total <= 0:
        return False

    body  = abs(c_ - o_)
    upper = h_ - max(o_, c_)
    lower = min(o_, c_) - l_

    if upper < 0 or lower < 0:
        return False

    if zone == "OB":
        # пирамида сверху: длинный верхний фитиль
        return upper >= pct * total
    else:
        # пирамида снизу: длинный нижний фитиль
        return lower >= pct * total

# ========== ЦВЕТОВОЙ РАЗВОРОТ (4-й паттерн, только дневка) ==========

def color_flip_pattern_1d(o, zone):
    """
    4-й паттерн для молнии (только 1D):
      - зона перепроданности: красная свеча → зелёная свеча.
      - зона перекупленности: зелёная свеча → красная свеча.
    Используем только закрытые свечи: -2 и -1.
    """
    if zone not in ("OB", "OS"):
        return False
    if not o or len(o) < 2:
        return False

    o_prev, _, _, c_prev = o[-2][1:5]
    o_last, _, _, c_last = o[-1][1:5]

    prev_green = c_prev >= o_prev
    last_green = c_last >= o_last

    if zone == "OS":
        # было красное (нисходящее), стало зелёное
        return (not prev_green) and last_green
    else:  # "OB"
        # было зелёное (восходящее), стало красное
        return prev_green and (not last_green)

# ========== ОБЩИЙ ПАТТЕРН (для обычных сигналов 1TF4H / 1TF1D) ==========

def candle_pattern(o, zone):
    """
    Свечной паттерн на последней закрытой свече (минус первой)
    для обычных сигналов 1TF4H / 1TF1D:
      - pin-bar по зоне (wick>=40%) ИЛИ engulfing_with_prior4.
    """
    if not o or len(o) < 3:
        return False
    if zone not in ("OB", "OS"):
        return False

    # pin-bar по минус первой свече, порог 40% (фикс)
    has_pin = pinbar_by_zone(o, -1, zone, 0.40)
    # engulfing по трём последним закрытым свечам
    has_eng = engulfing_with_prior4(o)
    return has_pin or has_eng

# ========== ПАТТЕРНЫ ДЛЯ МОЛНИИ (4 шт.) ==========

def lightning_has_pattern(k4, z4, k1, z1) -> bool:
    """
    Четыре свечных паттерна для сигнала молнии:
      1) Engulfing на 4H.
      2) Engulfing на 1D.
      3) Усиленный pin-bar (фитиль ≥50%) на 1D.
      4) Цветовой разворот на 1D (красная→зелёная в зоне перепроданности,
         зелёная→красная в зоне перекупленности).
      5) Пирамидальный паттерн (фитиль ≥85%) на 4H или 1D.
    Достаточно хотя бы одного из них.
    """
    # 1) engulfing на 4H
    if z4 and k4 and len(k4) >= 3 and engulfing_with_prior4(k4):
        return True

    # 2) engulfing на 1D
    if z1 and k1 and len(k1) >= 3 and engulfing_with_prior4(k1):
        return True

    # 3) усиленный pin-bar >=50% на 1D
    if z1 and k1 and pinbar_by_zone(k1, -1, z1, 0.50):
        return True

    # 4) цветовой разворот на 1D
    if z1 and k1 and color_flip_pattern_1d(k1, z1):
        return True

    # 5) пирамидальный паттерн (4H или 1D)
    if z4 and k4 and pyramidal_pattern(k4, -1, z4):
        return True
    if z1 and k1 and pyramidal_pattern(k1, -1, z1):
        return True

    return False

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
    if is_fx_sym(s) and len(s) == 6:
        return s + "-USD"
    return s

def format_signal(symbol, sig, zone, src):
    arrow = "🟢↑" if zone == "OS" else ("🔴↓" if zone == "OB" else "")
    status = "⚡" if sig == "LIGHT" else ""
    return f"{to_display(symbol)} [{src}] {arrow}{status}"

# ================= BYBIT =====================

def fetch_bybit_klines(symbol, interval, category, limit=600):
    iv = "240" if interval == "4h" else ("D" if interval == "1d" else interval)
    try:
        r = requests.get(
            BB_KLINES,
            params={"category": category, "symbol": symbol, "interval": iv, "limit": limit},
            timeout=BB_TIMEOUT
        )
        if r.status_code != 200:
            return None
        lst = (r.json().get("result") or {}).get("list") or []
        out = []
        for k in lst:
            ts = int(k[0]); ts = ts // 1000 if ts > 10**12 else ts
            o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
            if h <= 0 or l <= 0:
                continue
            out.append([ts, o, h, l, c])
        out.sort(key=lambda x: x[0])
        return out
    except:
        return None

# ================= TwelveData HELPERS =====================

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

# ================= TICKERS =====================

CRYPTO = [
    "BTC", "ETH", "SOL", "XRP"
]

INDEX_PERP = [
    "US500USDT","US100USDT","US30USDT","VIXUSDT","DE40USDT",
    "FR40USDT","UK100USDT","JP225USDT","HK50USDT","CN50USDT",
    "AU200USDT","ES35USDT","IT40USDT"
]

METALS = ["XAUUSDT","XAGUSDT","XCUUSDT","XPTUSDT","XPDUSDT"]
ENERGY = ["OILUSDT","BRENTUSDT","GASUSDT"]

STOCKS = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","BRKB",
    "AVGO","NFLX","AMD","JPM","V","MA","UNH","LLY","XOM","KO","PEP"
]

RU_STOCKS = [
    "IMOEX.ME","RTSI.ME","GAZP.ME","SBER.ME","LKOH.ME","ROSN.ME","TATN.ME",
    "ALRS.ME","GMKN.ME","YNDX.ME","MAGN.ME","MTSS.ME","CHMF.ME","AFLT.ME",
    "PHOR.ME","MOEX.ME","BELU.ME","PIKK.ME","VTBR.ME","IRAO.ME"
]

FX = ["EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","USDCAD","USDCHF"]

# ================= FETCH ROUTERS =====================

def fetch_crypto(base, interval):
    bb_lin = base + "USDT"
    d = fetch_bybit_klines(bb_lin, interval, "linear")
    if d: return d, bb_lin, "BB"
    bb_perp = base + "PERP"
    d = fetch_bybit_klines(bb_perp, interval, "linear")
    if d: return d, bb_perp, "BB"
    d = fetch_bybit_klines(bb_lin, interval, "spot")
    if d: return d, bb_lin, "BB"
    return None, base, "BB"

def fetch_other(sym, interval):
    # 1) Все ...USDT (индексы, металлы, энергия): только Bybit
    if sym.endswith("USDT"):
        d = fetch_bybit_klines(sym, interval, "linear")
        if d:
            return d, sym, "BB"
        return None, sym, "BB"

    # 2) FX 6-символьные: TwelveData FOREX
    if len(sym) == 6 and sym[:3].isalpha() and sym[3:].isalpha():
        td_sym = fx_to_td(sym)
        d = fetch_td_candles(td_sym, interval)
        return d, sym, "TD"

    # 3) Акции, включая RU: TwelveData STOCKS
    td_sym = ru_to_td(sym) if sym.upper().endswith(".ME") else sym.upper()
    d = fetch_td_candles(td_sym, interval)
    return d, sym, "TD"

# ================= PLAN =====================

def build_plan():
    plan = []
    for x in CRYPTO:     plan.append(("CRYPTO", x))
    for x in INDEX_PERP: plan.append(("OTHER", x))
    for x in METALS:     plan.append(("OTHER", x))
    for x in ENERGY:     plan.append(("OTHER", x))
    for x in STOCKS:     plan.append(("OTHER", x))
    for x in FX:         plan.append(("OTHER", x))
    for x in RU_STOCKS:  plan.append(("OTHER", x))
    return plan

# ================= CORE =====================

def process_symbol(kind, name):

    # Запрос сырых свечей (включая текущую нулевую)
    if kind == "CRYPTO":
        k4_raw, n4, s4 = fetch_crypto(name, KLINE_4H)
        k1_raw, n1, s1 = fetch_crypto(name, KLINE_1D)
    else:
        k4_raw, n4, s4 = fetch_other(name, KLINE_4H)
        k1_raw, n1, s1 = fetch_other(name, KLINE_1D)

    have4 = bool(k4_raw); have1 = bool(k1_raw)
    if not have4 and not have1:
        print(f"WARN: no data for {name} ({kind})", flush=True)
        return False

    # Обрезаем нулевую свечу: работаем только по закрытым
    k4 = closed_ohlc(k4_raw) if have4 else None
    k1 = closed_ohlc(k1_raw) if have1 else None

    if have4 and not k4: have4 = False
    if have1 and not k1: have1 = False

    if not have4 and not have1:
        print(f"WARN: no closed bars for {name} ({kind})", flush=True)
        return False

    # DeMarker по закрытым свечам
    d4 = demarker_series(k4, DEM_LEN) if have4 else None
    d1 = demarker_series(k1, DEM_LEN) if have1 else None

    v4 = last_closed(d4) if d4 else None  # значение DeM на минус первой свече (4H)
    v1 = last_closed(d1) if d1 else None  # значение DeM на минус первой свече (1D)

    z4 = zone_of(v4, "4H")
    z1 = zone_of(v1, "1D")

    # Свечные паттерны для обычных 1TF-сигналов (pin-bar 40% / engulfing)
    pat4 = candle_pattern(k4, z4) if have4 and z4 else False
    pat1 = candle_pattern(k1, z1) if have1 and z1 else False

    # Времена открытия последней закрытой свече на каждом ТФ
    open4 = k4[-1][0] if have4 else None
    open1 = k1[-1][0] if have1 else None
    dual  = max([x for x in (open4, open1) if x is not None]) if (open4 or open1) else None

    sym = n4 or n1 or name
    src = "BB" if "BB" in (s4, s1) else "TD"

    sent = False

    # ⚡ — 4H и 1D в одной зоне + любой из 4 свечных паттернов для молнии
    if z4 and z1 and z4 == z1:
        if lightning_has_pattern(k4 if have4 else None, z4, k1 if have1 else None, z1):
            sig = "LIGHT"
            key = f"{sym}|{sig}|{z4}|{dual}|{src}"
            if _broadcast_signal(format_signal(sym, sig, z4, src), key):
                sent = True

    # 1TF4H — зона только на 4H + обычный паттерн на 4H
    if (not sent) and have4 and z4 and pat4 and not (z1 and z1 == z4):
        sig = "1TF4H"
        key = f"{sym}|{sig}|{z4}|{open4}|{src}"
        if _broadcast_signal(format_signal(sym, sig, z4, src), key):
            sent = True

    # 1TF1D — зона только на 1D + обычный паттерн на 1D
    if (not sent) and have1 and z1 and pat1 and not (z4 and z4 == z1):
        sig = "1TF1D"
        key = f"{sym}|{sig}|{z1}|{open1}|{src}"
        if _broadcast_signal(format_signal(sym, sig, z1, src), key):
            sent = True

    if sent:
        print(
            f"DEBUG {sym} "
            f"4H: v={v4} z={z4} pat={pat4} "
            f"1D: v={v1} z={z1} pat={pat1} src={src}",
            flush=True
        )

    return sent

# ================= MAIN =====================

def main():
    plan = build_plan()
    n = len(plan)
    print(f"INFO: Symbols loaded: {n}", flush=True)
    if plan:
        print(f"Loaded {n} symbols for scan.", flush=True)
        print(f"First symbol checked: {plan[0][1]}", flush=True)
    else:
        print("WARN: empty plan, nothing to scan.", flush=True)

    idx = STATE.get("plan_idx", 0)
    if idx >= n:
        idx = 0

    # Цикл: за один «квази-минутный» слот обрабатываем не более TD_MINUTE_LIMIT тикеров,
    # затем ждём до конца минуты. Так мы укладываемся в лимит 8 тикеров в минуту.
    while True:
        start = time.time()
        plan = build_plan()
        n = len(plan)
        if n == 0:
            time.sleep(60)
            continue
        if idx >= n:
            idx = 0

        processed = 0
        # Обрабатываем до 8 тикеров (TD_MINUTE_LIMIT) за один слот
        while processed < TD_MINUTE_LIMIT:
            kind, name = plan[idx]
            process_symbol(kind, name)
            idx = (idx + 1) % n
            processed += 1

            # Промежуточная пауза, чтобы 8 тикеров уложились примерно в одну минуту
            # 8 * 5 сек ≈ 40 сек + накладные — потом доспим до 60.
            time.sleep(5)

        STATE["plan_idx"] = idx
        gc_state(STATE, 21)
        save_state(STATE_PATH, STATE)

        elapsed = time.time() - start
        # Досыпаем до ~60 секунд, чтобы не вылезать за «8 тикеров в минуту»
        sleep_left = 60.0 - elapsed
        if sleep_left > 0:
            time.sleep(sleep_left)

if __name__ == "__main__":
    main()