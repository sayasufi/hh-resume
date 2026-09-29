"""Решения reply-employers по черновику LLM и истории чата (без сети)."""
from hh_applicant_tool.operations.reply_employers import Operation

HDR = ("Здравствуйте! Спасибо за интерес к вакансии. Чтобы работодатель узнал о вас больше, "
       "пожалуйста, ответьте на несколько вопросов. Это займет всего пару минут. Начнем?")
Q = "Готовы ли вы работать с нами по самозанятости, оформив её на себя?"


def _m(who, text):
    return {"author": {"participant_type": who}, "text": text}


def test_answer_rejected_by_questionnaire():
    chat = [_m("applicant", "письмо"), _m("employer", HDR), _m("employer", Q),
            _m("applicant", "Здравствуйте! По этому вопросу готов обсудить детали на созвоне"),
            _m("employer", Q)]
    assert Operation._answer_rejected(chat, Q)
    # анкета просто ждёт первого ответа (повторы по дням) — это не отказ
    waiting = [_m("applicant", "письмо"), _m("employer", HDR), _m("employer", Q),
               _m("employer", HDR), _m("employer", Q)]
    assert not Operation._answer_rejected(waiting, Q)


def test_check_reply_verdicts():
    op = Operation()
    assert op._check_reply("Да, работал. В Cally проектировал микросервисы.", "Работали ли вы с Kafka?", True) == ("ok", "Да")
    assert op._check_reply("SKIP", "Рассмотрим резюме", False) == ("skip", "")
    assert op._check_reply("ASK", Q, False) == ("ask", "")
    assert op._check_reply("Здравствуйте! По этому вопросу готов обсудить детали на созвоне", Q, False)[0] == "fix"
    assert op._check_reply("Хорошо, спасибо! Сейчас напишу вам в Telegram", "Пишите в тг @hr", False)[0] == "fix"
    assert op._check_reply("Опыт с Go — [количество] лет", "Сколько лет Go?", False)[0] == "fix"
    assert op._check_reply("Около 4 лет в продакшене.", "Сколько лет вы писали Go в продакшене?", False) == ("ok", "Около 4 лет в продакшене.")
    # на закрытый вопрос анкеты не «Да/Нет» -> человеку
    assert op._check_reply("Сложно сказать однозначно", "Был ли опыт с MariaDB?", True)[0] == "ask"
