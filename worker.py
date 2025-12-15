import os
import time
import json
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

# ---------- LOGGING ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("summary-bot")


# ---------- HELPERS ----------
def _safe_text(s: str, limit: int = 500) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return s[:limit]


def _smaipl_call(message: str, chat_id: int) -> str:
    """
    Делает запрос в SMAIPL и возвращает текст ответа.
    Поддерживает разные форматы ответов, чтобы не падать.
    """
    payload = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": str(chat_id),
        "message": message,
    }

    logger.info(f"SMAIPL POST url={SMAIPL_API_URL} bot_id={SMAIPL_BOT_ID} chat_id={chat_id} msg_len={len(message)}")

    r = requests.post(SMAIPL_API_URL, json=payload, timeout=90)
    logger.info(f"SMAIPL status={r.status_code}")

    # Иногда API может вернуть не-JSON — логируем аккуратно
    try:
        data = r.json()
    except Exception:
        txt = r.text[:800]
        logger.error(f"SMAIPL non-json response: {txt}")
        return f"Ошибка SMAIPL: не-JSON ответ, status={r.status_code}"

    logger.info(f"SMAIPL json keys={list(data.keys())}")

    # Пытаемся вытащить ответ из типичных полей
    for key in ("answer", "text", "result", "message"):
        if key in data and isinstance(data[key], str) and data[key].strip():
            return data[key].strip()

    # Иногда ответ вложенный
    if "data" in data and isinstance(data["data"], dict):
        for key in ("answer", "text", "result", "message"):
            v = data["data"].get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    return "SMAIPL: не удалось распознать ответ. " + json.dumps(data, ensure_ascii=False)[:800]


# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    logger.info(f"/start from chat={chat_id} user={getattr(update.effective_user, 'id', 'unknown')}")
    await update.message.reply_text("Summary Bot готов. Напиши несколько сообщений и затем /summary")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    logger.info(f"/help from chat={chat_id}")
    await update.message.reply_text("Команды:\n/summary — сделать обзор последних сообщений\n/start — старт")


async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    user_id = getattr(update.effective_user, "id", "unknown")

    txt = update.message.text

    history = context.chat_data.setdefault("history", [])
    history.append(txt)

    # ограничиваем память
    if len(history) > 200:
        context.chat_data["history"] = history[-200:]

    logger.info(
        f"COLLECT chat={chat_id} user={user_id} "
        f"text='{_safe_text(txt, 120)}' history_len={len(context.chat_data['history'])}"
    )


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = getattr(update.effective_user, "id", "unknown")

    messages = context.chat_data.get("history", [])
    logger.info(f"SUMMARY request chat={chat_id} user={user_id} history_len={len(messages)}")

    if not messages:
        await update.message.reply_text("Пока нечего суммировать. Напиши несколько сообщений и повтори /summary.")
        return

    # последние 50 сообщений
    text_block = "\n".join(messages[-50:])

    prompt = (
        "Сделай краткое summary диалога. "
        "Структура:\n"
        "1) Ключевые темы (3-7 пунктов)\n"
        "2) Решения/договоренности (если есть)\n"
        "3) Открытые вопросы\n"
        "4) Следующие шаги\n\n"
        f"Диалог:\n{text_block}"
    )

    await update.message.reply_text("Ок, делаю summary...")

    try:
        answer = _smaipl_call(prompt, chat_id=chat_id)
        logger.info(f"SUMMARY done chat={chat_id} answer_len={len(answer)}")
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception(f"SUMMARY error chat={chat_id}: {e}")
        await update.message.reply_text(f"Ошибка при запросе к SMAIPL: {e}")


# ---------- MAIN ----------
def main():
    logger.info("Starting Telegram bot (long polling)...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # SUMMARY_COMMAND может быть "/summary" — для CommandHandler нужен без "/"
    summary_cmd = SUMMARY_COMMAND.lstrip("/") if isinstance(SUMMARY_COMMAND, str) else "summary"
    app.add_handler(CommandHandler(summary_cmd, summary))

    # Сбор всех текстовых сообщений, кроме команд
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages))

    # Запуск
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
