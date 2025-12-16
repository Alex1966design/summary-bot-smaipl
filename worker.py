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

# =====================
# ЛОГИРОВАНИЕ
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("summary-bot")

# =====================
# ENV ПЕРЕМЕННЫЕ
# =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL")
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID")
SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# =====================
# ХЕНДЛЕР /summary
# =====================
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"/summary received from chat {chat_id}")

    # Берём последние сообщения
    messages = context.chat_data.get("messages", [])

    if not messages:
        await update.message.reply_text("Нет сообщений для анализа.")
        return

    text = "\n".join(messages[-20:])

    payload = {
        "bot_id": SMAIPL_BOT_ID,
        "text": text,
    }

    try:
        logger.info("Sending request to SMAIPL...")
        response = requests.post(SMAIPL_API_URL, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        summary = result.get("answer", "Summary не получен")

        await update.message.reply_text(summary)

    except Exception as e:
        logger.exception("SMAIPL request failed")
        await update.message.reply_text(f"Ошибка при запросе Summary: {e}")

# =====================
# СОХРАНЕНИЕ СООБЩЕНИЙ
# =====================
async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    context.chat_data.setdefault("messages", []).append(update.message.text)

# =====================
# MAIN
# =====================
def main():
    logger.info("Starting Telegram bot (long polling)...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages))

    app.run_polling()

if __name__ == "__main__":
    main()
