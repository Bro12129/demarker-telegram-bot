# bot.py
import os
import time
import json
import logging
import requests
from typing import List, Dict, Tuple, Optional

# ---------------------- ENV ----------------------
BINGX_BASE     = os.getenv("BINGX_BASE", "https://open-api.bingx.com")
KLINE_4H       = os.getenv("KLINE_4H", "4h")
KLINE_1D       = os.getenv("KLINE_1D", "1d")

DEM_LEN        = int(os.getenv("DEM_LEN", "28"))
DEM_OB         = float(os.getenv("DEM_OB", "0.70"))
DEM_OS         = float(os.getenv("DEM_OS", "0.30"))

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))
STATE_PATH     = os.getenv("STATE_PATH", "/data/state.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
TG_API         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ---------------------- LOGGING (тихий режим) ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    force=True
)
log = logging.getLogger("bot")

# ---------------------- HTTP ----------------------
def http_get(url: str, params: Dict[str, str], timeout: int = 15) -> Optional[Dict]:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None

# ---------------------- STATE (для дедупа между рестартами) ----------------------
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

STATE = load_state(STATE_PATH)  # {"sent": { "<key>": <last_ts> }}

# ---------------------- SYMBOLS ----------------------
def to_bingx(sym: str) -> str:
    """
    Конвертация форматов символов:
    BYBIT: BTCUSDT, EURUSD, US100, XAUUSDT
    BINGX: BTC-USDT, EUR-USD, US100, XAU-USDT
    + спец-маппинг для индексов/металлов/волатильности.
    """
    special = {
        "US100": "US100",
        "US500": "US500",
        "US30":  "US30",
        "US2000":"US2000",
        "VIX":   "VIX",
        "XAUUSDT":"XAU-USDT",
        "XAGUSDT":"XAG-USDT",
    }
    if sym in special:
        return special[sym]
    # Форекс EURUSD -> EUR-USD
    if len(sym) == 6 and sym.isalpha():
        return f"{sym[:3]}-{sym[3:]}"
    # Крипто/прочие XXXUSDT -> XXX-USDT
    if sym.endswith("USDT"):
        return f"{sym[:-4]}-USDT"
    return sym.replace("/", "-")

def fetch_contracts() -> List[str]:
    """
    Тянем доступные PERP-контракты и фильтруем категории:
    - крипто-мейджоры
    - драгметаллы (XAU, XAG)
    - фондовые индексы США (US100/US500/US30/US2000, VIX)
    - FX-PERP
    - токенизированные акции (xStock)
    """
    url = f"{BINGX_BASE}/openApi/swap/v2/quote/contracts"
    data = http_get(url, params={}) or {}
    items = data.get("data") or data.get("symbolList") or []
    symbols: List[str] = []

    for it in items:
        # На разных версиях API ключи могут отличаться, обрабатываем безопасно
        symbol = (it.get("symbol") or it.get("contractId") or "").upper()
        if not symbol:
            continue
        # Признак PERP
        ctype = (it.get("contractType") or it.get("type") or "").upper()
        if "PERP" not in ctype:
            continue

        # Категория/подсказки
        cat = (it.get("category") or it.get("assetType") or "").lower()
        s_up = symbol.upper()

        # Индексы/волатильность/металлы — через спец-список
        if s_up in {"US100", "US500", "US30", "US2000", "VIX", "XAU-USDT", "XAG-USDT"}:
            symbols.append(s_up)
            continue

        # xStock
        if "stock" in cat or "xstock" in cat:
            symbols.append(s_up)
            continue

        # FX
        if "fx" in cat or "-" in s_up and len(s_up) == 7 and s_up[3] == "-":
            symbols.append(s_up)
            continue

        # Крипто по -USDT
        if s_up.endswith("-USDT"):
            symbols.append(s_up)
            continue

    # Убираем дубликаты, сортируем
    uniq = sorted(set(symbols))
    return uniq

# ---------------------- KLINES ----------------------
def fetch_klines(symbol: str, interval: str, limit: int = 200) -> Optional[List[List[float]]]:
    """
    Возвращает список свечей [openTime, open, high, low, close, volume, closeTime, ...]
    В BingX формат может отличаться; нормализуем ниже.
    """
    url = f"{BINGX_BASE}/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    data = http_get(url, params=params)
    if not data:
        return None

    raw = data.get("data") or data.get("klines") or []
    out: List[List[float]] = []
    for k in raw:
        # Поддержка разных форматов (dict/list)
        if isinstance(k, dict):
            o = float(k.get("open", 0))
            h = float(k.get("high", 0))
            l = float(k.get("low", 0))
            c = float(k.get("close", 0))
            t = int(k.get("openTime") or k.get("time") or k.get("t") or 0)
        else:
            # Предположим k=[t,o,h,l,c,...]
            try:
                t = int(k[0])
                o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
            except Exception:
                continue
        if h <= 0 or l <= 0:
            continue
        out.append([t, o, h, l, c])
    # сортировка по времени
    out.sort(key=lambda x: x[0])
    return out

