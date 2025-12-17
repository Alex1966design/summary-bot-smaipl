import os
import json
import asyncio
import logging
from collections import defaultdict, deque
from typing import Deque, Dict, Any, Optional

import requests
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Config (ENV)
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# SMAIPL:
# обычно выглядит так: https://api.smaipl.ru/api/v1.0/ask/<ask_id>
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = int(os.getenv("SMAIPL_BOT_ID", "0").strip() or 0)

SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary").strip() or "/summary"

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))

SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "30"))
SMAIPL_VERIFY_SSL = os.getenv("SMAIPL_VERIFY_SSL", "true").lower() in ("1", "true", "yes", "y")

# Webhook:
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")  # например: https://<service>.up.railway.app
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip() or "/webhook"

# Доп. защита webhook (не обязательно, но рекомендую):
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # любая строка; если задана — Telegram будет присылать заголовок
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Railway
PORT = int(os.getenv("PORT", "8000"))

# =========================
# Logging
# =========================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

# =========================
# In-memory history
# =========================

# chat_id -> deque[str]
CHAT_HISTORY: Dict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))


def _append_history(chat_id: int, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    CHAT_HISTORY[chat_id].append(text)


def _get_last_messages(chat_id: int, n: int) -> str:
    msgs = list(CHAT_HISTORY[chat_id])[-max(1, n):]
    return "\n".join(msgs).strip()


# =========================
# SMAIPL call
# =========================

def _call_smaipl_sync(bot_id: int, chat_id: str, message: str) -> Dict[str, Any]:
    """
    Синхронный вызов SMAIPL. Возвращаем JSON-словарь целиком.
    """
    if not SMAIPL_API_URL:
        return {"error": True, "detail": "SMAIPL_API_URL is not set"}
    if bot_id <= 0:
        return {"error": True, "detail": "SMAIPL_BOT_ID is not set or invalid"}

    payload = {
        "bot_id": bot_id,
        "chat_id": chat_id,
        "message": message,
    }

    try:
        r = requests.post(
            SMAIPL_API_URL,
            json=payload,
            timeout=SMAIPL_TIMEOUT,
            verify=SMAIPL_VERIFY_SSL,
        )
        # SMAIPL может отвечать 400 при неверном формате/параметрах
        try:
            data = r.json()
        except Exception:
            data = {"error": True, "detail": f"Non-JSON response from SMAIPL: {r.text[:500]}"}

        if r.status_code >= 400:
            data.setdefault("error", True)
            data.setdefault("http_status", r.status_code)
        return data

    except Exception as e:
        return {"error": True, "detail": f"Exception calling SMAIPL: {type(e).__name__}: {e}"}


async def call_smaipl(bot_id: int, chat_id: str, message: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_call_smaipl_sync, bot_id, chat_id, message)


def extract_smaipl_text(resp: Dict[str, Any]) -> str:
    """
    Попытка достать "текст ответа" из разных форматов SMAIPL.
    Если не нашли — вернем JSON как строку.
    """
    if not isinstance(resp, dict):
        return str(resp)

    # Частые варианты полей (на случай изменений формата):
    for key in ("answer", "text", "message", "result", "data"):
        if key in resp and isinstance(resp[key], str) and resp[key].strip():
            return resp[key].strip()

    # Иногда бывает вложенная структура
    if "done" in resp and isinstance(resp["done"], str) and resp["done"].strip():
        return resp["done"].strip()

    return json.dumps(resp, ensure_ascii=False)


# =========================
# Telegram handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я Summary Bot. Пиши сообщения и используй /summary для сводки.")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text("Готовлю summary...")

    history_text = _get_last_messages(chat_id, SUMMARY_LAST_N)
    if not history_text:
        await update.message.reply_text("История пуста — пришли несколько сообщений, затем повтори /summary.")
        return

    resp = await call_smaipl(SMAIPL_BOT_ID, str(chat_id), history_text)

    # Если SMAIPL вернул {"error": true} — покажем аккуратно
    if isinstance(resp, dict) and resp.get("error") is True:
        await update.message.reply_text(f"Ответ SMAIPL: {json.dumps(resp, ensure_ascii=False)}")
        return

    text = extract_smaipl_text(resp)
    if not text:
        text = f"Ответ SMAIPL: {json.dumps(resp, ensure_ascii=False)}"

    # Telegram ограничивает длину сообщений; режем мягко
    max_len = 3500
    if len(text) <= max_len:
        await update.message.reply_text(text)
    else:
        parts = [text[i:i + max_len] for i in range(0, len(text), max_len)]
        for p in parts:
            await update.message.reply_text(p)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    _append_history(chat_id, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception in handler: %s", context.error)


# =========================
# FastAPI webhook server
# =========================

api = FastAPI(title="Summary Bot Webhook")

tg_app: Optional[Application] = None


@api.get("/health")
async def health():
    return {"ok": True}


@api.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # Проверка секретного токена (если используем)
    if WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    data = await request.json()

    if tg_app is None:
        raise HTTPException(status_code=503, detail="Telegram application not ready")

    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


# =========================
# Startup / Shutdown
# =========================

@api.on_event("startup")
async def on_startup():
    global tg_app

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # Создаём telegram application
    tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    tg_app.add_error_handler(on_error)
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler(SUMMARY_COMMAND.lstrip("/"), summary_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Инициализируем и запускаем (без polling!)
    await tg_app.initialize()
    await tg_app.start()

    # Ставим webhook (если есть публичный URL)
    if PUBLIC_BASE_URL:
        webhook_url = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"
        log.info("Setting webhook to: %s", webhook_url)
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        log.info("Webhook set OK")
    else:
        log.warning(
            "PUBLIC_BASE_URL is not set. Webhook will NOT be set. "
            "Expose the service in Railway and set PUBLIC_BASE_URL=https://<your-domain>"
        )

    me = await tg_app.bot.get_me()
    log.info("Bot started: @%s (id=%s). Webhook mode.", me.username, me.id)


@api.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app is not None:
        try:
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            log.exception("Error during telegram shutdown")


def main():
    # FastAPI сервер должен слушать 0.0.0.0:$PORT
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
