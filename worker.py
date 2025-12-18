import os
import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

# ============================================================
# ENV
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# SMAIPL (ask-style endpoint)
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()

# OpenAI-compatible SMAIPL
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "").strip().rstrip("/")
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "gpt-4o-mini").strip()

# ============================================================
# Helpers
# ============================================================
def _require_env(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"{name} is not set")

# ============================================================
# 1. Generate summary via SMAIPL (OpenAI-compatible API)
# ============================================================
def generate_summary(prompt: str) -> str:
    _require_env("SMAIPL_BASE_URL", SMAIPL_BASE_URL)
    _require_env("SMAIPL_API_KEY", SMAIPL_API_KEY)

    url = f"{SMAIPL_BASE_URL}/chat/completions"

    payload = {
        "model": SMAIPL_MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Суммаризируй текст кратко и структурировано по пунктам. "
                    "Выдели ключевые выводы и действия.\n\n"
                    f"{prompt}"
                ),
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()

    return data["choices"][0]["message"]["content"].strip()

# ============================================================
# 2. SEND summary back to SMAIPL (ask endpoint)
# ============================================================
def send_summary_to_smaipl(summary_text: str) -> str:
    """
    Формат строго как у SMAIPL:
    r.post(...).json()['done']
    """
    _require_env("SMAIPL_API_URL", SMAIPL_API_URL)
    _require_env("SMAIPL_BOT_ID", SMAIPL_BOT_ID)
    _require_env("SMAIPL_CHAT_ID", SMAIPL_CHAT_ID)

    payload = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": SMAIPL_CHAT_ID,
        "message": summary_text,
    }

    with httpx.Client(timeout=60.0) as client:
        r = client.post(SMAIPL_API_URL, json=payload)
        r.raise_for_status()
        data = r.json()

    if data.get("error") is True:
        raise RuntimeError(f"SMAIPL error=true, response={data}")

    return str(data.get("done", ""))

# ============================================================
# Telegram handlers
# ============================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — ответь (reply) на сообщение, которое нужно суммаризировать."
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message

    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text("Команду /summary нужно отправлять ответом на текст.")
        return

    source_text = msg.reply_to_message.text.strip()
    await msg.reply_text("Готовлю summary...")

    try:
        summary = await run_in_threadpool(generate_summary, source_text)
        await msg.reply_text(summary)

        # 🔥 отправляем summary обратно в SMAIPL
        done = await run_in_threadpool(send_summary_to_smaipl, summary)
        log.info("Summary sent to SMAIPL", extra={"done": done})

    except Exception as e:
        log.exception("Summary pipeline failed")
        await msg.reply_text(f"Ошибка: {e}")

# ============================================================
# FastAPI
# ============================================================
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
        raise HTTPException(status_code=403, detail="Invalid secret")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

# ============================================================
# Startup / Shutdown
# ============================================================
@app.on_event("startup")
async def on_startup() -> None:
    global tg_app

    tg_app = build_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    webhook_path = f"/webhook/{WEBHOOK_SECRET}"
    webhook_url = f"{PUBLIC_BASE_URL}{webhook_path}"

    await tg_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )

    log.info(f"Webhook set to {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown() -> None:
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
