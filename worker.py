import os
import json
import logging
from typing import Any, Dict, Optional, List

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from starlette.concurrency import run_in_threadpool

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
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "https://ai.smaipl.ru/v1").strip().rstrip("/")
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()  # Bearer token (43891_...)
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "test_chat_1").strip()

# Optional: push summary INTO SMAIPL chat (needs correct SMAIPL endpoint from them)
SMAIPL_PUSH_URL = os.getenv("SMAIPL_PUSH_URL", "").strip()
SMAIPL_PUSH_AUTH = os.getenv("SMAIPL_PUSH_AUTH", "").strip()  # optional auth for push

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty. Telegram bot will not start until it is set.")
if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is empty. Webhook cannot be set without it.")
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET is empty. Webhook path protection is disabled (NOT recommended).")
if not SMAIPL_API_KEY:
    log.warning("SMAIPL_API_KEY is empty. SMAIPL generation will not work until it is set.")

# ----------------------------
# SMAIPL: OpenAI-compatible call
# ----------------------------
def call_smaipl_chat_completion(prompt: str) -> str:
    """
    Synchronous call to SMAIPL OpenAI-compatible endpoint:
      POST {SMAIPL_BASE_URL}/chat/completions
      Authorization: Bearer {SMAIPL_API_KEY}
    """
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is not set")
    if not SMAIPL_MODEL:
        raise RuntimeError("SMAIPL_MODEL is not set")

    url = f"{SMAIPL_BASE_URL}/chat/completions"
    payload = {
        "model": SMAIPL_MODEL,
        "messages": [
            {"role": "system", "content": "Ты помощник, который делает краткие, структурированные summary."},
            {"role": "user", "content": prompt},
        ],
        # при желании можно добавить temperature/max_tokens, но оставим дефолты SMAIPL
    }

    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=90.0) as client:
        r = client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    # Ожидаем OpenAI-like структуру
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        return json.dumps(data, ensure_ascii=False)

# ----------------------------
# OPTIONAL: push summary into SMAIPL chat/widget
# ----------------------------
def push_summary_to_smaipl(summary_text: str, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    This requires SMAIPL_PUSH_URL from SMAIPL side.
    We cannot guess it reliably: ask SMAIPL support for endpoint to append a message into widget dialog.

    Expected behavior: POST to SMAIPL_PUSH_URL with JSON payload containing summary and optional meta.
    """
    if not SMAIPL_PUSH_URL:
        # Not configured -> do nothing
        return

    payload: Dict[str, Any] = {
        "message": summary_text,
        "meta": meta or {},
    }

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if SMAIPL_PUSH_AUTH:
        headers["Authorization"] = f"Bearer {SMAIPL_PUSH_AUTH}"

    with httpx.Client(timeout=30.0) as client:
        r = client.post(SMAIPL_PUSH_URL, headers=headers, json=payload)
        r.raise_for_status()

# ----------------------------
# Telegram Handlers
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — отправляй ответом (reply) на сообщение, которое нужно суммаризировать."
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    # /summary must be a reply
    if not msg.reply_to_message:
        await msg.reply_text("Команду /summary нужно отправлять ответом (reply) на сообщение для суммаризации.")
        return

    # Text can be in text OR caption (if replied message is media)
    source_text = ""
    if msg.reply_to_message.text:
        source_text = msg.reply_to_message.text.strip()
    elif msg.reply_to_message.caption:
        source_text = msg.reply_to_message.caption.strip()

    if not source_text:
        await msg.reply_text("Не вижу текста для суммаризации (reply-сообщение пустое).")
        return

    await msg.reply_text("Готовлю summary...")

    prompt = (
        "Суммаризируй текст кратко и структурировано.\n"
        "Требования:\n"
        "- 5–10 пунктов\n"
        "- отдельный блок: 'Ключевые решения/действия' (если есть)\n"
        "- без воды\n\n"
        f"ТЕКСТ:\n{source_text}"
    )

    try:
        result = await run_in_threadpool(call_smaipl_chat_completion, prompt)

        # Optional: push to SMAIPL chat (if configured)
        meta = {
            "source": "telegram",
            "chat_id": update.effective_chat.id if update.effective_chat else None,
            "message_id": msg.message_id,
        }
        await run_in_threadpool(push_summary_to_smaipl, result, meta)

        await msg.reply_text(result)
    except Exception as e:
        log.exception("Summary generation failed")
        await msg.reply_text(f"Ошибка при генерации summary: {e}")

# ----------------------------
# FastAPI Webhook Server
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
    # Безопасно: не светим значения токенов
    return {
        "public_base_url_set": bool(PUBLIC_BASE_URL),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "bot_token_set": bool(BOT_TOKEN),
        "smaipl_base_url": SMAIPL_BASE_URL,
        "smaipl_api_key_set": bool(SMAIPL_API_KEY),
        "smaipl_model": SMAIPL_MODEL,
        "smaipl_push_url_set": bool(SMAIPL_PUSH_URL),
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

    # Secret in URL
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # Optional header secret (Telegram supports it)
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token is not None:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token header")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
