"""База ответов: сопоставление вопроса, форма ответа, карточка вопроса в Telegram."""
from hh_applicant_tool.utils import answer_match as am

BANK = [
    {"id": 3, "question": "Готовы ли вы работать с нами по самозанятости, оформив её на себя?", "answer": "Да"},
    {"id": 7, "question": "Каким уровнем английского владеете?", "answer": "B2, свободно читаю документацию"},
]


def test_build_match_query_lists_ids():
    q = am.build_match_query("Вы готовы оформить самозанятость?", BANK)
    assert "3. Вопрос: Готовы ли вы работать" in q and "7. Вопрос: Каким уровнем" in q


def test_parse_match():
    assert am.parse_match("3", BANK)["answer"] == "Да"
    assert am.parse_match("Номер 7", BANK)["id"] == 7
    assert am.parse_match("NONE", BANK) is None
    assert am.parse_match("42", BANK) is None      # номера нет в списке — не выдумываем
    assert am.parse_match("", BANK) is None


def test_reply_from_bank_form():
    assert am.reply_from_bank("Да, готов", True) == "Да"
    assert am.reply_from_bank("B2, свободно читаю документацию", True) is None  # анкете нужно Да/Нет
    assert am.reply_from_bank("B2, свободно читаю документацию", False) == "B2, свободно читаю документацию"


def test_question_card_and_keyboard():
    card = am.question_card("Готовы <b>к ИП</b>?", "Backend", "ООО Ромашка", "https://hh.ru/chat/1", True)
    assert "&lt;b&gt;к ИП&lt;/b&gt;" in card and "hh.ru/chat/1" in card
    kb = am.question_keyboard(12, True)["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["qa:12:yes", "qa:12:no", "qa:12:skip"]
    assert [b["callback_data"] for b in am.question_keyboard(12, False)["inline_keyboard"][0]] == ["qa:12:skip"]
