import os
import uuid
import logging
import asyncio
import requests

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.ext._application import Application as PTBApplication

# ---------------- CONFIG ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL")

if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("BOT_TOKEN or PUBLIC_BASE_URL is not set")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", uuid.uuid4().hex)
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"

PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("summary_bot")

# ---------------- FASTAPI ----------------

app = FastAPI()
tg_app: PTBApplication | None = None

# ---------------- SMAIPL ----------------

def call_smaipl(prompt: str, chat_id: int) -> str:
    try:
        payload = {
            "bot_id": 5129,
            "chat_id": str(chat_id),
            "message": prompt,
        }
        r = requests.post(SMAIPL_API_URL, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        return data.get("done") or "❌ Пустой ответ SMAIPL"
    except Exception as e:
        logger.exception("SMAIPL error")
        return f"❌ Ошибка SMAIPL: {e}"

# ---------------- HANDLERS ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Отправь текст и **ответь на него** командой /summary — я сделаю краткое резюме."
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Команду /summary нужно отправлять **ответом** на сообщение для суммаризации."
        )
        return

    source_text = update.message.reply_to_message.text
    chat_id = update.effective_chat.id

    await update.message.reply_text("Готовлю summary...")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, call_smaipl, source_text, chat_id
    )

    await update.message.reply_text(result)

# ---------------- WEBHOOK ----------------

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if tg_app is None:
        raise HTTPException(status_code=503, detail="Bot not ready")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}

# ---------------- STARTUP ----------------

@app.on_event("startup")
async def on_startup():
    global tg_app

    tg_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("summary", summary_cmd))

    await tg_app.initialize()
    await tg_app.bot.set_webhook(WEBHOOK_URL)

    logger.info(f"Webhook set to: {WEBHOOK_URL}")

@app.on_event("shutdown")
async def on_shutdown():
    if tg_app:
        await tg_app.shutdown()
