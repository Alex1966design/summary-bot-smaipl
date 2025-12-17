import os
import json
import time
import sqlite3
import logging
from typing import Optional, List, Tuple

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
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
# Env / Config
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")  # e.g. https://charismatic-smile-production.up.railway.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # random string
PORT = int(os.getenv("PORT", "8080"))

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()  # optional; if empty, we won't send it
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()  # optional; if empty, we won't send it
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "60"))

# DB for storing recent messages per chat (so /summary can work without reply)
DB_PATH = os.getenv("SQLITE_PATH", "/tmp/summary_bot.sqlite3")
MAX_STORED_PER_CHAT = int(os.getenv("MAX_STORED_PER_CHAT", "50"))


def require_env():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is missing (e.g. https://<app>.up.railway.app)")
    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET is missing (random string)")
    if not SMAIPL_API_URL:
        raise RuntimeError("SMAIPL_API_URL is missing (your SMAIPL endpoint)")


# -----------------------------
# SQLite helpers
# -----------------------------
def db_init():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                ts INTEGER NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts)")
        conn.commit()
    finally:
        conn.close()


def db_add_message(chat_id: int, message_id: int, user_id: Optional[int], username: str, text: str):
    if not text:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages(chat_id, message_id, user_id, username, text, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, user_id, username or "", text, int(time.time())),
        )
        # keep last MAX_STORED_PER_CHAT
        cur.execute(
            """
            DELETE FROM messages
            WHERE rowid IN (
                SELECT rowid
                FROM messages
                WHERE chat_id = ?
                ORDER BY ts DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (chat_id, MAX_STORED_PER_CHAT),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_last_user_text(chat_id: int) -> Optional[str]:
    """Return last non-command text message for this chat."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT text
            FROM messages
            WHERE chat_id = ?
              AND text IS NOT NULL
              AND TRIM(text) != ''
              AND text NOT LIKE '/%'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (chat_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# -----------------------------
# SMAIPL call
# -----------------------------
def _extract_smaipl_text(data) -> Optional[str]:
    """
    SMAIPL may return different shapes; we try a few common fields.
    In your screenshot example, they used .json()['done'].
    """
    if data is None:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if data.get("error") is True:
            return None
        for key in ("done", "result", "answer", "text", "message", "output"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # sometimes nested
        for key in ("data", "response"):
            nested = data.get(key)
            if isinstance(nested, dict):
                t = _extract_smaipl_text(nested)
                if t:
                    return t
    return None


def call_smaipl(prompt: str) -> str:
    payload = {"message": prompt}
    # If your SMAIPL endpoint requires these fields — set them in Railway Variables
    if SMAIPL_BOT_ID:
        payload["bot_id"] = int(SMAIPL_BOT_ID) if SMAIPL_BOT_ID.isdigit() else SMAIPL_BOT_ID
    if SMAIPL_CHAT_ID:
        payload["chat_id"] = SMAIPL_CHAT_ID

    with httpx.Client(timeout=SMAIPL_TIMEOUT) as client:
        r = client.post(SMAIPL_API_URL, json=payload)
        # even on 200 SMAIPL might return {"error": true}
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"SMAIPL returned non-JSON: status={r.status_code}, body={r.text[:300]}")

    text = _extract_smaipl_text(data)
    if not text:
        raise RuntimeError(f"SMAIPL error/empty response: status={r.status_code}, body={json.dumps(data, ensure_ascii=False)[:500]}")
    return text


# -----------------------------
# Telegram handlers
# -----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет. Я Summary Bot.\n\n"
        "Как использовать:\n"
        "1) Ответь (Reply) на нужное сообщение командой /summary — сделаю краткое резюме.\n"
        "2) Или отправь /summary без Reply — я суммаризирую последнее сообщение в чате.\n"
    )


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id

    # 1) Prefer replied message text
    target_text = None
    if msg.reply_to_message and msg.reply_to_message.text:
        target_text = msg.reply_to_message.text.strip()

    # 2) Else: last user text from DB
    if not target_text:
        target_text = db_get_last_user_text(chat_id)

    if not target_text:
        await msg.reply_text("Не вижу текста для суммаризации. Ответь (Reply) на сообщение и отправь /summary.")
        return

    await msg.reply_text("Готовлю summary...")

    # Build prompt for SMAIPL
    prompt = (
        "Сделай краткое резюме текста на русском.\n"
        "Формат:\n"
        "1) Суть (1-2 предложения)\n"
        "2) Ключевые пункты (5-7 буллетов)\n"
        "3) Следующие шаги (если применимо, 3-5 буллетов)\n\n"
        f"Текст:\n{target_text}"
    )

    try:
        # run sync HTTP in threadpool
        resp = await context.application.run_in_threadpool(call_smaipl, prompt)
        await msg.reply_text(resp, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.exception("Summary failed")
        await msg.reply_text(f"Ошибка при генерации summary: {e}")


async def store_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    # store any plain text (including long messages); commands are stored too, but excluded by query
    db_add_message(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        user_id=msg.from_user.id if msg.from_user else None,
        username=msg.from_user.username if msg.from_user else "",
        text=msg.text,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)


# -----------------------------
# FastAPI + Webhook bridge
# -----------------------------
app = FastAPI()
tg_app: Optional[Application] = None


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # 1) Path secret
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 2) Optional header secret (Telegram sends it if setWebhook secret_token used)
    if x_telegram_bot_api_secret_token and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret token")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


@app.on_event("startup")
async def on_startup():
    global tg_app
    require_env()
    db_init()

    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("summary", summary_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_text))
    tg_app.add_error_handler(error_handler)

    await tg_app.initialize()
    await tg_app.start()

    webhook_url = f"{PUBLIC_BASE_URL}/webhook/{WEBHOOK_SECRET}"

    # setWebhook with secret_token => Telegram will send header X-Telegram-Bot-Api-Secret-Token
    await tg_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    me = await tg_app.bot.get_me()
    logger.info("Bot started: @%s (id=%s)", me.username, me.id)
    logger.info("Webhook set to: %s", webhook_url)


@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        try:
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            logger.exception("Shutdown error")


if __name__ == "__main__":
    # Important: use PORT env directly (Railway sets it), do NOT pass "$PORT" as a literal string
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
