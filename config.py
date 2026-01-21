import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    smaipl_endpoint: str = os.getenv("SMAIPL_ENDPOINT", "").strip()
    smaipl_api_key: str = os.getenv("SMAIPL_API_KEY", "").strip()
    smaipl_auth_header: str = os.getenv("SMAIPL_AUTH_HEADER", "Authorization").strip()
    smaipl_auth_prefix: str = os.getenv("SMAIPL_AUTH_PREFIX", "Bearer").strip()

    google_doc_url: str = os.getenv("GOOGLE_DOC_URL", "").strip()

    summary_max_bullets: int = int(os.getenv("SUMMARY_MAX_BULLETS", "12"))
    summary_min_bullets: int = int(os.getenv("SUMMARY_MIN_BULLETS", "5"))
    summary_language: str = os.getenv("SUMMARY_LANGUAGE", "ru").strip()

    port: int = int(os.getenv("PORT", "8000"))

settings = Settings()
