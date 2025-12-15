import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SMAIPL_API_URL = os.environ["SMAIPL_API_URL"]           # полный URL вида https://api.smaipl.ru/api/v1.0/ask/<token>
SMAIPL_BOT_ID = int(os.environ.get("SMAIPL_BOT_ID", "5129"))
SUMMARY_COMMAND = os.environ.get("SUMMARY_COMMAND", "/summary")

