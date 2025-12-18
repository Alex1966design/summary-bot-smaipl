import os
import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# SMAIPL
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()  # полный URL на /ask/....
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()    # например 5129
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()  # например ask123456
SMAIPL_RESPONSE_FIELD = os.getenv("SMAIPL_RESPONSE_FIELD", "done").strip()  # "done" по умолчанию

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty. Bot will not work without it.")
if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is empty. Webhook cannot be set without it.")
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET is empty. Webhook path protection is disabled (NOT recommended).")

# ----------------------------
# Telegram Application (PTB)
# ----------------------------
tg_app = Application.builder().token(BOT_TOKEN).build()

# ----------------------------
# SMAIPL Call
# ----------------------------
def call_smaipl(prompt: str) -> str:
    """
    Синхронный вызов SMAIPL (запускаем в threadpool).
    Ожидаем, что SMAIPL_API_URL уже содержит полный endpoint /ask/<...>.
    """
    if not SMAIPL_API_URL:
        raise RuntimeError("SMAIPL_API_URL is not set")
    if not SMAIPL_BOT_ID or not SMAIPL_CHAT_ID:
        raise RuntimeError("SMAIPL_BOT_ID / SMAIPL_CHAT_ID is not set")

    payload = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": SMAIPL_CHAT_ID,
        "message": prompt,
    }

    with httpx.Client(timeout=60.0) as client:
        r = client.post(SMAIPL_API_URL, json=payload)
        r.raise_for_status()
        data = r.json()

    # типичная ошибка у тебя: {"error": true}
    if isinstance(data, dict) and data.get("error") is True:
        # отдадим максимум полезного
        raise RuntimeError(f"SMAIPL returned error=true. Payload={payload}. Response={data}")

    # берём поле ответа
    if isinstance(data, dict) and SMAIPL_RESPONSE_FIELD in data:
        val = data.get(SMAIPL_RESPONSE_FIELD)
        return str(val)

    # запасной вариант: если поле не найдено
    return json.dumps(data, ensure_ascii=False)


# ----------------------------
# Handlers
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — отправляй *ответом (reply)* на сообщение, которое нужно суммаризировать.",
        parse_mode="Markdown",
    )


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    # Требуем, чтобы /summary было reply на сообщение
    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text("Команду /summary нужно отправлять *ответом* на сообщение для суммаризации.")
        return

    source_text = msg.reply_to_message.text.strip()
    if not source_text:
        await msg.reply_text("Не вижу текста для суммаризации (reply-сообщение пустое).")
        return

    await msg.reply_text("Готовлю summary...")

    prompt = (
        "Суммаризируй текст кратко, структурировано, по пунктам. "
        "Выдели ключевые решения/действия (если есть).\n\n"
        f"ТЕКСТ:\n{source_text}"
    )

    try:
        # ВАЖНО: run_in_threadpool из starlette, а не из telegram Application
        result = await run_in_threadpool(call_smaipl, prompt)
        await msg.reply_text(result)
    except Exception as e:
        log.exception("Summary generation failed")
        await msg.reply_text(f"Ошибка при генерации summary: {e}")


tg_app.add_handler(CommandHandler("start", start_cmd))
tg_app.add_handler(CommandHandler("summary", summary_cmd))

# ----------------------------
# FastAPI Webhook Server
# ----------------------------
api = FastAPI()


@api.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@api.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    # 1) Проверка секрета в URL
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # 2) Доп. проверка секрета в заголовке (Telegram умеет так)
    # Если хочешь строго — раскомментируй блок ниже:
    # if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
    #     raise HTTPException(status_code=403, detail="Invalid secret token header")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)

    # ВАЖНО: webhook-режим — просто "скармливаем" update в PTB
    await tg_app.process_update(update)
    return {"ok": True}


# ----------------------------
# Startup: set webhook
# ----------------------------
@api.on_event("startup")
async def on_startup() -> None:
    # запускаем PTB "внутри" (без polling)
    await tg_app.initialize()
    await tg_app.start()

    if not (PUBLIC_BASE_URL and BOT_TOKEN):
        log.warning("Skipping setWebhook: PUBLIC_BASE_URL or BOT_TOKEN missing")
        return

    webhook_path = f"/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/webhook/no-secret"
    webhook_url = f"{PUBLIC_BASE_URL}{webhook_path}"

    # setWebhook через Bot API
    try:
        # secret_token можно передать Telegram, он будет присылать его в заголовке
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=(WEBHOOK_SECRET if WEBHOOK_SECRET else None),
            drop_pending_updates=True,
        )
        log.info(f"Webhook set to: {webhook_url}")
    except Exception:
        log.exception("Failed to set webhook")


@api.on_event("shutdown")
async def on_shutdown() -> None:
    try:
        await tg_app.stop()
        await tg_app.shutdown()
    except Exception:
        log.exception("Shutdown error")


# ----------------------------
# Entrypoint (Railway)
# ----------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(api, host="0.0.0.0", port=port)
