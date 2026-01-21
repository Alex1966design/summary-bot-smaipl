import os
import json
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# OpenAI (new SDK)
try:
    from openai import OpenAI
except Exception as e:
    OpenAI = None  # type: ignore


# ===============================
# ENV / CONFIG
# ===============================
load_dotenv()

def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def _env_int(name: str, default: int) -> int:
    v = _env(name, str(default))
    try:
        return int(v)
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    v = _env(name, str(default))
    try:
        return float(v)
    except Exception:
        return default


# ---- OpenAI ----
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4o-mini")

SUMMARY_LANGUAGE = _env("SUMMARY_LANGUAGE", "ru")
SUMMARY_MAX_TOKENS = _env_int("SUMMARY_MAX_TOKENS", 350)

# ---- Server ----
PORT = _env_int("PORT", 8000)

# ---- SMAIPL (ASK endpoint) ----
# Пример из переписки с разработчиком:
# https://api.smaipl.ru/api/v1.0/ask/<ASK_TOKEN>
SMAIPL_ASK_BASE_URL = _env("SMAIPL_ASK_BASE_URL", "https://api.smaipl.ru/api/v1.0/ask")
SMAIPL_ASK_TOKEN = _env("SMAIPL_ASK_TOKEN")  # ОБЯЗАТЕЛЬНО для /smaipl-ask
SMAIPL_BOT_ID = _env_int("SMAIPL_BOT_ID", 0)  # если не знаешь — поставь 0 и передавай в запросе
SMAIPL_TIMEOUT_S = _env_float("SMAIPL_TIMEOUT_S", 45)

# ---- SMAIPL (Functions endpoint, optional) ----
# Если хочешь дергать функцию #181:
# base: https://ai.smaipl.ru/v1
SMAIPL_BASE_URL = _env("SMAIPL_BASE_URL", "https://ai.smaipl.ru/v1")
SMAIPL_API_KEY = _env("SMAIPL_API_KEY")  # Bearer token для функций (если нужен)
SMAIPL_FUNCTION_ID = _env_int("SMAIPL_FUNCTION_ID", 181)  # можно менять
SMAIPL_FUNCTION_PATH = _env("SMAIPL_FUNCTION_PATH", "/functions/{id}/execute")  # если у них другой путь — меняем тут

# ---- Default Google Doc URL (optional) ----
GOOGLE_DOC_URL = _env("GOOGLE_DOC_URL")


# ===============================
# APP
# ===============================
app = FastAPI(title="summary_bot", version="1.1.0")


# ===============================
# MODELS
# ===============================
class SummaryRequest(BaseModel):
    raw_text: str = Field(..., description="Исходный текст для саммари")
    title: Optional[str] = Field(None, description="Заголовок/контекст (опционально)")
    source: Optional[str] = Field(None, description="Источник (powershell/telegram/etc)")
    chat_id: Optional[str] = Field(None, description="Произвольный идентификатор диалога/сессии")


class SummaryResponse(BaseModel):
    summary: str
    next_steps: List[str]
    language: str
    openai_model: str


class SmaiplAskRequest(BaseModel):
    text: str = Field(..., description="Сообщение боту SMAIPL")
    chat_id: str = Field("local-test", description="chat_id для SMAIPL")
    bot_id: Optional[int] = Field(None, description="bot_id (если не задан в env)")
    ask_token: Optional[str] = Field(None, description="ask token (если не задан в env)")


class SmaiplAskResponse(BaseModel):
    ok: bool
    url: str
    status_code: int
    response: Any


class SummaryToDocRequest(BaseModel):
    raw_text: str
    title: Optional[str] = None
    source: Optional[str] = None
    chat_id: str = "local-test"
    google_doc_url: Optional[str] = None


class SummaryToDocResponse(BaseModel):
    summary: SummaryResponse
    write_mode: str
    smaipl: Optional[SmaiplAskResponse] = None
    function_call: Optional[Dict[str, Any]] = None
    note: Optional[str] = None


# ===============================
# HELPERS
# ===============================
def _require_openai() -> OpenAI:
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK is not installed. Install: pip install openai")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty. Add it to .env")
    return OpenAI(api_key=OPENAI_API_KEY)