# ---------------------- INDICATORS ----------------------
def demarker_series(ohlc: List[List[float]], length: int) -> Optional[List[float]]:
    """
    DeMarker(n):
      DeM_up[i]   = max(H[i] - H[i-1], 0)
      DeM_down[i] = max(L[i-1] - L[i], 0)
      DeM = SMA(DeM_up, n) / (SMA(DeM_up, n) + SMA(DeM_down, n))
    Возвращает список значений DeM по всем барам (NaN пропускаем как None).
    """
    if not ohlc or len(ohlc) < length + 2:
        return None
    highs = [x[2] for x in ohlc]
    lows  = [x[3] for x in ohlc]

    up = [0.0]
    dn = [0.0]
    for i in range(1, len(ohlc)):
        up.append(max(highs[i] - highs[i-1], 0.0))
        dn.append(max(lows[i-1] - lows[i], 0.0))

    def sma(arr: List[float], i: int, n: int) -> float:
        s = 0.0
        for k in range(i-n+1, i+1):
            s += arr[k]
        return s / n

    dem: List[Optional[float]] = [None] * len(ohlc)
    for i in range(length, len(ohlc)):
        up_s = sma(up, i, length)
        dn_s = sma(dn, i, length)
        denom = up_s + dn_s
        dem[i] = (up_s / denom) if denom != 0 else 0.5
    # Преобразуем Optional[float] в float, оставив None на ранних барах
    return dem  # type: ignore

# ---------------------- CANDLE PATTERNS ----------------------
def wick_ge_25pct(o: float, h: float, l: float, c: float) -> bool:
    rng = max(h - l, 1e-12)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return (upper >= 0.25 * rng) or (lower >= 0.25 * rng)

def is_bull(c: float, o: float) -> bool:
    return c >= o

def engulfing_with_prior_opposition(ohlc: List[List[float]]) -> bool:
    """
    Engulfing с условием: перед текущей свечой было >=2 подряд свечей противоположного цвета.
    И текущая свеча телом поглощает предыдущую.
    """
    if len(ohlc) < 4:
        return False
    # текущая и предыдущая
    o0, h0, l0, c0 = ohlc[-1][1], ohlc[-1][2], ohlc[-1][3], ohlc[-1][4]
    o1, h1, l1, c1 = ohlc[-2][1], ohlc[-2][2], ohlc[-2][3], ohlc[-2][4]
    bull0 = is_bull(c0, o0)
    bull1 = is_bull(c1, o1)

    # 2 свечи до предыдущей (для последовательности)
    o2, c2 = ohlc[-3][1], ohlc[-3][4]
    o3, c3 = ohlc[-4][1], ohlc[-4][4]
    bull2 = is_bull(c2, o2)
    bull3 = is_bull(c3, o3)

    # Должны быть >=2 подряд свечей противоположного цвета перед previous
    # Если текущая бычья, то перед ней >=2 медвежьих подряд и наоборот
    if bull0:
        if not ((not bull1) and (not bull2 or not bull3) and ((not bull2 and not bull3) or (not bull2))):
            # Упростим: требуем хотя бы 2 медвежьих из двух последних до previous (жёстко: оба медвежьи)
            if not ((not bull2) and (not bull3)):
                return False
        # Поглощение телом: текущее тело >= предыдущего и перекрывает open/close
        return (o0 <= c1 and c0 >= o1) or (o0 <= o1 and c0 >= c1)
    else:
        if not (bull2 and bull3):
            return False
        return (o0 >= c1 and c0 <= o1) or (o0 >= o1 and c0 <= c1)

def candle_pattern_ok(ohlc: List[List[float]]) -> bool:
    o, h, l, c = ohlc[-1][1], ohlc[-1][2], ohlc[-1][3], ohlc[-1][4]
    return wick_ge_25pct(o, h, l, c) or engulfing_with_prior_opposition(ohlc)

