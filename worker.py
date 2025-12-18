import os
import json
import time
import hmac
import hashlib
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ============================
# Logging
# ============================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("summary_bot")

APP_VERSION = os.getenv("APP_VERSION", "v16")  # можно менять для проверки, что релиз обновился


# ============================
# ENV (Telegram / Webhook)
# ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# Флаг: отправлять summary обратно в SMAIPL (push) или нет
SEND_TO_SMAIPL = os.getenv("SEND_TO_SMAIPL", "false").strip().lower() in ("1", "true", "yes", "y", "on")

# ============================
# ENV (SMAIPL LLM – chat completions)
# ============================
# У тебя это работает по Bearer token:
#   POST https://ai.smaipl.ru/v1/chat/completions
#   Authorization: Bearer <SMAIPL_API_KEY>
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "https://ai.smaipl.ru/v1").strip().rstrip("/")
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()  # Bearer token
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "gpt-4o-mini").strip()

# ============================
# ENV (SMAIPL legacy ask – optional push)
# ============================
# Это тот endpoint, который у тебя возвращал {"error": true}.
# Оставляем опционально, чтобы была функция send_summary_to_smaipl()
SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()  # полный URL на /api/v1.0/ask/<token>
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()    # например 5129
SMAIPL_CHAT_ID = os.getenv("SMAIPL_CHAT_ID", "").strip()  # например ask123456
SMAIPL_RESPONSE_FIELD = os.getenv("SMAIPL_RESPONSE_FIELD", "done").strip()

# ============================
# ENV (Auth for /api/summary)
# ============================
# Чтобы SMAIPL мог безопасно дергать наш API, используем тот же WEBHOOK_SECRET:
# SMAIPL будет отправлять заголовок: X-Api-Key: <WEBHOOK_SECRET>
API_KEY_HEADER = os.getenv("API_KEY_HEADER", "X-Api-Key").strip()


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


# ============================
# FastAPI app (Fly expects `app`)
# ============================
app = FastAPI()

# Telegram PTB app
tg_app: Optional[Application] = None


# ============================
# Models for API
# ============================
class SummaryRequest(BaseModel):
    text: str
    prompt: Optional[str] = None  # можно передать свой system/prompt
    language: Optional[str] = "ru"


class SummaryResponse(BaseModel):
    ok: bool
    summary: str
    model: str
    provider: str = "smaipl_chat_completions"
    version: str = APP_VERSION


# ============================
# SMAIPL: chat completions (primary)
# ============================
async def smaipl_chat_completion(user_text: str, system_prompt: Optional[str] = None) -> str:
    """
    Основной рабочий путь (у тебя он уже отвечает):
    POST {SMAIPL_BASE_URL}/chat/completions
    Authorization: Bearer {SMAIPL_API_KEY}
    """
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is not set")

    url = f"{SMAIPL_BASE_URL}/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})

    payload = {"model": SMAIPL_MODEL, "messages": messages}

    headers = {"Authorization": f"Bearer {SMAIPL_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    # ожидаем стандартный формат choices[0].message.content
    try:
        return str(data["choices"][0]["message"]["content"])
    except Exception:
        return json.dumps(data, ensure_ascii=False)


def naive_fallback_summary(text: str) -> str:
    """
    Фолбэк если SMAIPL временно недоступен: очень простой "summary".
    """
    text = (text or "").strip()
    if not text:
        return "Пустой текст — нечего суммаризировать."

    # обрежем, разобьём на предложения
    cut = text[:2000]
    # грубая эвристика: первые 3-5 "предложений"
    parts = [p.strip() for p in cut.replace("\n", " ").split(".") if p.strip()]
    head = parts[:5]
    bullets = "\n".join([f"- {p}." for p in head]) if head else cut
    return "Фолбэк-резюме (LLM недоступна):\n" + bullets


async def generate_summary_with_retry(source_text: str, *, language: str = "ru") -> str:
    """
    Retry + fallback:
    1) 3 попытки вызвать SMAIPL chat completions
    2) если не вышло — naive_fallback_summary()
    """
    system_prompt = (
        "Ты — аналитик по проектным коммуникациям. "
        "Сделай краткое, структурированное резюме по пунктам. "
        "Выдели ключевые решения/действия (если есть). "
        f"Язык ответа: {language}."
    )

    user_text = f"ТЕКСТ:\n{source_text}"

    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            return await smaipl_chat_completion(user_text=user_text, system_prompt=system_prompt)
        except Exception as e:
            last_err = e
            wait = 1.5 * attempt
            log.warning(f"SMAIPL attempt {attempt}/3 failed: {e}. Retrying in {wait:.1f}s")
            await _sleep(wait)

    log.error(f"SMAIPL failed after retries: {last_err}")
    return naive_fallback_summary(source_text)


async def _sleep(seconds: float) -> None:
    # отдельная функция, чтобы было проще мокать/отлаживать
    await __import__("asyncio").sleep(seconds)


# ============================
# SMAIPL legacy push (optional)
# ============================
async def send_summary_to_smaipl(summary_text: str) -> Dict[str, Any]:
    """
    Best-effort PUSH результата в SMAIPL через legacy endpoint:
      POST SMAIPL_API_URL  json={"bot_id": <int>, "chat_id": "...", "message": "..."}
    Возвращает dict с результатом.

    ВАЖНО: у тебя этот endpoint ранее отдавал {"error": true}.
    Поэтому:
      - функция не ломает основной поток
      - ошибки логируются
    """
    if not SMAIPL_API_URL:
        return {"ok": False, "reason": "SMAIPL_API_URL not set"}

    if not SMAIPL_BOT_ID or not SMAIPL_CHAT_ID:
        return {"ok": False, "reason": "SMAIPL_BOT_ID/SMAIPL_CHAT_ID not set"}

    try:
        bot_id_int = int(SMAIPL_BOT_ID)
    except ValueError:
        return {"ok": False, "reason": "SMAIPL_BOT_ID must be integer"}

    payload = {"bot_id": bot_id_int, "chat_id": SMAIPL_CHAT_ID, "message": summary_text}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(SMAIPL_API_URL, json=payload)
            r.raise_for_status()
            data = r.json()

        if isinstance(data, dict) and data.get("error") is True:
            return {"ok": False, "reason": "SMAIPL returned error=true", "response": data}

        # если вернули ожидаемое поле
        if isinstance(data, dict) and SMAIPL_RESPONSE_FIELD in data:
            return {"ok": True, "response": data, "value": data.get(SMAIPL_RESPONSE_FIELD)}

        return {"ok": True, "response": data}
    except Exception as e:
        log.exception("send_summary_to_smaipl failed")
        return {"ok": False, "reason": str(e)}


# ============================
# Telegram handlers
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! Я Summary Bot.\n\n"
        "Команда: /summary — отправляй ответом (reply) на сообщение, которое нужно суммаризировать."
    )


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if not msg.reply_to_message or not msg.reply_to_message.text:
        await msg.reply_text("Команду /summary нужно отправлять ответом (reply) на сообщение для суммаризации.")
        return

    source_text = (msg.reply_to_message.text or "").strip()
    if not source_text:
        await msg.reply_text("Не вижу текста для суммаризации (reply-сообщение пустое).")
        return

    await msg.reply_text("Готовлю summary...")

    summary = await generate_summary_with_retry(source_text, language="ru")
    await msg.reply_text(summary)

    # опциональный push в SMAIPL legacy /ask
    if SEND_TO_SMAIPL:
        push_res = await send_summary_to_smaipl(summary)
        log.info(f"SEND_TO_SMAIPL result: {push_res}")


