import os
import json
import logging
from typing import Any, Dict, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("summary_bot")

# -----------------------------
# Env vars (required)
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# Optional SMAIPL tuning (you may leave these unset)
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()         # optional
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()       # optional
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "60"))

# Railway port
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing env TELEGRAM_BOT_TOKEN")
if not SMAIPL_API_URL:
    raise RuntimeError("Missing env SMAIPL_API_URL")
if not PUBLIC_BASE_URL:
    raise RuntimeError("Missing env PUBLIC_BASE_URL (example: https://xxxx.up.railway.app)")
if not WEBHOOK_SECRET:
    raise RuntimeError("Missing env WEBHOOK_SECRET (generate random string)")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"

# -----------------------------
# Telegram handlers
# -----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Как использовать:\n"
        "1) Ответь на сообщение, которое нужно суммировать\n"
        "2) Напиши команду /summary\n"
    )

def _extract_text_from_replied_message(update: Update) -> Optional[str]:
    msg = update.effective_message
    if not msg:
        return None
    replied = getattr(msg, "reply_to_message", None)
    if not replied:
        return None
    if getattr(replied, "text", None):
        return replied.text
    if getattr(replied, "caption", None):
        return replied.caption
    return None

def _best_effort_extract_answer(data: Any) -> str:
    """
    SMAIPL may return different shapes.
    We try typical fields; otherwise return pretty JSON.
    """
    if isinstance(data, dict):
        # common candidates
        for key in ("done", "answer", "result", "text", "message", "output"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        # sometimes nested
        for key in ("data", "response"):
            val = data.get(key)
            if isinstance(val, dict):
                nested = _best_effort_extract_answer(val)
                if nested:
                    return nested

        # explicit error
        if data.get("error") is True:
            return f"Ответ SMAIPL: {json.dumps(data, ensure_ascii=False)}"

    # fallback
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)

async def call_smaipl(text: str, chat_id: Optional[str] = None) -> str:
    """
    Calls SMAIPL endpoint.
    Minimal payload based on what you shared:
      {"bot_id": 5129, "chat_id": "ask123456", "message": "..."}

    If SMAIPL_BOT_ID / SMAIPL_CHAT_ID are not set, we still try to send
    message-only or infer chat_id from Telegram chat id.
    """
    payload: Dict[str, Any] = {"message": text}

    # bot_id (optional)
    if SMAIPL_BOT_ID:
        try:
            payload["bot_id"] = int(SMAIPL_BOT_ID)
        except ValueError:
            logger.warning("SMAIPL_BOT_ID is not int; ignoring")

    # chat_id (optional)
    if SMAIPL_CHAT_ID:
        payload["chat_id"] = SMAIPL_CHAT_ID
    elif chat_id:
        payload["chat_id"] = str(chat_id)

    logger.info("SMAIPL request -> %s | keys=%s", SMAIPL_API_URL, list(payload.keys()))

    async with httpx.AsyncClient(timeout=SMAIPL_TIMEOUT) as client:
        r = await client.post(SMAIPL_API_URL, json=payload)
        # SMAIPL sometimes returns JSON even on non-200; try to parse anyway
        try:
            data = r.json()
        except Exception:
            data = {"http_status": r.status_code, "text": r.text}

    return _best_effort_extract_answer(data)

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    await msg.reply_text("Готовлю summary...")

    text = _extract_text_from_replied_message(update)
    if not text:
        await msg.reply_text("Команду /summary нужно отправлять *ответом* на сообщение для суммаризации.")
        return

    try:
        tg_chat_id = str(update.effective_chat.id) if update.effective_chat else None
        summary = await call_smaipl(text, chat_id=tg_chat_id)
        await msg.reply_text(summary)
    except Exception as e:
        logger.exception("Summary failed")
        await msg.reply_text(f"Ошибка при создании summary: {e}")

# -----------------------------
# FastAPI webhook app
# -----------------------------
app = FastAPI()
tg_app: Optional[Application] = None

@app.get("/health")
async def health():
    return {"ok": True, "service": "summary-bot", "webhook": WEBHOOK_PATH}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    global tg_app
    if tg_app is None:
        raise HTTPException(status_code=503, detail="Telegram app not ready")

    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update = Update.de_json(payload, tg_app.bot)
    await tg_app.process_update(update)
    return JSONResponse({"ok": True})

@app.on_event("startup")
async def on_startup():
    global tg_app

    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("summary", summary_cmd))

    # Initialize & start internal PTB machinery
    await tg_app.initialize()
    await tg_app.start()

    # IMPORTANT: set webhook (no polling)
    try:
        me = await tg_app.bot.get_me()
        logger.info("Bot started: @%s (id=%s)", me.username, me.id)
    except Exception:
        logger.warning("Bot get_me failed (but continuing)")

    logger.info("Setting webhook to: %s", WEBHOOK_URL)
    await tg_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
    )
    logger.info("Webhook set to: %s", WEBHOOK_URL)

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app is None:
        return
    try:
        await tg_app.stop()
    finally:
        await tg_app.shutdown()
    tg_app = None

if __name__ == "__main__":
    # Railway expects the process to bind PORT
    uvicorn.run(app, host="0.0.0.0", port=PORT)
