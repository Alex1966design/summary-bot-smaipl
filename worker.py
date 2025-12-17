import os
import re
import json
import asyncio
import logging
from typing import Dict, List
from collections import defaultdict

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------------------
# Logging (entrypoint)
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class RedactTelegramTokenFilter(logging.Filter):
    _bot_url_re = re.compile(r"(https://api\.telegram\.org/bot)(\d{6,}:[A-Za-z0-9_-]{20,})")
    _token_re = re.compile(r"(\b\d{6,}:[A-Za-z0-9_-]{20,}\b)")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            msg = self._bot_url_re.sub(r"\1[REDACTED]", msg)
            msg = self._token_re.sub("[REDACTED]", msg)
            record.msg = msg
            record.args = ()
        except Exception:
            pass
        return True

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# IMPORTANT: prevent httpx/httpcore from logging request URLs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

log = logging.getLogger("summary_bot")
log.addFilter(RedactTelegramTokenFilter())

# ----------------------------
# Config
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()

SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "30"))
SMAIPL_VERIFY_SSL = os.getenv("SMAIPL_VERIFY_SSL", "true").lower() in ("1", "true", "yes")

SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary").strip()
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not SMAIPL_API_URL:
    log.warning("SMAIPL_API_URL is not set. /summary will return fallback only.")

# ----------------------------
# State: history + locks
# ----------------------------
chat_history: Dict[int, List[Dict[str, str]]] = defaultdict(list)
chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def add_history(chat_id: int, user: str, text: str) -> None:
    chat_history[chat_id].append({"user": user, "text": text})
    if len(chat_history[chat_id]) > HISTORY_LIMIT:
        chat_history[chat_id] = chat_history[chat_id][-HISTORY_LIMIT:]

def build_context(chat_id: int) -> str:
    items = chat_history.get(chat_id, [])
    items = items[-SUMMARY_LAST_N:] if SUMMARY_LAST_N > 0 else items
    lines = []
    for it in items:
        u = (it.get("user") or "user").strip()
        t = (it.get("text") or "").strip()
        if t:
            lines.append(f"{u}: {t}")
    return "\n".join(lines).strip()

def fallback_summary(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "Нет данных для summary."
    last = lines[-10:]
    out = ["Summary (fallback):", ""]
    out += [f"• {ln[:300]}" for ln in last]
    return "\n".join(out)

# ----------------------------
# SMAIPL call (STRICT per provided contract)
# ----------------------------
async def smaipl_ask(chat_id: int, context_text: str) -> str:
    """
    SMAIPL contract (from your example):
    POST {SMAIPL_API_URL}
    json={"bot_id": <int>, "chat_id": "<str>", "message": "<text>"}
    response.json()["done"]
    """
    if not SMAIPL_API_URL:
        raise RuntimeError("SMAIPL_API_URL is not configured")
    if not SMAIPL_BOT_ID:
        raise RuntimeError("SMAIPL_BOT_ID is not configured (expected numeric bot_id like 5129)")

    try:
        bot_id_int = int(SMAIPL_BOT_ID)
    except ValueError:
        raise RuntimeError("SMAIPL_BOT_ID must be an integer (e.g., 5129)")

    payload = {
        "bot_id": bot_id_int,
        "chat_id": f"tg_{chat_id}",     # SMAIPL ожидает строку; уникальный id чата
        "message": context_text,
    }

    log.info("SMAIPL ask: chat_id=%s chars=%s", chat_id, len(context_text))

    async with httpx.AsyncClient(timeout=SMAIPL_TIMEOUT, verify=SMAIPL_VERIFY_SSL) as client:
        r = await client.post(SMAIPL_API_URL, json=payload)

    # Helpful diagnostics (safe)
    if r.status_code >= 400:
        raise RuntimeError(f"SMAIPL HTTP {r.status_code}: {r.text[:500]}")

    try:
        data = r.json()
    except Exception:
        return r.text.strip()

    # Per contract: field "done"
    done = None
    if isinstance(data, dict):
        done = data.get("done")

    if isinstance(done, str) and done.strip():
        return done.strip()

    # If SMAIPL returns {"error": true}, bubble it up clearly
    return json.dumps(data, ensure_ascii=False)

# ----------------------------
# Telegram handlers
# ----------------------------
async def on_startup(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    log.info("Bot started: @%s (id=%s). Webhook cleared.", me.username, me.id)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Напиши несколько сообщений и используй /summary для сводки.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    user = update.effective_user.full_name if update.effective_user else "user"
    add_history(update.effective_chat.id, user, text)

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    async with chat_locks[chat_id]:
        await update.message.reply_text("Готовлю summary…")

        ctx = build_context(chat_id)
        if not ctx:
            await update.message.reply_text("Недостаточно истории. Напиши несколько сообщений и повтори /summary.")
            return

        try:
            result = await smaipl_ask(chat_id, ctx)
            # If SMAIPL returns JSON like {"error": true}, show readable text
            if result.strip().startswith("{") and result.strip().endswith("}"):
                await update.message.reply_text(f"Ответ SMAIPL: {result}")
            else:
                await update.message.reply_text(result[:3900])
        except Exception as e:
            log.exception("SMAIPL failed; using fallback")
            out = fallback_summary(ctx) + f"\n\n[SMAIPL error: {e}]"
            await update.message.reply_text(out[:3900])

def main():
    cmd = SUMMARY_COMMAND.lstrip("/") or "summary"

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler(cmd, summary_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
