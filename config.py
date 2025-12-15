import os


def must(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing env var: {name}")
    return val


TELEGRAM_BOT_TOKEN = must("TELEGRAM_BOT_TOKEN")
SMAIPL_API_URL = must("SMAIPL_API_URL")          # например: https://api.smaipl.ru/api/v1.0/ask/XXXXXXXX
SMAIPL_BOT_ID = int(must("SMAIPL_BOT_ID"))       # 5129
SUMMARY_COMMAND = os.getenv("SUMMARY_COMMAND", "/summary")