def _summarize(raw_text: str, title: Optional[str] = None) -> Tuple[str, List[str]]:
    """
    Returns: (summary_text, next_steps_list)
    """
    client = _require_openai()

    sys = (
        "You are a concise operations assistant. "
        "Produce a short executive summary and concrete next steps. "
        f"Write in {SUMMARY_LANGUAGE}. "
        "No hallucinations. If something is unknown, say it is unknown."
    )

    user_parts = []
    if title:
        user_parts.append(f"TITLE/CONTEXT:\n{title}")
    user_parts.append(f"TEXT:\n{raw_text}")

    user = "\n\n".join(user_parts) + (
        "\n\nOUTPUT FORMAT строго JSON:\n"
        "{\n"
        '  "summary": "1-2 абзаца",\n'
        '  "next_steps": ["шаг 1", "шаг 2", "шаг 3"]\n'
        "}\n"
    )

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        max_tokens=SUMMARY_MAX_TOKENS,
    )

    content = (resp.choices[0].message.content or "").strip()
    # Пытаемся распарсить JSON, но не падаем, если модель вернула текст
    try:
        data = json.loads(content)
        summary = str(data.get("summary", "")).strip()
        ns = data.get("next_steps", [])
        if not isinstance(ns, list):
            ns = []
        next_steps = [str(x).strip() for x in ns if str(x).strip()]
        if not summary:
            summary = content
        if not next_steps:
            next_steps = []
        return summary, next_steps
    except Exception:
        # fallback — возвращаем как есть
        return content, []


def _smaipl_ask_url(ask_token: str) -> str:
    base = SMAIPL_ASK_BASE_URL.rstrip("/")
    return f"{base}/{ask_token}"


def smaipl_ask(
    text: str,
    chat_id: str,
    bot_id: int,
    ask_token: str,
    timeout_s: float = SMAIPL_TIMEOUT_S,
) -> SmaiplAskResponse:
    """
    Calls SMAIPL ask endpoint:
      POST https://api.smaipl.ru/api/v1.0/ask/<ASK_TOKEN>
      json = {"bot_id": ..., "chat_id": "...", "message": "..."}
    """
    url = _smaipl_ask_url(ask_token)

    payload = {
        "bot_id": bot_id,
        "chat_id": chat_id,
        "message": text,
    }

    r = requests.post(url, json=payload, timeout=timeout_s)
    try:
        data = r.json()
    except Exception:
        data = r.text

    ok = 200 <= r.status_code < 300
    return SmaiplAskResponse(ok=ok, url=url, status_code=r.status_code, response=data)


def smaipl_execute_function(
    function_id: int,
    payload: Dict[str, Any],
    timeout_s: float = SMAIPL_TIMEOUT_S,
) -> Dict[str, Any]:
    """
    Optional: calls SMAIPL Functions endpoint (if configured).
    Many users have 404 here if path/base_url is wrong or function doesn't exist.
    """
    if not SMAIPL_API_KEY:
        raise RuntimeError("SMAIPL_API_KEY is empty. Cannot call SMAIPL functions.")

    base = SMAIPL_BASE_URL.rstrip("/")
    path = SMAIPL_FUNCTION_PATH.format(id=function_id)
    url = f"{base}{path}"

    headers = {
        "Authorization": f"Bearer {SMAIPL_API_KEY}",
        "Content-Type": "application/json",
    }

    r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    try:
        data = r.json()
    except Exception:
        data = r.text

    return {
        "url": url,
        "status_code": r.status_code,
        "response": data,
        "ok": 200 <= r.status_code < 300,
    }


def _compose_doc_block(title: Optional[str], summary: str, next_steps: List[str], source: Optional[str]) -> str:
    lines = []
    if title:
        lines.append(f"### {title}")
    lines.append("**Summary**")
    lines.append(summary.strip())

    if next_steps:
        lines.append("\n**Next steps**")
        for i, step in enumerate(next_steps, 1):
            lines.append(f"{i}. {step}")

    if source:
        lines.append(f"\n_Source: {source}_")

    return "\n".join(lines).strip() + "\n"


# ===============================
# ROUTES
# ===============================
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "summary_bot",
        "openai_model": OPENAI_MODEL,
        "summary_language": SUMMARY_LANGUAGE,
        "has_openai_key": bool(OPENAI_API_KEY),
        "smaipl": {
            "ask_base_url": SMAIPL_ASK_BASE_URL,
            "has_ask_token": bool(SMAIPL_ASK_TOKEN),
            "bot_id": SMAIPL_BOT_ID,
            "functions_base_url": SMAIPL_BASE_URL,
            "has_functions_key": bool(SMAIPL_API_KEY),
            "function_id": SMAIPL_FUNCTION_ID,
        },
    }


