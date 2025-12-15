import os
import logging
import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------
# ЛОГИРОВАНИЕ
# --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("summary-bot")

# --------------------
# ENV ПЕРЕМЕННЫЕ
# --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL")
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID")
SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# --------------------
# HANDLERS
# --------------------
async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    history = context.chat_data.setdefault("history", [])
    history.append(update.message.text)

    if len(history) > 200:
        context.chat_data["history"] = history[-200:]


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Summary command received")

    history = context.chat_data.get("history", [])

    if not history:
        await update.message.reply_text("Пока нечего суммировать.")
        return

    text = "\n".join(history[-50:])

    payload = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": update.effective_chat.id,
        "message": f"Сделай краткое summary диалога:\n{text}",
    }

    try:
        logger.info("Sending request to SMAIPL")
        r = requests.post(SMAIPL_API_URL, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        answer = data.get("answer") or data.get("text") or str(data)

    except Exception as e:
        logger.exception("SMAIPL request failed")
        answer = f"Ошибка при запросе к SMAIPL:\n{e}"

    await update.message.reply_text(answer)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Summary Bot запущен. Используй /summary")


# --------------------
# MAIN
# --------------------
def main():
    logger.info("Starting Telegram bot (long polling)")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages))

    app.run_polling()


if __name__ == "__main__":
    main()