def _build_tg_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("summary", summary_cmd))
    return application


# ============================
# Health + Debug
# ============================
@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "version": APP_VERSION}


@app.get("/debug/config")
async def debug_config(
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
) -> Dict[str, Any]:
    # Защитим endpoint тем же WEBHOOK_SECRET (или отдельным ключом)
    if WEBHOOK_SECRET:
        if x_api_key != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    return {
        "version": APP_VERSION,
        "public_base_url": PUBLIC_BASE_URL,
        "send_to_smaipl": SEND_TO_SMAIPL,
        "telegram": {
            "bot_token_set": bool(BOT_TOKEN),
            "webhook_secret_set": bool(WEBHOOK_SECRET),
        },
        "smaipl": {
            "base_url": SMAIPL_BASE_URL,
            "model": SMAIPL_MODEL,
            "api_key_set": bool(SMAIPL_API_KEY),
            "legacy_ask": {
                "smaipl_api_url_set": bool(SMAIPL_API_URL),
                "smaipl_bot_id_set": bool(SMAIPL_BOT_ID),
                "smaipl_chat_id_set": bool(SMAIPL_CHAT_ID),
            },
        },
    }


# ============================
# API endpoint for SMAIPL -> PilotBot (pull summary)
# ============================
@app.post("/api/summary", response_model=SummaryResponse)
async def api_summary(
    body: SummaryRequest,
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
) -> SummaryResponse:
    """
    Это и есть правильный интеграционный endpoint:
    SMAIPL/плагин вызывает наш API и получает summary.

    Защита:
      - если WEBHOOK_SECRET задан, то нужен заголовок X-Api-Key: <WEBHOOK_SECRET>
    """
    if WEBHOOK_SECRET:
        if x_api_key != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    # пользовательский prompt (если нужен)
    summary = await generate_summary_with_retry(text, language=body.language or "ru")
    return SummaryResponse(ok=True, summary=summary, model=SMAIPL_MODEL)


# ============================
# Telegram webhook endpoint
# ============================
@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    global tg_app

    if tg_app is None:
        raise HTTPException(status_code=503, detail="Telegram app is not initialised")

    # 1) секрет в URL
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    # 2) секрет в заголовке Telegram
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token is not None:
        if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token header")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


# ============================
# Startup / Shutdown
# ============================
@app.on_event("startup")
async def on_startup() -> None:
    global tg_app

    if not BOT_TOKEN:
        log.error("BOT_TOKEN is empty: Telegram app will not start.")
        return

    tg_app = _build_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    # set webhook
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


# ============================
# Local entrypoint (optional)
# ============================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