# ---------------------- SIGNAL LOGIC ----------------------
def zone_of(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value >= DEM_OB:
        return "OB"  # overbought
    if value <= DEM_OS:
        return "OS"  # oversold
    return None

def classify_signal(dem4h: Optional[float], dem1d: Optional[float], has_candle: bool) -> Optional[Tuple[str, Optional[str]]]:
    """
    Возвращает (signal_type, zone) или None.
    Типы:
      - LIGHT      — обе DeM в одной зоне (OB/OS) и НЕТ свечного паттерна
      - L+CAN      — обе DeM в одной зоне и ЕСТЬ свечной паттерн
      - 1TF+CAN    — только одна из DeM в зоне и ЕСТЬ свечной паттерн
    Никогда не отправляем "чистый" свечной паттерн.
    """
    z4 = zone_of(dem4h)
    z1 = zone_of(dem1d)
    both = (z4 is not None) and (z1 is not None) and (z4 == z1)
    one  = ((z4 is not None) ^ (z1 is not None))  # ровно одна в зоне

    if both and has_candle:
        return ("L+CAN", z4)
    if both and not has_candle:
        return ("LIGHT", z4)
    if one and has_candle:
        # неважно какая зона, вернём ту, что есть
        return ("1TF+CAN", z4 or z1)
    return None

# ---------------------- TELEGRAM (важно: правильная последовательность) ----------------------
def tg_send_symbol_only(symbol: str) -> bool:
    """
    Отправляем в Telegram СТРОГО только символ, без текста про стратегию/таймфреймы и т.д.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    try:
        r = requests.post(
            TG_API,
            data={"chat_id": TELEGRAM_CHAT, "text": symbol, "disable_notification": True},
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

# ---------------------- CORE ----------------------
def last_value(series: List[Optional[float]]) -> Optional[float]:
    if not series:
        return None
    return series[-1]

def build_dedup_key(symbol: str, signal_type: str, zone: Optional[str], last_ts: int) -> str:
    # ключ для дедупа: символ + тип сигнала + зона + таймстемп последней свечи старшего ТФ (1D)
    return f"{symbol}|{signal_type}|{zone or '-'}|{last_ts}"

def process_symbol(symbol: str) -> Optional[str]:
    """
    Возвращает символ для отправки (или None).
    Последовательность (исправленная):
      1) Загружаем данные (4h и 1d)
      2) Считаем индикаторы и паттерны
      3) Классифицируем сигнал
      4) ДЕДУП (STATE) — проверяем, не отправляли ли уже ключ
      5) ОТПРАВКА в Telegram (только символ)
      6) Сохраняем STATE
    """
    k4 = fetch_klines(symbol, KLINE_4H, limit=max(200, DEM_LEN + 10))
    k1 = fetch_klines(symbol, KLINE_1D, limit=max(200, DEM_LEN + 10))
    if not k4 or not k1:
        return None

    dem4_series = demarker_series(k4, DEM_LEN)
    dem1_series = demarker_series(k1, DEM_LEN)
    if not dem4_series or not dem1_series:
        return None

    dem4 = last_value(dem4_series)
    dem1 = last_value(dem1_series)

    # свечной паттерн проверяем на ТЕКУЩЕЙ свече старшего из двух? У нас правило:
    # паттерны учитываются, но НИКОГДА не отправляются без условия DeMarker.
    # Берём паттерн на текущей свече младшего ТФ (4h) — более чувствительный.
    has_candle = candle_pattern_ok(k4)

    cls = classify_signal(dem4, dem1, has_candle)
    if not cls:
        return None

    sig_type, zone = cls
    last_ts_1d = k1[-1][0]

    key = build_dedup_key(symbol, sig_type, zone, last_ts_1d)
    if STATE["sent"].get(key):
        return None  # уже отправляли этот сигнал для этой дневной свечи

    # >>> ВАЖНО: правильная последовательность — отправляем после дедупа:
    ok = tg_send_symbol_only(symbol)
    if not ok:
        return None

    # фиксируем отправку
    STATE["sent"][key] = int(time.time())
    return symbol

def main():
    symbols = fetch_contracts()
    if not symbols:
        symbols = ["BTC-USDT"]  # fallback

    # Тихий старт — РОВНО три строки:
    log.info(f"INFO: Symbols loaded: {len(symbols)}")
    log.info(f"INFO: Loaded {len(symbols)} symbols for scan.")
    log.info(f"INFO: First symbol checked: {symbols[0]}")

    # Основной цикл
    while True:
        sent_any = False
        for sym in symbols:
            try:
                res = process_symbol(sym)
                if res:
                    sent_any = True
            except Exception:
                # Ничего лишнего в лог — молчим
                pass

        if sent_any:
            save_state(STATE_PATH, STATE)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()