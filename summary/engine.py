from openai import OpenAI
from .prompt import SummaryPromptConfig, build_summary_prompt

SYSTEM_MSG = "Ты аккуратный аналитик. Не выдумывай факты. Пиши кратко и структурировано."

def generate_summary_openai(raw_text: str, api_key: str, model: str, cfg: SummaryPromptConfig) -> str:
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)
    prompt = build_summary_prompt(raw_text, cfg)

    # Используем совместимый и широко применяемый метод chat.completions
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("Empty summary from OpenAI")
    return text
