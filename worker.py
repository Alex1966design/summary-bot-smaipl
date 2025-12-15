import asyncio
import logging
import requests

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from config import TELEGRAM_BOT_TOKEN, SMAIPL_API_URL, SMAIPL_BOT_ID, SUMMARY_COMMAND

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("summary-bot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Summary Bot готов. Пиши сообщения, затем команда /summary.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команда: /summary — сделать обзор последних сообщений.")


async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    history = context.chat_data.setdefault("history", [])
    history.append(update.message.text)
    # ограничим память
    if len(history) > 200:
        context.chat_data["history"] = history[-200:]


def _call_smaipl_sync(chat_id: int, text: str) -> str:
    payload = {
        "bot_id": SMAIPL_BOT_ID,
        "chat_id": str(chat_id),
        "message": f"Сделай краткое summary диалога (структурировано, пункты):\n{text}",
    }
    r = requests.post(SMAIPL_API_URL, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    return data.get("answer") or data.get("text") or str(data)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    history = context.chat_data.get("history", [])

    if not history:
        await update.message.reply_text("Пока нечего суммировать.")
        return

    await update.message.reply_text("Ок, делаю summary...")

    text = "\n".join(history[-50:])  # последние 50 сообщений

    loop = asyncio.get_running_loop()
    try:
        answer = await loop.run_in_executor(None, _call_smaipl_sync, chat_id, text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при запросе к SMAIPL: {e}")
        return

    await update.message.reply_text(answer)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # /summary (без слэша в CommandHandler)
    app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary))

    # сбор всех обычных сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages))

    app.run_polling()


if __name__ == "__main__":
    main()
