import os
import re
import json
import asyncio
import logging
from typing import Any, Dict, List
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
# 0) LOGGING (entrypoint: must be first)
# ==========================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class RedactSecretsFilter(logging.Filter):
    """
    Redacts Telegram bot tokens and URLs if they appear in log messages.
    This is a safety net. Primary protection is disabling httpx/httpcore noisy logs.
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
logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Critical: avoid printing request URLs (they include bot token)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Telegram libs can be chatty on DEBUG; keep INFO
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

log = logging.getLogger("summary_bot")
log.addFilter(RedactSecretsFilter())

# ==========================================================
# 1) CONFIG (all via env; no further code edits needed)
# ==========================================================

APP_VERSION = os.getenv("APP_VERSION", "stable-1").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary").strip()  # "/summary"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))             # store up to N messages per chat
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))            # include last N into summary request

# SMAIPL settings (optional; command still works without SMAIPL via fallback)
SMAIPL_ENABLED = os.getenv("SMAIPL_ENABLED", "true").lower() in ("1", "true", "yes")
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_METHOD = os.getenv("SMAIPL_METHOD", "POST").upper().strip()  # POST/GET
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "30"))
SMAIPL_VERIFY_SSL = os.getenv("SMAIPL_VERIFY_SSL", "true").lower() in ("1", "true", "yes")

# Auth: supports Bearer, X-API-Key, or none
SMAIPL_AUTH_TYPE = os.getenv("SMAIPL_AUTH_TYPE", "bearer").lower().strip()  # bearer|x-api-key|none
SMAIPL_AUTH_VALUE = os.getenv("SMAIPL_AUTH_VALUE", os.getenv("SMAIPL_BOT_ID", "")).strip()

# Payload template is configurable to avoid “we must change code to match SMAIPL contract”
# Default matches what you used earlier: bot_id/chat_id/message
DEFAULT_PAYLOAD_TEMPLATE = {
    "bot_id": "{auth_value}",
    "chat_id": "{chat_id}",
    "message": "{text}",
}
SMAIPL_PAYLOAD_TEMPLATE_JSON = os.getenv("SMAIPL_PAYLOAD_TEMPLATE_JSON", "").strip()

# Response field preference
SMAIPL_RESPONSE_FIELD = os.getenv("SMAIPL_RESPONSE_FIELD", "summary").strip()  # summary/result/text/answer/output


def _load_payload_template() -> Dict[str, Any]:
    if not SMAIPL_PAYLOAD_TEMPLATE_JSON:
        return DEFAULT_PAYLOAD_TEMPLATE
    try:
        tpl = json.loads(SMAIPL_PAYLOAD_TEMPLATE_JSON)
        if not isinstance(tpl, dict):
            raise ValueError("template is not a JSON object")
        return tpl
    except Exception as e:
        log.warning("Invalid SMAIPL_PAYLOAD_TEMPLATE_JSON, using default. Error: %s", e)
        return DEFAULT_PAYLOAD_TEMPLATE


SMAIPL_PAYLOAD_TEMPLATE = _load_payload_template()

# ==========================================================
# 2) STATE (history + per-chat locks)
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


def fallback_summary(text: str) -> str:
    """
    Guaranteed response even if SMAIPL is down/misconfigured.
    This is intentionally simple and robust.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "Нет данных для summary."
    # Take last 10 lines and format as bullets
    last = lines[-10:]
    out = ["Summary (fallback):", ""]
    out += [f"• {ln[:300]}" for ln in last]
    return "\n".join(out)


