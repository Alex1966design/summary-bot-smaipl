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
# ENV (Telegram / Webhook)
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# ----------------------------
# ENV (SMAIPL OpenAI-compatible)
# ----------------------------
SMAIPL_BASE_URL = os.getenv("SMAIPL_BASE_URL", "https://ai.smaipl.ru/v1").strip().rstrip("/")
SMAIPL_API_KEY = os.getenv("SMAIPL_API_KEY", "").strip()
SMAIPL_MODEL = os.getenv("SMAIPL_MODEL", "gpt-4o-mini").strip()
SMAIPL_TEMPERATURE = float(os.getenv("SMAIPL_TEMPERATURE", "0.2").strip())
SMAIPL_TIMEOUT = float(os.getenv("SMAIPL_TIMEOUT", "60").strip())

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty. Telegram bot will not start until it is set.")
if not PUBLIC_BASE_URL:
    log.warning("PUBLIC_BASE_URL is empty. Webhook cannot be set without it.")
if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET is empty. Webhook path protection is disabled (NOT recommended).")
if not SMAIPL_API_KEY:
    log.warning("SMAIPL_API_KEY is empty. /summary will fail until it is set.")

# ----------------------------
# FastAPI (ASGI app for Fly.io)
# IMPORTANT: Fly expects `app` in module `worker.py` (uvicorn worker:app)
# ----------------------------
app = FastAPI()

# Telegram Application (PTB)
tg_app: Optional[Application] = None


def _build_tg_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("summary", summary_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    return application


# ----------------------------
# SMAIPL Call (OpenAI-compatible /v1/chat/completions)
# ----------------------------
async def call_smaipl_chat(prompt: str) -> str:
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is not set")
    if not SMAIPL_BASE_URL:
        raise RuntimeError("SMAIPL_BASE_URL is not set")
    if not SMAIPL_MODEL:
        raise RuntimeError("SMAIPL_MODEL is not set")

    url = f"{SMAIPL_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": SMAIPL_MODEL,
        "temperature": SMAIPL_TEMPERATURE,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — помощник, который делает краткие, структурированные summaries на русском языке. "
                    "Отвечай только итоговым summary, без JSON и без служебных полей."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }

    timeout = httpx.Timeout(SMAIPL_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, headers=headers, json
