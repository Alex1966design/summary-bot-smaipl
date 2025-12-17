import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

import requests
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("summary_bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

SMAIPL_API_URL = os.getenv("SMAIPL_API_URL", "").strip()
SMAIPL_BOT_ID = os.getenv("SMAIPL_BOT_ID", "").strip()
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "60"))
SMAIPL_VERIFY_SSL = os.getenv("SMAIPL_VERIFY_SSL", "true").lower() == "true"

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))
SUMMARY_LAST_N = int(os.getenv("SUMMARY_LAST_N", "50"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
if not PUBLIC_BASE_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_BASE_URL must start with https://")

application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()


def call_smaipl(message: str, chat_id: str) -> Dict[str, Any]:
    """
    SMAIPL ожидает POST на URL вида:
    https://api.smaipl.ru/api/v1.0/ask/<ASK_TOKEN>
    payload: {"bot_id": <int>, "chat_id": "<str>", "message": "<str>"}
    """
    if not SMAIPL_API_URL or not SMAIPL_BOT_ID:
        return {"error": True, "detail": "SMAIPL_API_URL or SMAIPL_BOT_ID not set"}

    payload = {
        "bot_id": int(SMAIPL_BOT_ID),
        "chat_id": str(chat_id),
        "message": message,
    }

    try:
        r = requests.post(
            SMAIPL_API_URL,
            json=payload,
            timeout=SMAIPL_TIMEOUT,
            verify=SMAIPL_VERIFY_SSL,
        )
        # На всякий случай логируем статус
        log.info("SMAIPL status=%s", r.status_code)

        # SMAIPL может вернуть не-JSON при ошибке
        try:
            data = r.json()
        except Exception:
            return {"error": True, "http_status": r.status_code, "raw": r.text[:500]}

        # Если HTTP не 200 — тоже считаем ошибкой
        if r.status_code != 200:
            data.setdefault("error", True)
            data["http_status"] = r.status_code
        return data

    except Exception as e:
        return {"error": True, "detail": f"Exception calling SMAIPL: {e}"}


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот онлайн. Команда: /summary")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("Готовлю summary...")

    # MVP: отправляем в SMAIPL сам текст команды или пояснение
    # (дальше подключим реальную историю чата)
    prompt = f"Сделай краткое резюме последних сообщений чата (MVP). Chat_id={chat_id}"

    resp = await context.application.run_in_threadpool(call_smaipl, prompt, chat_id)

    # Пробуем вытащить полезный текст из ответа
    if isinstance(resp, dict) and not resp.get("error"):
        # Часто провайдеры кладут ответ в done/answer/result — покажем максимально безопасно
        text = resp.get("done") or resp.get("answer") or resp.get("result") or json.dumps(resp, ensure_ascii=False)
        await update.message.reply_text(str(text)[:3500])
    else:
        await update.message.reply_text(f"Ответ SMAIPL: {json.dumps(resp, ensure_ascii=False)[:3500]}")


application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("summary", summary_cmd))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализируем PTB и ставим webhook
    await application.initialize()
    await application.start()

    webhook_url = f"{PUBLIC_BASE_URL}/webhook"
    await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)

    log.info("Webhook set to: %s", webhook_url)
    yield

    # Корректное выключение
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.stop()
    await application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}
