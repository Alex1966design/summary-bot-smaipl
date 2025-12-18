# worker.py
import os
import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Header, HTTPException

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# SMAIPL (OpenAI-compatible)
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "").strip().rstrip("/")  # e.g. https://ai.smaipl.ru/v1
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()               # bearer token
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "").strip()                   # e.g. gpt-4.1o / gpt-4o-mini

# Optional: push summary to SMAIPL/anywhere (if you later need it)
SMAIPL_PUSH_URL = os.getenv("SMAIPL_PUSH_URL", "").strip()             # optional endpoint to receive produced summary
SMAIPL_PUSH_BEARER = os.getenv("SMAIPL_PUSH_BEARER", "").strip()        # optional bearer for push

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty. Telegram bot will not start until it is set.")
if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is empty. Webhook cannot be set without it.")
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET is empty. URL protection is disabled (NOT recommended).")

if not SMAIPL_BASE_URL:
    log.warning("SMAIPL_BASE_URL is empty. SMAIPL calls will fail until it is set.")
if not SMAIPL_API_KEY:
    log.warning("SMAIPL_API_KEY is empty. SMAIPL calls will fail until it is set.")
if not SMAIPL_MODEL:
    log.warning("SMAIPL_MODEL is empty. SMAIPL calls will fail until it is set.")

# ----------------------------
# Helpers
# ----------------------------
def _mask(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return f"{s[:3]}***{s[-(keep-3):]}"

async def smaipl_chat_complete(user_text: str) -> str:
    """
    Calls SMAIPL OpenAI-compatible endpoint: {SMAIPL_BASE_URL}/chat/completions
    Returns assistant message content as string.
    """
    if not SMAIPL_BASE_URL:
        raise RuntimeError("SMAIPL_BASE_URL is not set (expected e.g. https://ai.smaipl.ru/v1)")
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is not set")
    if not SMAIPL_MODEL:
        raise RuntimeError("SMAIPL_MODEL is not set (e.g. gpt-4.1o)")

    url = f"{SMAIPL_BASE_URL}/chat/completions"

    system_prompt = (
        "Ты — ассистент-аналитик по проектным коммуникациям. "
        "Сделай краткое, чёткое и структурированное резюме текста.\n\n"
        "Формат ответа:\n"
        "1) Кратко (1–2 предложения)\n"
        "2) Ключевые пункты (буллеты)\n"
        "3) Решения/действия (если есть)\n"
        "4) Риски/вопросы (если есть)\n\n"
        "Отвечай на русском. Не возвращай JSON, если я не прошу JSON."
    )

    payload = {
        "model": SMAIPL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"Unexpected SMAIPL response format: {json.dumps(data, ensure_ascii=False)}")

    return (content or "").strip()

async def push_summary_somewhere(summary_text: str, meta: Dict[str, Any]) -> None:
    """
    Optional: push produced summary to SMAIPL/another endpoint if you set SMAIPL_PUSH_URL.
    This is how you can make “SMAIPL receive summary via API”.
    """
    if not SMAIPL_PUSH_URL:
        return

    headers = {}
    if SMAIPL_PUSH_BEARER:
        headers["Authorization"] = f"Bearer {SMAIPL_PUSH_BEARER}"

    payload = {
        "summary": summary_text,
        "meta": meta,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(SMAIPL_PUSH_URL, headers=headers, json=payload)
        r.raise_for_status()

# ----------------------------
# Telegram handlers
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — отправляй *ответом (reply)* на сообщение, которое нужно суммаризировать.",
        parse_mode="Markdown",
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    # /summary must be a reply to a message with text
    if not msg.reply_to_message or not (msg.reply_to_message.text or msg.reply_to_message.caption):
        await msg.reply_text(
            "Команду /summary нужно отправлять *ответом* на сообщение для суммаризации.",
            parse_mode="Markdown",
        )
        return

    source_text = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
    if not source_text:
        await msg.reply_text("Не вижу текста для суммаризации (reply-сообщение пустое).")
        return

    await msg.reply_text("Готовлю summary...")

    try:
        prompt = f"ТЕКСТ:\n{source_text}"
        result = await smaipl_chat_complete(prompt)

        # Send back to Telegram
        await msg.reply_text(result)

        # Optional: push to external/SMAIPL endpoint (if you configure it)
        await push_summary_somewhere(
            result,
            meta={
                "telegram_chat_id": update.effective_chat.id if update.effective_chat else None,
                "telegram_message_id": msg.message_id,
            },
        )

    except Exception as e:
        log.exception("Summary generation failed")
        await msg.reply_text(f"Ошибка при генерации summary: {e}")

# ----------------------------
# FastAPI (ASGI) app
# Fly runs: uvicorn worker:app --host 0.0.0.0 --port ${PORT}
# ----------------------------
app = FastAPI()

tg_app: Optional[Application] = None

def _build_tg_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("summary", summary_cmd))
    return application

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}

@app.get("/debug/config")
async def debug_config() -> Dict[str, Any]:
    """
    Safe config visibility for debugging.
    Does NOT expose full secrets.
    """
    return {
        "BOT_TOKEN_set": bool(BOT_TOKEN),
        "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "WEBHOOK_SECRET_set": bool(WEBHOOK_SECRET),
        "WEBHOOK_SECRET_masked": _mask(WEBHOOK_SECRET, keep=8),

        "SMAIPL_BASE_URL": SMAIPL_BASE_URL,
        "SMAIPL_API_KEY_set": bool(SMAIPL_API_KEY),
        "SMAIPL_API_KEY_masked": _mask(SMAIPL_API_KEY, keep=10),
        "SMAIPL_MODEL": SMAIPL_MODEL,

        "SMAIPL_PUSH_URL": SMAIPL_PUSH_URL,
        "SMAIPL_PUSH_BEARER_set": bool(SMAIPL_PUSH_BEARER),
    }

@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    global tg_app

    if tg_app is None:
        raise HTTPException(status_code=503, detail="Telegram app is not initialised")

    # 1) URL secret check
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # 2) Header secret check (Telegram supports it)
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token is not None:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token header")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)

    await tg_app.process_update(update)
    return {"ok": True}

# ----------------------------
# Startup / Shutdown
# ----------------------------
@app.on_event("startup")
async def on_startup() -> None:
    global tg_app

    if not BOT_TOKEN:
        log.error("BOT_TOKEN is empty: Telegram app will not start.")
        return

    tg_app = _build_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    if not PUBLIC_BASE_URL:
        log.warning("Skipping setWebhook: PUBLIC_BASE_URL missing")
        return

    webhook_path = f"/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/webhook/no-secret"
    webhook_url = f"{PUBLIC_BASE_URL}{webhook_path}"

    try:
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=(WEBHOOK_SECRET if WEBHOOK_SECRET else None),
            drop_pending_updates=True,
        )
        log.info(f"Webhook set to: {webhook_url}")
    except Exception:
        log.exception("Failed to set webhook")

@app.on_event("shutdown")
async def on_shutdown() -> None:
    global tg_app
    if tg_app is None:
        return
    try:
        await tg_app.stop()
        await tg_app.shutdown()
    except Exception:
        log.exception("Shutdown error")

# ----------------------------
# Local run (optional)
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
