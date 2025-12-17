import os
import re
import json
import asyncio
import logging
from typing import Any, Dict, Optional
from collections import defaultdict

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ----------------------------
# 1) Logging (must be FIRST)
# ----------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class RedactSecretsFilter(logging.Filter):
    """
    Extra safety: redacts 'bot<token>' fragments if they ever appear in logs.
    This is a last line of defence; primary defence is disabling httpx/httpcore debug.
    """
    _bot_token_re = re.compile(r"(bot)(\d{6,}:[A-Za-z0-9_-]{10,})")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            msg2 = self._bot_token_re.sub(r"\1[REDACTED]", msg)
            if msg2 != msg:
                # overwrite message safely
                record.msg = msg2
                record.args = ()
        except Exception:
            pass
        return True

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# Hard-disable noisy low-level HTTP logs that can leak URLs/tokens
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

log = logging.getLogger("summary_bot")
log.addFilter(RedactSecretsFilter())

# ----------------------------
# 2) Env / Config
# ----------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()  # токен/ключ SMAIPL (как у тебя заведено)
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "30"))
SMAIPL_VERIFY_SSL = os.getenv("SMAIPL_VERIFY_SSL", "true").lower() in ("1", "true", "yes")

SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary").strip()
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not SMAIPL_API_URL:
    log.warning("SMAIPL_API_URL is empty. /summary will fail until it is set.")
if not SMAIPL_BOT_ID:
    log.warning("SMAIPL_BOT_ID is empty. /summary may fail if SMAIPL requires auth.")

# ----------------------------
# 3) In-memory history + locks
# ----------------------------

# Для простоты: храним последние сообщения в памяти процесса.
# При рестарте Railway история сбросится. Для MVP это нормально.
chat_history: Dict[int, list] = defaultdict(list)
chat_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def _append_history(chat_id: int, user: str, text: str) -> None:
    chat_history[chat_id].append({"user": user, "text": text})
    if len(chat_history[chat_id]) > HISTORY_LIMIT:
        chat_history[chat_id] = chat_history[chat_id][-HISTORY_LIMIT:]

def _build_prompt(chat_id: int) -> str:
    items = chat_history.get(chat_id, [])
    items = items[-SUMMARY_LAST_N:] if SUMMARY_LAST_N > 0 else items

    lines = []
    for it in items:
        u = (it.get("user") or "user").strip()
        t = (it.get("text") or "").strip()
        if not t:
            continue
        lines.append(f"{u}: {t}")
    return "\n".join(lines).strip()

# ----------------------------
# 4) SMAIPL call (async, stable)
# ----------------------------

async def call_smaipl(payload: Dict[str, Any]) -> str:
    """
    Calls SMAIPL endpoint and returns summary text.
    On 400: raises RuntimeError with SMAIPL response text (for fast debugging).
    """
    if not SMAIPL_API_URL:
        raise RuntimeError("SMAIPL_API_URL is not configured")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Если SMAIPL использует Bearer — оставляем так.
    # Если у SMAIPL другой формат, поменяем только тут.
    if SMAIPL_BOT_ID:
        headers["Authorization"] = f"Bearer {SMAIPL_BOT_ID}"

    async with httpx.AsyncClient(timeout=SMAIPL_TIMEOUT, verify=SMAIPL_VERIFY_SSL) as client:
        r = await client.post(SMAIPL_API_URL, headers=headers, json=payload)

        # 400 — это “почини запрос”, поэтому текст обязателен для диагностики
        if r.status_code == 400:
            raise RuntimeError(f"SMAIPL 400: {r.text}")

        r.raise_for_status()

        # Попробуем извлечь “summary” из JSON.
        # Если формат другой — адаптируем.
        try:
            data = r.json()
        except Exception:
            return r.text.strip()

        # распространённые варианты
        for key in ("summary", "result", "text", "answer"):
            if isinstance(data, dict) and key in data and isinstance(data[key], str):
                return data[key].strip()

        # fallback
        return json.dumps(data, ensure_ascii=False)

# ----------------------------
# 5) Telegram handlers
# ----------------------------

async def on_startup(app):
    # ВАЖНО: убираем webhook и чистим хвост апдейтов
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    log.info("Bot started: @%s (id=%s). Webhook cleared.", me.username, me.id)

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    # Игнорим команду /summary в истории, чтобы не зашумлять prompt
    if text.startswith(SUMMARY_COMMAND):
        return

    user = update.effective_user.full_name if update.effective_user else "user"
    _append_history(update.effective_chat.id, user, text)

async def summary_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    # Anti-race: 1 summary at a time per chat
    async with chat_locks[chat_id]:
        await update.message.reply_text("Готовлю summary…")

        prompt = _build_prompt(chat_id)
        if not prompt:
            await update.message.reply_text("Пока нет истории сообщений для summary. Напишите несколько сообщений и повторите команду.")
            return

        payload = {
            # ВАЖНО: эти поля часто являются причиной SMAIPL 400.
            # Если SMAIPL требует другую схему, поменяем здесь.
            "bot_id": SMAIPL_BOT_ID or "unknown",
            "chat_id": str(chat_id),
            "message": prompt,
        }

        # Логируем безопасно: без токенов и без полного prompt
        log.info("Summary request: chat_id=%s, chars=%s, last_n=%s", chat_id, len(prompt), SUMMARY_LAST_N)

        try:
            summary_text = await call_smaipl(payload)
        except Exception as e:
            log.exception("Summary failed for chat_id=%s", chat_id)
            await update.message.reply_text(f"Ошибка при генерации summary: {e}")
            return

        if not summary_text:
            await update.message.reply_text("SMAIPL вернул пустой ответ.")
            return

        # Telegram message length safety (4096 max)
        if len(summary_text) > 3900:
            summary_text = summary_text[:3900] + "\n\n[сообщение обрезано]"

        await update.message.reply_text(summary_text)

# ----------------------------
# 6) Main
# ----------------------------

def main() -> None:
    # Команда может быть "/summary" — handler должен быть "summary"
    cmd = SUMMARY_COMMAND.lstrip("/") or "summary"

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler(cmd, summary_handler))
    # Собираем историю обычных сообщений
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Привет! Напиши несколько сообщений и используй /summary")))
    app.add_handler(CommandHandler("help", lambda u, c: u.message.reply_text("Команда: /summary — сделать сводку по последним сообщениям.")))
    app.add_handler(CommandHandler("ping", lambda u, c: u.message.reply_text("pong")))
    app.add_handler(CommandHandler("clear", lambda u, c: chat_history.pop(u.effective_chat.id, None) or u.message.reply_text("История очищена.")))

    # Ловим все текстовые сообщения (не команды)
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Polling: one instance only
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
