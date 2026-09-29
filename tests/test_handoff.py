"""Горячие чаты: расписание напоминаний и карточка приглашения со шпаргалкой."""
from hh_applicant_tool.utils import handoff as h


def test_due_stage_progression():
    assert h.due_stage(1, set()) is None
    assert h.due_stage(3.5, set())[0] == 3
    assert h.due_stage(5, {3}) is None
    assert h.due_stage(25, {3})[0] == 24
    assert h.due_stage(80, {3, 24})[0] == 72
    assert h.due_stage(200, {3, 24, 72}) is None


def test_due_stage_skips_missed_early_stages():
    # бот лежал двое суток — шлём сразу «сутки без ответа», а не «3 часа»
    assert h.due_stage(50, set())[0] == 24


def test_prep_card_escapes_and_links():
    card = h.prep_card("Python <Dev>", "ООО Ромашка", "Давайте созвонимся завтра в 15:00",
                       "Компания: <b>финтех</b>", "https://hh.ru/chat/1")
    assert "Python &lt;Dev&gt;" in card and "&lt;b&gt;финтех" in card
    assert "Шпаргалка" in card and 'href="https://hh.ru/chat/1"' in card
    assert "Шпаргалка" not in h.prep_card("V", "", "текст", "", "")
