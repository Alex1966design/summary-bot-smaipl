import os
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import requests


class SMAIPLRequestError(RuntimeError):
    pass


@dataclass
class SMAIPLClient:
    base_url: str
    api_key: str
    model: str
    timeout_s: int = 45

    @staticmethod
    def from_env() -> "SMAIPLClient":
        base_url = os.getenv("SMAIPL_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("SMAIPL_API_KEY", "").strip()
        model = os.getenv("SMAIPL_MODEL", "").strip()
        timeout_s = int(os.getenv("SMAIPL_TIMEOUT_S", "45").strip() or "45")

        if not base_url:
            raise ValueError("SMAIPL_BASE_URL is missing")
        if not api_key:
            raise ValueError("SMAIPL_API_KEY is missing")
        if not model:
            raise ValueError("SMAIPL_MODEL is missing")

        return SMAIPLClient(base_url=base_url, api_key=api_key, model=model, timeout_s=timeout_s)

    def chat_completions(self, messages: List[Dict[str, str]], temperature: float = 0.2,
                         max_tokens: Optional[int] = None) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=self.timeout_s)
        except Exception as e:
            raise SMAIPLRequestError(f"SMAIPL request failed: {e}") from e

        if r.status_code >= 400:
            raise SMAIPLRequestError(f"SMAIPL HTTP {r.status_code}: {r.text}")

        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            raise SMAIPLRequestError(f"Unexpected SMAIPL response format: {json.dumps(data, ensure_ascii=False)}")
