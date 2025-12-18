import os
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

SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "gpt-4o-mini").strip()

SMAIPL_CHAT_URL = "https://ai.smaipl.ru/v1/chat/completions"

# ----------------------------
# SMAIPL Call (LLM)
# ----------------------------
def call_smaipl(prompt: str) -> str:
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is not set")

    payload = {
        "model": SMAIPL_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "Ты помощник, который делает краткие структурированные summary."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=60.0) as client:
        r = client.post(SMAIPL_CHAT_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    return data["choices"][0]["message"]["content"].strip()

# ----------------------------
# Telegram handlers
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — отправь *ответом (reply)* на сообщение, "
        "которое нужно суммаризировать.",
        parse_mode="Markdown",
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text(
            "Команду /summary нужно отправлять *ответом* на сообщение.",
            parse_mode="Markdown",
        )
        return

    source_text = msg.reply_to_message.text.strip()
    await msg.reply_text("Готовлю summary...")

    prompt = (
        "Суммаризируй текст кратко, структурировано, по пунктам. "
        "Выдели ключевые выводы и действия.\n\n"
        f"ТЕКСТ:\n{source_text}"
    )

    try:
        result = await run_in_threadpool(call_smaipl, prompt)
        await msg.reply_text(result)
    except Exception as e:
        log.exception("Summary failed")
        await msg.reply_text(f"Ошибка генерации summary: {e}")

# ----------------------------
# FastAPI
# ----------------------------
app = FastAPI()
tg_app: Optional[Application] = None

def build_tg_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("summary", summary_cmd))
    return application

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}

@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    global tg_app
    tg_app = build_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    webhook_url = f"{PUBLIC_BASE_URL}/webhook/{WEBHOOK_SECRET}"
    await tg_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    log.info(f"Webhook set to {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
