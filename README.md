# summary_bot (FastAPI)

Variant A:
- summary генерируется на нашей стороне (OpenAI)
- запись в Google Doc делается через SMAIPL function #181 (append_text_to_google_doc)

## Setup
1) Создай виртуальное окружение (PyCharm предложит автоматически)
2) Установи зависимости:
   pip install -r requirements.txt
3) Скопируй .env.example → .env и заполни:
   - OPENAI_API_KEY
   - SMAIPL_ENDPOINT
   - SMAIPL_API_KEY
   - GOOGLE_DOC_URL

## Run (локально)
uvicorn app:app --reload --port 8000

## Проверка
GET http://127.0.0.1:8000/health

## Основной вызов
POST http://127.0.0.1:8000/summary-to-doc
Content-Type: application/json

Body example:
{
  "raw_text": "длинный текст...",
  "chat_id": "12345",
  "source": "telegram",
  "title": "SUMMARY (test)"
}

Ожидается:
- summary вернётся в ответе
- summary будет дописан в Google Doc через SMAIPL #181
