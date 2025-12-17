import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

logging.basicConfig(level=logging.INFO)

app = FastAPI()

application = Application.builder().token(TOKEN).build()

# ===== handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает через webhook ✅")

application.add_handler(CommandHandler("start", start))

# ===== webhook endpoint =====
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

# ===== startup =====
@app.on_event("startup")
async def on_startup():
    webhook_url = f"{PUBLIC_BASE_URL}/webhook"
    await application.bot.set_webhook(url=webhook_url)
    logging.info(f"Webhook set to {webhook_url}")
