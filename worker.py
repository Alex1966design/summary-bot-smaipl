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

from config import (
    TELEGRAM_BOT_TOKEN,
    SMAIPL_API_URL,
    SMAIPL_BOT_ID,
    SUMMARY_COMMAND,
)

# --------------------------------------------------
# Логирование
# --------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------
# /start
# --------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Summary Bot запущен.\n"
        "Отправляй сообщения или вставляй текст.\n"
        "Для summary используй команду /summary"
    )

# --------------------------------------------------
# Сбор всех сообщений (copy-paste тоже сюда попадает)
# --------------------------------------------------
async def collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    history = context.chat_data.setdefault("history", [])
    history.append(update.message.text)

    # ограничим историю
    if len(history) > 300:
        context.chat_data["history"] = history[-300:]

# --------------------------------------------------
# /summary
# --------------------------------------------------
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.chat_data.get("history", [])

    if not history:
        await update.message.reply_text("Пока нечего суммировать.")
        return

    text = "\n".join(history[-50:])

    payload = {
        "bot_id": SMAIPL_BOT_ID,
        "chat_id": str(update.effective_chat.id),
        "message": f"Сделай краткое summary диалога:\n{text}",
    }

    try:
        r = requests.post(SMAIPL_API_URL, json=payload, timeout=60)
        r.raise_for_status()
        result = r.json()
    except Exception as e:
        logger.exception("Ошибка при запросе к SMAIPL")
        await update.message.reply_text(f"Ошибка при запросе к SMAIPL:\n{e}")
        return

    answer = (
        result.get("answer")
        or result.get("text")
        or result.get("response")
        or str(result)
    )

    await update.message.reply_text(answer)

# --------------------------------------------------
# main
# --------------------------------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_message))

    logger.info("Summary Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
