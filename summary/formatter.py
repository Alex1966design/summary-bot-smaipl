from datetime import datetime

def format_summary_block(summary: str, chat_id: str | None, source: str, title: str | None = None) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_title = title or "SUMMARY"
    chat_part = f" — Chat {chat_id}" if chat_id else ""
    return (
        "\n\n"
        "==============================\n"
        f"{header_title} — {ts}{chat_part}\n"
        f"Источник: {source}\n\n"
        f"{summary}\n"
        "==============================\n"
    )
