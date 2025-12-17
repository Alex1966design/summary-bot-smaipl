import os
import json
import asyncio
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

# -------------------------
# ENV
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
# Пример: https://charismatic-smile-production.up.railway.app

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
# Это часть URL пути: /webhook/<WEBHOOK_SECRET>

# Telegram "secret_token" (опционально). Если задан — будем проверять заголовок.
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip()

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
# Пример: https://api.smaipl.ru/api/v1.0/ask/<...>

SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()  # например 5129
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()  # например ask123456
SMAIPL_RESPONSE_FIELD = os.getenv("SMAIPL_RESPONSE_FIELD", "done").strip()
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "60"))

PORT = int(os.getenv("PORT", "8080"))

# -------------------------
# Validation
# -------------------------
if not TELEGRAM_TOKEN:
    log.warning("TELEGRAM_TOKEN is empty. Bot will not start correctly.")

if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is empty. Webhook setup will fail.")

if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET is empty. Webhook path will be insecure/invalid.")

if not SMAIPL_API_URL:
    log.warning("SMAIPL_API_URL is empty. /summary will return error.")

# -------------------------
# FastAPI
# -------------------------
app = FastAPI()
tg_app: Optional[Application] = None


# -------------------------
# SMAIPL call (sync function for to_thread)
# -------------------------
def call_smaipl_sync(prompt: str) -> Dict[str, Any]:
    """
    Синхронный вызов SMAIPL, чтобы запускать через asyncio.to_thread.
    Ожидаем, что SMAIPL возвращает JSON и итог лежит в поле SMAIPL_RESPONSE_FIELD (по умолчанию 'done').
    """
    if not SMAIPL_API_URL:
        return {"error": True, "detail": "SMAIPL_API_URL is not set"}

    payload: Dict[str, Any] = {"message": prompt}

    # Если у вас SMAIPL требует bot_id/chat_id — добавляем
    if SMAIPL_BOT_ID:
        try:
            payload["bot_id"] = int(SMAIPL_BOT_ID)
        except ValueError:
            payload["bot_id"] = SMAIPL_BOT_ID

    if SMAIPL_CHAT_ID:
        payload["chat_id"] = SMAIPL_CHAT_ID

    try:
        with httpx.Client(timeout=SMAIPL_TIMEOUT) as client:
            r = client.post(SMAIPL_API_URL, json=payload)
            r.raise_for_status()
            data = r.json()
            return data
    except Exception as e:
        return {"error": True, "detail": str(e)}


# -------------------------
# Telegram handlers
# -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Как пользоваться:\n"
        "1) Ответь (Reply) на сообщение, которое нужно суммаризировать\n"
        "2) В ответе напиши команду: /summary\n\n"
        "Я отправлю текст в SMAIPL и верну результат."
    )


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    # Требуем reply
    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text("Команду /summary нужно отправлять *ответом* на сообщение для суммаризации.", parse_mode=ParseMode.MARKDOWN)
        return

    prompt = msg.reply_to_message.text.strip()
    if not prompt:
        await msg.reply_text("Пустой текст. Пришлите сообщение с текстом и ответьте на него /summary.")
        return

    await msg.reply_text("Готовлю summary...")

    # ВАЖНО: правильная замена run_in_threadpool -> asyncio.to_thread
    data = await asyncio.to_thread(call_smaipl_sync, prompt)

    if isinstance(data, dict) and data.get("error"):
        await msg.reply_text(f"Ошибка при генерации summary: {json.dumps(data, ensure_ascii=False)}")
        return

    # Достаём результат
    result = None
    if isinstance(data, dict):
        result = data.get(SMAIPL_RESPONSE_FIELD)
        # если поле другое — покажем весь JSON, чтобы быстро понять структуру
        if result is None:
            await msg.reply_text(
                "SMAIPL вернул JSON без ожидаемого поля.\n"
                f"Ожидали поле: {SMAIPL_RESPONSE_FIELD}\n\n"
                f"Ответ SMAIPL: {json.dumps(data, ensure_ascii=False)}"
            )
            return

    await msg.reply_text(str(result))


# -------------------------
# Startup / shutdown
# -------------------------
async def setup_telegram() -> None:
    global tg_app
    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("summary", summary_cmd))

    await tg_app.initialize()
    await tg_app.start()

    me = await tg_app.bot.get_me()
    log.info(f"Bot started: @{me.username} (id={me.id})")

    # setWebhook
    if PUBLIC_BASE_URL and WEBHOOK_SECRET:
        webhook_url = f"{PUBLIC_BASE_URL}/webhook/{WEBHOOK_SECRET}"
        kwargs: Dict[str, Any] = {"url": webhook_url, "drop_pending_updates": True}

        # Telegram secret_token (опционально)
        if WEBHOOK_SECRET_TOKEN:
            kwargs["secret_token"] = WEBHOOK_SECRET_TOKEN

        ok = await tg_app.bot.set_webhook(**kwargs)
        log.info(f"Webhook set to: {webhook_url} | ok={ok}")
    else:
        log.warning("PUBLIC_BASE_URL or WEBHOOK_SECRET not set; webhook NOT configured.")


async def shutdown_telegram() -> None:
    global tg_app
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
        tg_app = None


@app.on_event("startup")
async def on_startup() -> None:
    await setup_telegram()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await shutdown_telegram()


# -------------------------
# Routes
# -------------------------
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> JSONResponse:
    # 1) проверка секрета в пути
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Not found")

    # 2) (опционально) проверка Telegram secret_token
    # Telegram шлёт заголовок: X-Telegram-Bot-Api-Secret-Token
    if WEBHOOK_SECRET_TOKEN:
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_token != WEBHOOK_SECRET_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid secret token")

    # 3) обработка апдейта
    data = await request.json()
    if not tg_app:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)

    return JSONResponse({"ok": True})


# -------------------------
# Entrypoint
# -------------------------
def main() -> None:
    # uvicorn внутри процесса — Railway будет пинговать /health и webhook endpoint
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
