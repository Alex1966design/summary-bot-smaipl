import os
import re
import json
import asyncio
import logging
from typing import Any, Dict, List, Optional
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

# ==========================================================
# 0) LOGGING (must be at the very top of the entrypoint)
# ==========================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class RedactTelegramTokenFilter(logging.Filter):
    """
    Extra safety: redact Telegram bot tokens if they appear in log messages.
    Token pattern: digits ':' base64url-like string.
    """
    _token_re = re.compile(r"(\b\d{6,}:[A-Za-z0-9_-]{20,}\b)")
    _bot_url_re = re.compile(r"(https://api\.telegram\.org/bot)(\d{6,}:[A-Za-z0-9_-]{20,})")

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
# Force root level in case something else sets DEBUG later
logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# CRITICAL: prevent httpx/httpcore from logging full request URLs (which include bot token)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

log = logging.getLogger("summary_bot")
log.addFilter(RedactTelegramTokenFilter())

# ==========================================================
# 1) ENV / CONFIG
# ==========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()  # ключ/токен SMAIPL (если нужен)
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "30"))
SMAIPL_VERIFY_SSL = os.getenv("SMAIPL_VERIFY_SSL", "true").lower() in ("1", "true", "yes")

SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary").strip()  # "/summary"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))             # сколько всего храним
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))            # сколько брать для summary

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

# ==========================================================
# 2) STATE (in-memory history + per-chat lock)
# ==========================================================

chat_history: Dict[int, List[Dict[str, str]]] = defaultdict(list)
chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def add_history(chat_id: int, user: str, text: str) -> None:
    chat_history[chat_id].append({"user": user, "text": text})
    if len(chat_history[chat_id]) > HISTORY_LIMIT:
        chat_history[chat_id] = chat_history[chat_id][-HISTORY_LIMIT:]

def build_context(chat_id: int) -> str:
    items = chat_history.get(chat_id, [])
    items = items[-SUMMARY_LAST_N:] if SUMMARY_LAST_N > 0 else items

    lines: List[str] = []
    for it in items:
        u = (it.get("user") or "user").strip()
        t = (it.get("text") or "").strip()
        if t:
            lines.append(f"{u}: {t}")
    return "\n".join(lines).strip()

# ==========================================================
# 3) SMAIPL client (async)
# ==========================================================

async def smaipl_generate_summary(chat_id: int, context_text: str) -> str:
    if not SMAIPL_API_URL:
        raise RuntimeError("SMAIPL_API_URL is not configured")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Если SMAIPL требует авторизацию — используем Bearer.
    # Если у SMAIPL другой формат — поменяем здесь, в одном месте.
    if SMAIPL_BOT_ID:
        headers["Authorization"] = f"Bearer {SMAIPL_BOT_ID}"

    # ВАЖНО: payload может отличаться в SMAIPL — это частая причина 400.
    # Сейчас оставляю максимально совместимый вариант (как у тебя было: bot_id/chat_id/message).
    payload = {
        "bot_id": SMAIPL_BOT_ID or "unknown",
        "chat_id": str(chat_id),
        "message": context_text,
    }

    # Безопасный лог: не печатаем context целиком
    log.info("SMAIPL request: chat_id=%s, chars=%s, last_n=%s", chat_id, len(context_text), SUMMARY_LAST_N)

    async with httpx.AsyncClient(timeout=SMAIPL_TIMEOUT, verify=SMAIPL_VERIFY_SSL) as client:
        r = await client.post(SMAIPL_API_URL, headers=headers, json=payload)

    if r.status_code == 400:
        # Обязательно показываем текст ошибки SMAIPL — это ключ к исправлению схемы payload
        raise RuntimeError(f"SMAIPL 400: {r.text}")

    r.raise_for_status()

    # Пытаемся извлечь текст summary из JSON
    try:
        data = r.json()
    except Exception:
        return r.text.strip()

    if isinstance(data, dict):
        for key in ("summary", "result", "text", "answer", "output"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    # fallback: отдадим JSON как строку
    return json.dumps(data, ensure_ascii=False)

# ==========================================================
# 4) Telegram handlers
# ==========================================================

async def on_startup(app):
    # Критично для стабильности: отключаем webhook и чистим pending updates
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    log.info("Bot started: @%s (id=%s). Webhook cleared.", me.username, me.id)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Напиши несколько сообщений, затем используй /summary для сводки.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды:\n"
                                    "/summary — сделать сводку по последним сообщениям\n"
                                    "/clear — очистить историю чата\n"
                                    "/ping — проверка бота")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        chat_history.pop(update.effective_chat.id, None)
    await update.message.reply_text("История очищена.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    # не пишем команды в историю
    if text.startswith("/"):
        return
    user = update.effective_user.full_name if update.effective_user else "user"
    add_history(update.effective_chat.id, user, text)

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    # 1 summary at a time per chat
    async with chat_locks[chat_id]:
        await update.message.reply_text("Готовлю summary…")

        ctx = build_context(chat_id)
        if not ctx:
            await update.message.reply_text("Недостаточно истории. Напиши несколько сообщений и повтори /summary.")
            return

        try:
            result = await smaipl_generate_summary(chat_id, ctx)
        except Exception as e:
            log.exception("Summary failed chat_id=%s", chat_id)
            await update.message.reply_text(f"Ошибка генерации summary: {e}")
            return

        # Telegram limit safety
        if len(result) > 3900:
            result = result[:3900] + "\n\n[сообщение обрезано]"

        await update.message.reply_text(result)

# ==========================================================
# 5) Main
# ==========================================================

def main():
    cmd = SUMMARY_COMMAND.lstrip("/") or "summary"

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler(cmd, summary_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Polling: one instance only (Railway: scale=1)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
