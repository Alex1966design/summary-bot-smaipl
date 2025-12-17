import os
import logging
import asyncio
import secrets
import requests

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# --------------------
# CONFIG
# --------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]  # https://charismatic-smile-production.up.railway.app
PORT = int(os.environ.get("PORT", 8080))

SMAIPL_API_URL = os.environ.get("SMAIPL_API_URL")

WEBHOOK_SECRET = os.environ.get(
    "WEBHOOK_SECRET",
    secrets.token_hex(16)
)

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"

# --------------------
# LOGGING
# --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("summary_bot")

# --------------------
# FASTAPI
# --------------------
app = FastAPI()

# --------------------
# TELEGRAM APP
# --------------------
tg_app = Application.builder().token(BOT_TOKEN).build()


# --------------------
# COMMANDS
# --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Используй /summary ответом на сообщение, которое нужно суммаризировать."
    )


def call_smaipl(prompt: str, chat_id: int) -> str:
    payload = {
        "bot_id": 5129,
        "chat_id": f"tg_{chat_id}",
        "message": prompt,
    }
    r = requests.post(SMAIPL_API_URL, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        return "Ошибка при обработке текста."
    return data.get("done", "Пустой ответ.")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Команду /summary нужно отправлять *ответом* на сообщение для суммаризации.",
            parse_mode="Markdown",
        )
        return

    text = update.message.reply_to_message.text
    chat_id = update.message.chat_id

    await update.message.reply_text("Готовлю summary...")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, call_smaipl, text, chat_id)

    await update.message.reply_text(result)


tg_app.add_handler(CommandHandler("start", start_cmd))
tg_app.add_handler(CommandHandler("summary", summary_cmd))


# --------------------
# WEBHOOK ENDPOINT
# --------------------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


# --------------------
# STARTUP
# --------------------
@app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    await tg_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
    )
    logger.info(f"Webhook set to: {WEBHOOK_URL}")


@app.on_event("shutdown")
async def on_shutdown():
    await tg_app.shutdown()


# --------------------
# LOCAL ENTRY (Docker)
# --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
