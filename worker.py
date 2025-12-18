import os
import json
import time
import logging
from typing import Any, Dict, Optional, Callable, TypeVar, Tuple

import httpx
from fastapi import FastAPI, Request, Header, HTTPException

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


T = TypeVar("T")

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

# ============================================================
# ENV
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# SMAIPL (ask-style endpoint) — куда отправляем готовое summary
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()

# OpenAI-compatible SMAIPL — через него генерим summary
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "").strip().rstrip("/")
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "gpt-4o-mini").strip()

# Флаг отправки summary обратно в SMAIPL
SEND_TO_SMAIPL = os.getenv("SEND_TO_SMAIPL", "true").strip().lower() in ("1", "true", "yes", "y", "on")

# Retry настройки
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3").strip())
RETRY_BASE_SLEEP_SEC = float(os.getenv("RETRY_BASE_SLEEP_SEC", "0.8").strip())
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "60").strip())


# ============================================================
# Helpers
# ============================================================
def _require_env(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"{name} is not set")


def _mask(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * 6


def _bool_str(b: bool) -> str:
    return "true" if b else "false"


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = RETRY_MAX_ATTEMPTS,
    base_sleep: float = RETRY_BASE_SLEEP_SEC,
    retry_on: Tuple[type, ...] = (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, RuntimeError),
    name: str = "operation",
) -> T:
    last_exc: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as e:
            last_exc = e
            if i == attempts:
                break
            sleep_s = base_sleep * (2 ** (i - 1))
            log.warning("%s failed (attempt %s/%s): %s; sleeping %.2fs", name, i, attempts, e, sleep_s)
            time.sleep(sleep_s)
    raise last_exc  # type: ignore[misc]


def fallback_summary(text: str) -> str:
    """
    Fallback, если генерация через SMAIPL недоступна.
    Простая "аварийная" выжимка: первые 800–1200 символов + структура.
    """
    clean = (text or "").strip()
    if not clean:
        return "Пустой текст — нечего суммаризировать."

    snippet = clean[:1200]
    return (
        "Краткое резюме (fallback):\n"
        "1) Основная тема: (проверьте текст ниже)\n"
        "2) Ключевые пункты:\n"
        f"- {snippet.replace(chr(10), ' ')[:300]}…\n\n"
        "Исходный фрагмент (для контроля):\n"
        f"{snippet}"
    )


# ============================================================
# 1) Generate summary via SMAIPL (OpenAI-compatible API)
# ============================================================
def generate_summary(text: str) -> str:
    _require_env("SMAIPL_BASE_URL", SMAIPL_BASE_URL)
    _require_env("SMAIPL_API_KEY", SMAIPL_API_KEY)

    url = f"{SMAIPL_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": SMAIPL_MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Суммаризируй текст кратко и структурировано по пунктам. "
                    "Выдели ключевые выводы и действия.\n\n"
                    f"ТЕКСТ:\n{text}"
                ),
            }
        ],
    }

    def _do() -> str:
        with httpx.Client(timeout=HTTP_TIMEOUT_SEC) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        return data["choices"][0]["message"]["content"].strip()

    try:
        return retry_call(_do, name="generate_summary")
    except Exception as e:
        log.exception("generate_summary failed, using fallback: %s", e)
        return fallback_summary(text)


# ============================================================
# 2) Send summary back to SMAIPL (ask endpoint)
# ============================================================
def send_summary_to_smaipl(summary_text: str) -> str:
    """
    Формат строго как у SMAIPL:
    r.post(...).json()['done']
    """
    _require_env("SMAIPL_API_URL", SMAIPL_API_URL)
    _require_env("SMAIPL_BOT_ID", SMAIPL_BOT_ID)
    _require_env("SMAIPL_CHAT_ID", SMAIPL_CHAT_ID)

    payload = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": SMAIPL_CHAT_ID,
        "message": summary_text,
    }

    def _do() -> str:
        with httpx.Client(timeout=HTTP_TIMEOUT_SEC) as client:
            r = client.post(SMAIPL_API_URL, json=payload)
            r.raise_for_status()
            data = r.json()

        if isinstance(data, dict) and data.get("error") is True:
            raise RuntimeError(f"SMAIPL returned error=true; response={data}")

        return str(data.get("done", ""))

    return retry_call(_do, name="send_summary_to_smaipl")