def render_template(obj: Any, vars_map: Dict[str, str]) -> Any:
    """
    Recursively substitutes placeholders in strings:
    {chat_id}, {text}, {auth_value}
    """
    if isinstance(obj, dict):
        return {k: render_template(v, vars_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_template(v, vars_map) for v in obj]
    if isinstance(obj, str):
        try:
            return obj.format(**vars_map)
        except Exception:
            return obj
    return obj

# ==========================================================
# 3) SMAIPL client
# ==========================================================

async def call_smaipl(chat_id: int, context_text: str) -> str:
    if not SMAIPL_ENABLED:
        raise RuntimeError("SMAIPL is disabled")
    if not SMAIPL_API_URL:
        raise RuntimeError("SMAIPL_API_URL is not configured")

    headers = {"Accept": "application/json"}

    if SMAIPL_AUTH_TYPE == "bearer" and SMAIPL_AUTH_VALUE:
        headers["Authorization"] = f"Bearer {SMAIPL_AUTH_VALUE}"
    elif SMAIPL_AUTH_TYPE == "x-api-key" and SMAIPL_AUTH_VALUE:
        headers["X-API-Key"] = SMAIPL_AUTH_VALUE
    # else: none

    vars_map = {
        "chat_id": str(chat_id),
        "text": context_text,
        "auth_value": SMAIPL_AUTH_VALUE or "",
    }
    payload = render_template(SMAIPL_PAYLOAD_TEMPLATE, vars_map)

    # Safe log (no payload text)
    log.info("SMAIPL call: method=%s chat_id=%s chars=%s verify_ssl=%s",
             SMAIPL_METHOD, chat_id, len(context_text), SMAIPL_VERIFY_SSL)

    async with httpx.AsyncClient(timeout=SMAIPL_TIMEOUT, verify=SMAIPL_VERIFY_SSL) as client:
        if SMAIPL_METHOD == "GET":
            r = await client.get(SMAIPL_API_URL, headers=headers, params=payload)
        else:
            headers.setdefault("Content-Type", "application/json")
            r = await client.post(SMAIPL_API_URL, headers=headers, json=payload)

    if r.status_code == 400:
        # This is the single most useful diagnostic line
        raise RuntimeError(f"SMAIPL 400: {r.text}")

    r.raise_for_status()

    # Parse response
    try:
        data = r.json()
    except Exception:
        return r.text.strip()

    if isinstance(data, dict):
        # Prefer explicit field from env
        v = data.get(SMAIPL_RESPONSE_FIELD)
        if isinstance(v, str) and v.strip():
            return v.strip()
        # Common fallbacks
        for key in ("summary", "result", "text", "answer", "output", "message"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    return json.dumps(data, ensure_ascii=False)

# ==========================================================
# 4) Telegram handlers
# ==========================================================

async def on_startup(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    log.info("App version: %s", APP_VERSION)
    log.info("Bot started: @%s (id=%s). Webhook cleared.", me.username, me.id)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен.\n"
        "Напиши несколько сообщений, затем вызови /summary.\n"
        "Команды: /summary /clear /ping"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/summary — сводка по последним сообщениям\n"
        "/clear — очистить историю\n"
        "/ping — проверка"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        chat_history.pop(update.effective_chat.id, None)
    await update.message.reply_text("История очищена.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        return
    user = update.effective_user.full_name if update.effective_user else "user"
    add_history(update.effective_chat.id, user, text)

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    async with chat_locks[chat_id]:
        await update.message.reply_text("Готовлю summary…")

        ctx = build_context(chat_id)
        if not ctx:
            await update.message.reply_text("Недостаточно истории. Напиши несколько сообщений и повтори /summary.")
            return

        # Try SMAIPL; if it fails, return fallback summary (so command always works)
        try:
            result = await call_smaipl(chat_id, ctx)
            if not result.strip():
                raise RuntimeError("SMAIPL returned empty response")
            prefix = "Summary (SMAIPL):\n\n"
            out = prefix + result.strip()
        except Exception as e:
            log.exception("SMAIPL failed; using fallback. chat_id=%s", chat_id)
            out = fallback_summary(ctx) + f"\n\n[SMAIPL error: {e}]"

        # Telegram length safety
        if len(out) > 3900:
            out = out[:3900] + "\n\n[сообщение обрезано]"

        await update.message.reply_text(out)

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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler(cmd, cmd_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Polling: Railway must be scale=1
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
