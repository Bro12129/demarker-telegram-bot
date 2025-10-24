# bot.py
import os
import logging
from datetime import timezone
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# === наши модули ===
from screener import run_screen
from state import load_state, save_state, is_new_alert, remember_alert
from config import (TICKERS, TIMEFRAME, OVERBOUGHT, OVERSOLD,
                    GREEN_ARROW, RED_ARROW)

# ====== env ======
TOKEN = (
    os.getenv("TG_BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)
CHAT_ID = (
    os.getenv("TG_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("CHAT_ID")
)

# ====== logging ======
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("demarker-bot")

# ====== helpers ======
def _fmt_line(item) -> str:
    if item["signal"] == "BUY":
        arrow = GREEN_ARROW
        title = "Oversold → BUY"
    else:
        arrow = RED_ARROW
        title = "Overbought → SELL"

    dem = item["demarker"]
    price = item["close"]
    ts = item["bar_time"].strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{arrow} {item['symbol']} | {title}\n"
        f"TF: {TIMEFRAME} | DeM: {dem:.2f} "
        f"(≤{OVERSOLD:.2f}/≥{OVERBOUGHT:.2f}) | Close: {price:.4f}\n"
        f"Bar close: {ts}"
    )

def _scan_and_notify(bot: Bot, chat_id: str):
    """Запускает сканер, шлёт новые сигналы в чат с дедупликацией."""
    state = load_state()
    found = run_screen(TICKERS)

    lines = []
    changed = False

    for it in found:
        if it.get("signal") is None:
            continue
        bar_iso = it["bar_time"].replace(tzinfo=timezone.utc).isoformat()
        if is_new_alert(state, it["symbol"], bar_iso, it["signal"]):
            remember_alert(state, it["symbol"], bar_iso, it["signal"])
            lines.append(_fmt_line(it))
            changed = True

    if changed and lines:
        msg = "📊 DeMarker(28) screener — подтверждённые сигналы (закрытая свеча):\n\n" + "\n\n".join(lines)
        bot.send_message(chat_id=chat_id, text=msg)
        save_state(state)
        log.info("sent %d signals", len(lines))
    else:
        log.info("no new confirmed signals")

# ====== handlers ======
def start(update: Update, context: CallbackContext):
    text = (
        "✅ DeMarker bot online\n"
        f"TF: {TIMEFRAME} | Overbought ≥ {OVERBOUGHT:.2f} | Oversold ≤ {OVERSOLD:.2f}\n"
        "Команды:\n"
        " /ping — проверка\n"
        " /scan — мгновенный скан и отправка сигналов\n"
        " /config — показать текущие настройки\n"
    )
    update.message.reply_text(text)

def ping(update: Update, context: CallbackContext):
    update.message.reply_text("pong ✅")

def show_config(update: Update, context: CallbackContext):
    update.message.reply_text(
        f"TICKERS: {', '.join(TICKERS)}\n"
        f"TIMEFRAME: {TIMEFRAME}\n"
        f"DeMarker: 28 | OB: {OVERBOUGHT:.2f} | OS: {OVERSOLD:.2f}"
    )

def scan_now(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    update.message.reply_text("⏳ Запускаю скан…")
    try:
        _scan_and_notify(context.bot, chat_id)
        update.message.reply_text("✅ Готово")
    except Exception as e:
        log.exception("scan_now failed")
        update.message.reply_text(f"❌ Ошибка: {e}")

# ====== scheduled job ======
def job_scan(context: CallbackContext):
    chat_id = CHAT_ID
    if not chat_id:
        log.warning("CHAT_ID not set, skip scheduled scan")
        return
    try:
        _scan_and_notify(context.bot, chat_id)
    except Exception:
        log.exception("scheduled scan failed")

# ====== entrypoint ======
def main():
    if not TOKEN:
        raise RuntimeError("TG_BOT_TOKEN (или TELEGRAM_BOT_TOKEN) не задан")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("config", show_config))
    dp.add_handler(CommandHandler("scan", scan_now))

    # Периодический скан — раз в 15 минут (можешь поменять)
    updater.job_queue.run_repeating(job_scan, interval=900, first=10)

    # Сообщение себе при старте
    if CHAT_ID:
        try:
            Bot(TOKEN).send_message(chat_id=CHAT_ID, text="✅ DeMarker bot started")
        except Exception:
            log.exception("cannot send start message")

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()