# ============================================================
# Telegram handlers
# ============================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — ответь (reply) на сообщение, которое нужно суммаризировать."
    )


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message

    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text("Команду /summary нужно отправлять ответом на сообщение с текстом.")
        return

    source_text = msg.reply_to_message.text.strip()
    await msg.reply_text("Готовлю summary...")

    # Генерим summary (с retry + fallback внутри)
    summary = await context.application.run_in_executor(None, generate_summary, source_text)
    await msg.reply_text(summary)

    # Отправляем summary в SMAIPL (опционально)
    if not SEND_TO_SMAIPL:
        log.info("SEND_TO_SMAIPL=false; skipping send to SMAIPL")
        return

    try:
        done = await context.application.run_in_executor(None, send_summary_to_smaipl, summary)
        log.info("Summary sent to SMAIPL, done=%s", done)
    except Exception as e:
        # Fallback: не ломаем пользователю UX, просто логируем (и можно уведомить)
        log.exception("Failed to send summary to SMAIPL: %s", e)
        await msg.reply_text("Примечание: summary отправить в SMAIPL не удалось (см. логи).")


# ============================================================
# FastAPI
# ============================================================
app = FastAPI()
tg_app: Optional[Application] = None


def build_tg_app() -> Application:
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
    Для проверки, что Fly подхватил секреты.
    Секреты маскируются, значения не раскрываем.
    """
    return {
        "status": "ok",
        "send_to_smaipl": SEND_TO_SMAIPL,
        "retry": {
            "max_attempts": RETRY_MAX_ATTEMPTS,
            "base_sleep_sec": RETRY_BASE_SLEEP_SEC,
            "http_timeout_sec": HTTP_TIMEOUT_SEC,
        },
        "env_present": {
            "BOT_TOKEN": bool(BOT_TOKEN),
            "PUBLIC_BASE_URL": bool(PUBLIC_BASE_URL),
            "WEBHOOK_SECRET": bool(WEBHOOK_SECRET),
            "SMAIPL_BASE_URL": bool(SMAIPL_BASE_URL),
            "SMAIPL_API_KEY": bool(SMAIPL_API_KEY),
            "SMAIPL_MODEL": bool(SMAIPL_MODEL),
            "SMAIPL_API_URL": bool(SMAIPL_API_URL),
            "SMAIPL_BOT_ID": bool(SMAIPL_BOT_ID),
            "SMAIPL_CHAT_ID": bool(SMAIPL_CHAT_ID),
        },
        "values_masked": {
            "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
            "WEBHOOK_SECRET": _mask(WEBHOOK_SECRET),
            "SMAIPL_BASE_URL": SMAIPL_BASE_URL,
            "SMAIPL_API_KEY": _mask(SMAIPL_API_KEY),
            "SMAIPL_MODEL": SMAIPL_MODEL,
            "SMAIPL_API_URL": SMAIPL_API_URL,
            "SMAIPL_BOT_ID": _mask(SMAIPL_BOT_ID),
            "SMAIPL_CHAT_ID": _mask(SMAIPL_CHAT_ID),
        },
    }


@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Доп. проверка заголовка (если Telegram присылает)
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token is not None:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token header")

    if tg_app is None:
        raise HTTPException(status_code=503, detail="Telegram app not initialised")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


# ============================================================
# Startup / Shutdown
# ============================================================
@app.on_event("startup")
async def on_startup() -> None:
    global tg_app

    _require_env("BOT_TOKEN", BOT_TOKEN)
    _require_env("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    _require_env("WEBHOOK_SECRET", WEBHOOK_SECRET)

    tg_app = build_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    webhook_path = f"/webhook/{WEBHOOK_SECRET}"
    webhook_url = f"{PUBLIC_BASE_URL}{webhook_path}"

    await tg_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )

    log.info("Webhook set to: %s", webhook_url)
    log.info("SEND_TO_SMAIPL=%s", _bool_str(SEND_TO_SMAIPL))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global tg_app
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
