"""Подбор ответа из базы ответов кандидата (чистые функции; покрыты tests/test_answer_match.py)."""
from __future__ import annotations

import html
import re

from . import dialog_rules as rules

MATCH_SYS = (
    "Тебе дают вопрос работодателя и пронумерованный список вопросов, на которые кандидат уже "
    "отвечал раньше (с его ответами). Если один из них спрашивает ПО СМЫСЛУ то же самое, так "
    "что прежний ответ кандидата честно подходит и к новому вопросу, — выведи ТОЛЬКО его "
    "номер. Если такого нет или есть сомнение — выведи NONE. Похожая тема — ещё не то же "
    "самое: «готовы работать по самозанятости?» и «есть ли у вас ИП?» — разные вопросы."
)


def build_match_query(question: str, answers: list[dict]) -> str:
    lines = [f"{a['id']}. Вопрос: {a['question'][:300]} | Ответ кандидата: {a['answer'][:200]}"
             for a in answers]
    return f"ВОПРОС РАБОТОДАТЕЛЯ: {question[:600]}\n\nРАНЕЕ ОТВЕЧЕННЫЕ:\n" + "\n".join(lines)


def parse_match(raw: str, answers: list[dict]) -> dict | None:
    """Ответ LLM -> запись из базы (только номер из списка) или None."""
    m = re.search(r"\d+", raw or "")
    if not m or (raw or "").strip().upper().startswith("NONE"):
        return None
    by_id = {int(a["id"]): a for a in answers}
    return by_id.get(int(m.group(0)))


def reply_from_bank(stored_answer: str, yes_no_questionnaire: bool) -> str | None:
    """Что отправить работодателю: анкете hh на закрытый вопрос — ровно «Да»/«Нет»,
    иначе — ответ кандидата как есть. None — ответ не подходит по форме."""
    if yes_no_questionnaire:
        return rules.normalize_yes_no(stored_answer)
    return (stored_answer or "").strip() or None


def question_card(question: str, vacancy: str, employer: str, link: str, yes_no: bool) -> str:
    """HTML-текст сообщения в Telegram с вопросом работодателя."""
    how = ("Нажми кнопку или ответь на это сообщение (reply) своим текстом"
           if yes_no else "Ответь на это сообщение (reply) своим текстом")
    return (
        f"🙋 <b>Вопрос работодателя</b> — {html.escape(vacancy or '')}"
        + (f" ({html.escape(employer)})" if employer else "")
        + f"\n\n«{html.escape((question or '')[:1200])}»\n\n"
        + f"{how} — отправлю работодателю и запомню: на такой же вопрос дальше отвечу сам."
        + (f'\n<a href="{html.escape(link)}">открыть чат →</a>' if link else "")
    )


def question_keyboard(pending_id: int, yes_no: bool) -> dict:
    row = ([{"text": "✅ Да", "callback_data": f"qa:{pending_id}:yes"},
            {"text": "❌ Нет", "callback_data": f"qa:{pending_id}:no"}] if yes_no else [])
    return {"inline_keyboard": [row + [{"text": "⏭ Пропустить",
                                        "callback_data": f"qa:{pending_id}:skip"}]]}
