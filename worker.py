import asyncio
import logging
import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    SMAIPL_API_URL,
    SMAIPL_BOT_ID,
    SUMMARY_COMMAND,
)

logging.basicConfig(level=logging.INFO)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Summary Bot готов. Пиши сообщения, затем команда /summary.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды:\n/start\n/summary")


async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    history = context.chat_data.setdefault("history", [])
    history.append(update.message.text)

    # ограничим память
    if len(history) > 200:
        context.chat_data["history"] = history[-200:]


def call_smaipl(text: str, chat_id: int) -> str:
    payload = {
        "bot_id": SMAIPL_BOT_ID,
        "chat_id": str(chat_id),
        "message": f"Сделай краткое summary диалога:\n{text}",
    }
    r = requests.post(SMAIPL_API_URL, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    return data.get("answer") or data.get("text") or str(data)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    messages = context.chat_data.get("history", [])
    if not messages:
        await update.message.reply_text("Пока нечего суммировать.")
        return

    await update.message.reply_text("Ок, делаю summary...")

    text = "\n".join(messages[-50:])  # последние 50 сообщений

    try:
        answer = await asyncio.to_thread(call_smaipl, text, chat_id)
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при запросе к SMAIPL: {e}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages))

    app.run_polling()


if __name__ == "__main__":
    main()
