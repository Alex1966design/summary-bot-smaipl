from dataclasses import dataclass

@dataclass(frozen=True)
class SummaryPromptConfig:
    language: str = "ru"
    min_bullets: int = 5
    max_bullets: int = 12

def build_summary_prompt(raw_text: str, cfg: SummaryPromptConfig) -> str:
    # Строгая структура — меньше “болтовни”, больше повторяемости.
    return f"""
Сделай структурированное summary на языке: {cfg.language}.
Требования:
- 4 раздела: Ключевые пункты / Решения / Следующие шаги / Риски и вопросы
- Всего {cfg.min_bullets}–{cfg.max_bullets} буллетов суммарно (на все разделы)
- Без воды, только факты из текста
- Ничего не выдумывай
- Если решений нет — напиши: "Решений не зафиксировано"

Текст:
<<<
{raw_text}
>>>
""".strip()
