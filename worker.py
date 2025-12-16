import os
import json
import logging
from typing import Optional, Dict, Any, List

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("summary_bot")

# ----------------------------
# Env
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary")

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL")  # либо полный URL, либо базовый
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID")    # строкой ок
SMAIPL_API_TOKEN = os.getenv("SMAIPL_API_TOKEN")  # если решим авторизацию через Bearer

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))
SMAIPL_TIMEOUT = int(os.getenv("SMAIPL_TIMEOUT", "60"))

# если вдруг окружение подсовывает прокси — отключим
DISABLE_PROXIES = os.getenv("DISABLE_PROXIES", "true").lower() in ("1", "true", "yes")

def _check_env():
    missing = []
    for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "SMAIPL_API_URL": SMAIPL_API_URL,
        "SMAIPL_BOT_ID": SMAIPL_BOT_ID,
    }.items():
        if not v:
            missing.append(k)

    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    logger.info("ENV OK. SUMMARY_COMMAND=%s HISTORY_LIMIT=%s SUMMARY_LAST_N=%s TIMEOUT=%s",
                SUMMARY_COMMAND, HISTORY_LIMIT, SUMMARY_LAST_N, SMAIPL_TIMEOUT)
    logger.info("SMAIPL_API_URL=%s", SMAIPL_API_URL)

def _session() -> requests.Session:
    s = requests.Session()
    if DISABLE_PROXIES:
        # полностью игнорируем HTTP(S)_PROXY и т.п.
        s.trust_env = False
    return s

def call_smaipl(prompt_text: str, chat_id: int) -> str:
    """
    Делает запрос в SMAIPL.
    ВАЖНО: мы логируем status + body при ошибке 400, чтобы понять требуемый формат.
    """
    url = SMAIPL_API_URL

    payload: Dict[str, Any] = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": chat_id,
        "message": prompt_text,
    }

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
    }

    # Вариант: если SMAIPL требует Bearer, включаем через отдельную переменную
    # (ты можешь добавить SMAIPL_API_TOKEN в Railway Variables)
    if SMAIPL_API_TOKEN:
        headers["Authorization"] = f"Bearer {SMAIPL_API_TOKEN}"

    logger.info("SMAIPL request -> url=%s payload_keys=%s", url, list(payload.keys()))

    s = _session()
    r = s.post(url, json=payload, headers=headers, timeout=SMAIPL_TIMEOUT)

    # Логи на случай ошибки
    if r.status_code >= 400:
        logger.error("SMAIPL ERROR status=%s body=%s", r.status_code, r.text[:2000])
        r.raise_for_status()

    # Пытаемся разобрать JSON
    try:
        data = r.json()
    except Exception:
        logger.error("SMAIPL non-JSON response: %s", r.text[:2000])
        return r.text

    # Поддержим несколько вариантов ключей
    return (
        data.get("answer")
        or data.get("text")
        or data.get("result")
        or json.dumps(data, ensure_ascii=False)
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Summary Bot готов. Пиши сообщения и вызывай /summary")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды:\n/summary — сделать краткий обзор последних сообщений")

async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    history: List[str] = context.chat_data.setdefault("history", [])
    history.append(update.message.text)

    if len(history) > HISTORY_LIMIT:
        context.chat_data["history"] = history[-HISTORY_LIMIT:]

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    history: List[str] = context.chat_data.get("history", [])

    if not history:
        await update.message.reply_text("Пока нечего суммировать.")
        return

    chunk = "\n".join(history[-SUMMARY_LAST_N:])
    prompt = f"Сделай краткое summary диалога:\n{chunk}"

    await update.message.reply_text("Ок, делаю summary...")

    try:
        answer = call_smaipl(prompt, chat_id)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("Summary failed")
        await update.message.reply_text(f"Ошибка при запросе Summary: {e}")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)

def main():
    _check_env()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # SUMMARY_COMMAND может быть "/summary" — python-telegram-bot ждёт без "/"
    app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages))

    app.add_error_handler(on_error)

    logger.info("Starting Telegram bot (long polling)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
