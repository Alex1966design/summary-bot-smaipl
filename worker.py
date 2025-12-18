import os
import asyncio
import logging
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("summary_bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

# ---------- Telegram handlers ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Команду /summary нужно отправлять *ответом* на сообщение.",
        parse_mode="Markdown"
    )

def call_smaipl(prompt: str) -> str:
    # TODO: здесь твоя реальная логика SMAIPL
    return f"📝 Сводка:\n\n{prompt[:500]}"

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Команду /summary нужно отправлять *ответом* на сообщение для суммаризации."
        )
        return

    original_text = update.message.reply_to_message.text
    await update.message.reply_text("Готовлю summary...")

    try:
        result = await asyncio.to_thread(call_smaipl, original_text)
        await update.message.reply_text(result)
    except Exception as e:
        logger.exception("Summary error")
        await update.message.reply_text(f"Ошибка при генерации summary: {e}")

# ---------- App / FastAPI ----------

application = Application.builder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("summary", summary_cmd))

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await application.initialize()
    await application.bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}",
        drop_pending_updates=True
    )
    await application.start()
    logger.info(f"Webhook set to {PUBLIC_BASE_URL}{WEBHOOK_PATH}")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}
