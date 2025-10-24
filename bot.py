import os
import asyncio
import math
from typing import List, Tuple, Dict

import aiohttp
from datetime import datetime, timezone

# ============ CONFIG ============
# Таймфреймы: 4H и 1D
TF_LIST = ["240", "D"]                   # 240 = 4H, D = 1D (Bybit формат)
TF_LABEL = {"240": "4H", "D": "1D"}

# DeMarker
DEM_LEN = 28
OB = 0.70
OS = 0.30

# Список из ~30 популярных тикеров (USDT)
SYMS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT",
    "DOGEUSDT","TONUSDT","TRXUSDT","LINKUSDT","MATICUSDT","DOTUSDT",
    "AVAXUSDT","SHIBUSDT","LTCUSDT","BCHUSDT","ATOMUSDT","XLMUSDT",
    "APTUSDT","SUIUSDT","ARBUSDT","OPUSDT","NEARUSDT","INJUSDT",
    "RUNEUSDT","AAVEUSDT","EGLDUSDT","FILUSDT","ETCUSDT","UNIUSDT"
]

# Bybit публичное API (spot-рынок; не требует ключей)
BYBIT_BASE = "https://api.bybit.com"
KLINE_PATH = "/v5/market/kline"          # GET: category=spot&symbol=&interval=&limit=
HTTP_TIMEOUT = 25
REQUEST_CONCURRENCY = 6                  # Параллелизм запросов
JOB_EVERY_SECONDS = 300                  # каждые 5 минут

# Telegram
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
assert TG_TOKEN and TG_CHAT_ID, "Нужны TG_BOT_TOKEN и TG_CHAT_ID в переменных окружения."

# =================================

# -------- DeMarker (vector) --------
def demarker(high: List[float], low: List[float], length: int) -> List[float]:
    """Возвращает массив значений DeMarker (0..1) той же длины, NaN в начале."""
    n = len(high)
    if n != len(low) or n == 0:
        return []
    out = [math.nan] * n
    up_buf = [0.0] * n
    dn_buf = [0.0] * n

    for i in range(1, n):
        up = max(high[i] - high[i-1], 0.0)
        dn = max(low[i-1] - low[i], 0.0)
        up_buf[i] = up
        dn_buf[i] = dn

    # скользящая сумма
    up_sum = 0.0
    dn_sum = 0.0
    for i in range(n):
        up_sum += up_buf[i]
        dn_sum += dn_buf[i]
        if i >= length:
            up_sum -= up_buf[i-length]
            dn_sum -= dn_buf[i-length]
        if i >= length:
            denom = up_sum + dn_sum
            out[i] = (up_sum / denom) if denom > 0 else math.nan
    return out

# -------- Bybit fetch --------
async def fetch_klines(session: aiohttp.ClientSession, symbol: str, tf: str, limit: int = 200) -> List[Tuple[int, float, float, float, float]]:
    """
    Возвращает OHLCV как список кортежей (ts_ms, open, high, low, close).
    Bybit v5 возвращает newest-first -> разворачиваем в oldest-first.
    """
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": tf,
        "limit": str(limit)
    }
    url = BYBIT_BASE + KLINE_PATH
    async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"HTTP {r.status} {symbol} {tf}: {text}")
        data = await r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode {data.get('retCode')} {symbol} {tf}: {data.get('retMsg')}")
    lst = data["result"]["list"]  # newest first
    lst.reverse()
    # элементы формата: [startTime, open, high, low, close, volume, turnover]
    out = []
    for it in lst:
        ts = int(it[0])
        o = float(it[1]); h = float(it[2]); l = float(it[3]); c = float(it[4])
        out.append((ts, o, h, l, c))
    return out

def last_closed_index(arr_len: int) -> int:
    """Индекс последней ЗАКРЫТОЙ свечи (не текущей)."""
    return max(0, arr_len - 2)

def format_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

async def scan_once(session: aiohttp.ClientSession) -> Dict[str, Dict[str, float]]:
    """
    Сканирует все тикеры на обоих ТФ.
    Возвращает словарь {tf: {symbol: dem_last}} только для тех, где попадание в зону.
    """
    results: Dict[str, Dict[str, float]] = {tf: {} for tf in TF_LIST}

    sem = asyncio.Semaphore(REQUEST_CONCURRENCY)

    async def one(symbol: str, tf: str):
        async with sem:
            try:
                kl = await fetch_klines(session, symbol, tf, limit=max(DEM_LEN*3, 120))
                if len(kl) < DEM_LEN + 2:
                    return
                _, _, highs, lows, closes = zip(*[(t,o,h,l,c) for (t,o,h,l,c) in kl])
                de = demarker(list(highs), list(lows), DEM_LEN)
                i = last_closed_index(len(de))
                val = de[i]
                if math.isnan(val):
                    return
                if val >= OB or val <= OS:
                    results[tf][symbol] = val
            except Exception as e:
                # молча пропускаем конкретный символ, чтобы не падал общий цикл
                pass

    tasks = []
    for tf in TF_LIST:
        for s in SYMS:
            tasks.append(asyncio.create_task(one(s, tf)))
    await asyncio.gather(*tasks)
    return results

# -------- Telegram (на чистом aiohttp) --------
async def tg_send_text(session: aiohttp.ClientSession, text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with session.post(url, json=payload, timeout=HTTP_TIMEOUT) as r:
        await r.text()  # проглатываем

async def start_message(session: aiohttp.ClientSession):
    tfs = ", ".join(TF_LABEL[tf] for tf in TF_LIST)
    await tg_send_text(session, f"✅ Бот запущен. Мониторю 30 тикеров (DeMarker {DEM_LEN}) на {tfs}.\n"
                                f"Зоны: OB ≥ {OB:.2f}, OS ≤ {OS:.2f}. Смотрю последнюю <u>закрытую</u> свечу.")

def format_hits(hits: Dict[str, Dict[str, float]]) -> str:
    parts = []
    for tf in TF_LIST:
        rows = []
        for sym, v in sorted(hits[tf].items()):
            tag = "🟢 OS" if v <= OS else "🔴 OB"
            rows.append(f"{tag} <b>{sym}</b>: {v:.3f}")
        if rows:
            parts.append(f"<b>{TF_LABEL[tf]}</b>\n" + "\n".join(rows))
    return "\n\n".join(parts)

async def worker():
    async with aiohttp.ClientSession() as session:
        await start_message(session)
        while True:
            try:
                hits = await scan_once(session)
                msg = format_hits(hits)
                if msg.strip():
                    await tg_send_text(session, f"📊 Сигналы DeMarker:\n\n{msg}")
            except Exception as e:
                # отправим краткую ошибку, но не спамим деталями
                try:
                    await tg_send_text(session, f"⚠️ Ошибка цикла: {type(e).__name__}")
                except:
                    pass
            await asyncio.sleep(JOB_EVERY_SECONDS)

if __name__ == "__main__":
    asyncio.run(worker())