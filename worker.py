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
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "https://ai.smaipl.ru/v1").strip().rstrip("/")
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "test_chat_1").strip()

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty. Telegram bot will not start until it is set.")
if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is empty. Webhook cannot be set without it.")
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET is empty. Webhook path protection is disabled (NOT recommended).")
if not SMAIPL_API_KEY:
    log.warning("SMAIPL_API_KEY is empty. SMAIPL calls will fail until it is set.")

# ----------------------------
# SMAIPL Call (OpenAI-compatible)
# ----------------------------
SYSTEM_PROMPT = (
    "Ты — ассистент по суммаризации. "
    "Отвечай ТОЛЬКО суммаризацией на русском языке, без JSON, без служебных полей. "
    "Формат: 5–10 маркеров, затем блок 'Действия' (если есть). "
    "Не выдумывай факты."
)

async def call_smaipl(prompt: str) -> str:
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is not set")
    url = f"{SMAIPL_BASE_URL}/chat/completions"

    payload = {
        "model": SMAIPL_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    # 2 попытки на случай сетевых подвисаний
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()

            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            content = (content or "").strip()

            # Если модель продолжает возвращать JSON, попробуем вытащить из него текст,
            # иначе вернём как есть (чтобы было видно, что именно приходит).
            if content.startswith("{") and content.endswith("}"):
                # Не ломаемся: просто возвращаем как есть, но без лишней экранизации
                return content

            if not content:
                return json.dumps(data, ensure_ascii=False)

            return content

        except Exception as e:
            last_err = e
            log.warning(f"SMAIPL call attempt {attempt+1} failed: {e}")

    raise RuntimeError(f"SMAIPL call failed after retries: {last_err}")

# ----------------------------
# Telegram Handlers
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — отправляй *ответом (reply)* на сообщение, которое нужно суммаризировать.",
        parse_mode="Markdown",
    )

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text(
            "Команду /summary нужно отправлять *ответом* на сообщение для суммаризации.",
            parse_mode="Markdown",
        )
        return

    source_text = (msg.reply_to_message.text or "").strip()
    if not source_text:
        await msg.reply_text("Не вижу текста для суммаризации (reply-сообщение пустое).")
        return

    await msg.reply_text("Готовлю summary...")

    prompt = (
        "Суммаризируй текст кратко, структурировано, по пунктам. "
        "Выдели ключевые решения/действия (если есть).\n\n"
        f"ТЕКСТ:\n{source_text}"
    )

    try:
        result = await call_smaipl(prompt)
        await msg.reply_text(result)
    except Exception as e:
        log.exception("Summary generation failed")
        await msg.reply_text(f"Ошибка при генерации summary: {e}")

# ----------------------------
# FastAPI Webhook Server
# IMPORTANT: Fly.io expects an ASGI app called `app` in `worker.py`
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

@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    global tg_app

    if tg_app is None:
        raise HTTPException(status_code=503, detail="Telegram app is not initialised")

    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

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