@app.post("/summary", response_model=SummaryResponse)
def make_summary(req: SummaryRequest) -> SummaryResponse:
    if not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text is empty")

    try:
        summary, next_steps = _summarize(req.raw_text, req.title)
        return SummaryResponse(
            summary=summary,
            next_steps=next_steps,
            language=SUMMARY_LANGUAGE,
            openai_model=OPENAI_MODEL,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Summary generation failed: {e}")


@app.post("/smaipl-ask", response_model=SmaiplAskResponse)
def smaipl_ask_route(req: SmaiplAskRequest) -> SmaiplAskResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    ask_token = (req.ask_token or SMAIPL_ASK_TOKEN).strip()
    if not ask_token:
        raise HTTPException(status_code=400, detail="SMAIPL_ASK_TOKEN is empty (set in .env or pass ask_token)")

    bot_id = req.bot_id if req.bot_id is not None else SMAIPL_BOT_ID
    if not bot_id:
        raise HTTPException(status_code=400, detail="bot_id is empty (set SMAIPL_BOT_ID in .env or pass bot_id)")

    try:
        return smaipl_ask(text=text, chat_id=req.chat_id, bot_id=bot_id, ask_token=ask_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SMAIPL ask call failed: {e}")


@app.post("/summary-to-doc", response_model=SummaryToDocResponse)
def summary_to_doc(req: SummaryToDocRequest) -> SummaryToDocResponse:
    if not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text is empty")

    google_doc_url = (req.google_doc_url or GOOGLE_DOC_URL or "").strip()
    if not google_doc_url:
        raise HTTPException(status_code=400, detail="google_doc_url is empty (set GOOGLE_DOC_URL in .env or pass it)")

    # 1) Generate summary
    try:
        summary_text, next_steps = _summarize(req.raw_text, req.title)
        summary_obj = SummaryResponse(
            summary=summary_text,
            next_steps=next_steps,
            language=SUMMARY_LANGUAGE,
            openai_model=OPENAI_MODEL,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Summary generation failed: {e}")

    # 2) Prepare block to append
    doc_block = _compose_doc_block(req.title, summary_text, next_steps, req.source)

    # 3) Try SMAIPL Function first (if key exists), else fallback to /ask
    function_result: Optional[Dict[str, Any]] = None
    ask_result: Optional[SmaiplAskResponse] = None

    # Function payload is intentionally generic (depends on how #181 is implemented on SMAIPL side)
    function_payload = {
        "google_doc_url": google_doc_url,
        "text": doc_block,
        "title": req.title,
        "source": req.source,
        "chat_id": req.chat_id,
    }

    if SMAIPL_API_KEY:
        try:
            function_result = smaipl_execute_function(SMAIPL_FUNCTION_ID, function_payload)
            if function_result.get("ok"):
                return SummaryToDocResponse(
                    summary=summary_obj,
                    write_mode="smaipl_function",
                    function_call=function_result,
                    note="Written via SMAIPL function.",
                )
            # if not ok — fallback to ask
        except Exception as e:
            function_result = {"ok": False, "error": str(e)}

    # Fallback: send instruction to SMAIPL bot via ASK endpoint
    ask_token = SMAIPL_ASK_TOKEN.strip()
    if not ask_token:
        raise HTTPException(
            status_code=502,
            detail="SMAIPL function call failed and SMAIPL_ASK_TOKEN is empty, cannot fallback to /ask.",
        )
    if not SMAIPL_BOT_ID:
        raise HTTPException(
            status_code=502,
            detail="SMAIPL function call failed and SMAIPL_BOT_ID is empty, cannot fallback to /ask.",
        )

    # This message is crafted to demonstrate working SMAIPL integration even if function #181 is missing/404
    ask_text = (
        "Нужно добавить текст в Google Doc.\n"
        f"Google Doc: {google_doc_url}\n\n"
        "Текст для добавления (append):\n"
        f"{doc_block}"
    )

    try:
        ask_result = smaipl_ask(text=ask_text, chat_id=req.chat_id, bot_id=SMAIPL_BOT_ID, ask_token=ask_token)
        return SummaryToDocResponse(
            summary=summary_obj,
            write_mode="smaipl_ask_fallback",
            smaipl=ask_result,
            function_call=function_result,
            note=(
                "SMAIPL function did not succeed (often 404 due to wrong function/path or missing function). "
                "Fallback used SMAIPL ASK endpoint to demonstrate working integration."
            ),
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to write via SMAIPL function and SMAIPL ask fallback also failed: {e}",
        )
