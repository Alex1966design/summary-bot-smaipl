import os
import json
import logging
import asyncio
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# --------------------
# Config
# --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. https://xxx.up.railway.app
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook").strip().lstrip("/")  # random string

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()
SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary").strip()

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))
SMAIPL_TIMEOUT = int(os.getenv("SMAIPL_TIMEOUT", "60"))

PORT = int(os.getenv("PORT", "8080"))  # Railway usually provides PORT

# --------------------
# In-memory chat history
# --------------------
chat_history: Dict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
history_lock = asyncio.Lock()


def _call_smaipl(bot_id: str, chat_id: str, message: str) -> dict:
    """
    SMAIPL call in sync mode (we run it in a thread via asyncio.to_thread).
    Expected SMAIPL example:
      POST https://api.smaipl.ru/api/v1.0/ask/<token>
      json={"bot_id": 5129, "chat_id":"ask123456", "message":"2+2"}
    """
    payload = {
        "bot_id": int(bot_id) if str(bot_id).isdigit() else bot_id,
        "chat_id": chat_id,
        "message": message,
    }

    r = requests.post(SMAIPL_API_URL, json=payload, timeout=SMAIPL_TIMEOUT)
    # SMAIPL sometimes returns JSON with {"error": true}
    try:
        return r.json()
    except Exception:
        return {"error": True, "raw": r.text, "status_code": r.status_code}


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я онлайн. Команда для резюме: /summary\n"
        "Сначала напиши несколько сообщений в этом чате, затем вызови /summary."
    )


async def _store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    # Не сохраняем команды
    if text.startswith("/"):
        return

    chat_id = update.effective_chat.id

    line = text
    async with history_lock:
        chat_history[chat_id].append(line)


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat_id_int = update.effective_chat.id
    await update.message.reply_text("Готовлю summary...")

    async with history_lock:
        items = list(chat_history[chat_id_int])[-SUMMARY_LAST_N:]

    if not items:
        await update.message.reply_text(
            "Недостаточно сообщений для summary. Напиши несколько сообщений и повтори /summary."
        )
        return

    # Формируем текст для SMAIPL
    prompt = (
        "Сделай краткое структурированное резюме переписки ниже.\n"
        "Формат: 1) Ключевые темы 2) Решения/договоренности 3) Следующие шаги.\n\n"
        "Текст:\n" + "\n".join(f"- {x}" for x in items)
    )

    # SMAIPL: используем стабильный chat_id, чтобы контекст не ломался
    smaipl_chat_id = f"tg_{chat_id_int}"

    try:
        resp = await asyncio.to_thread(_call_smaipl, SMAIPL_BOT_ID, smaipl_chat_id, prompt)
    except Exception as e:
        log.exception("SMAIPL request failed")
        await update.message.reply_text(f"SMAIPL error: {e}")
        return

    # Нормализуем ответ
    if isinstance(resp, dict) and resp.get("error") is True:
        await update.message.reply_text(f"Ответ SMAIPL: {json.dumps(resp, ensure_ascii=False)}")
        return

    # Популярные варианты поля с текстом:
    # - "answer"
    # - "response"
    # - "text"
    # - или просто строка
    answer: Optional[str] = None
    if isinstance(resp, dict):
        for k in ("answer", "response", "text", "result", "message"):
            v = resp.get(k)
            if isinstance(v, str) and v.strip():
                answer = v.strip()
                break
        if answer is None:
            # если пришло что-то сложное — покажем JSON
            answer = json.dumps(resp, ensure_ascii=False)
    else:
        answer = str(resp)

    await update.message.reply_text(answer)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # В webhook-режиме любые необработанные исключения = рестарт контейнера
    # Поэтому логируем, но не "падаем" из-за ошибок.
    err = context.error
    log.exception("Unhandled exception: %s", err)


def _validate_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not PUBLIC_URL:
        missing.append("PUBLIC_URL")
    if not WEBHOOK_PATH:
        missing.append("WEBHOOK_PATH")
    if not SMAIPL_API_URL:
        missing.append("SMAIPL_API_URL")
    if not SMAIPL_BOT_ID:
        missing.append("SMAIPL_BOT_ID")

    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def main() -> None:
    _validate_env()

    webhook_url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    log.info("Starting bot in WEBHOOK mode")
    log.info("Webhook URL: %s", webhook_url)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    # Команда summary должна совпадать с SUMMARY_COMMAND (по умолчанию /summary)
    app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _store_message))

    # Error handler
    app.add_error_handler(error_handler)

    # Запуск webhook-сервера (aiohttp)
    # drop_pending_updates=True — чистим старые апдейты, чтобы не "догонять" очередь
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
