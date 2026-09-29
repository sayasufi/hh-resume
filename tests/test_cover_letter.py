"""Требования работодателя к отклику: извлечение из описания и кодовое слово в письме."""
from hh_applicant_tool.utils import cover_letter as cl

DESC = (
    "<p><strong>Чем предстоит заниматься:</strong></p><ul><li>Разработка сервисов на Python</li></ul>"
    "<p>💥 Обращаем внимание, что отклики без сопроводительного письма с ответами на вопросы "
    "не рассматриваются. Ответьте, пожалуйста, на вопросы:</p>"
    "<ol><li>Сколько лет коммерческого опыта с Python?</li>"
    "<li>Работали ли с Kafka?</li><li>Ваши зарплатные ожидания?</li></ol>"
    "<p>В начале письма напишите слово «Питон», чтобы мы поняли, что вы дочитали.</p>"
    "<p>Мы предлагаем: ДМС, удалёнку.</p>"
)


def test_description_lines_keeps_list_structure():
    lines = cl.description_lines(DESC)
    assert "- Сколько лет коммерческого опыта с Python?" in lines
    assert all("<" not in line for line in lines)


def test_extract_requirements_takes_trigger_and_questions():
    reqs = cl.extract_requirements(cl.description_lines(DESC))
    assert any("без сопроводительного" in r for r in reqs)
    assert "- Сколько лет коммерческого опыта с Python?" in reqs
    assert "- Работали ли с Kafka?" in reqs
    assert "- Ваши зарплатные ожидания?" in reqs
    assert any("«Питон»" in r for r in reqs)
    assert not any("ДМС" in r for r in reqs)          # обычный текст вакансии — не требование
    assert not any("Разработка сервисов" in r for r in reqs)


def test_no_requirements_in_plain_vacancy():
    plain = "<p>Ищем backend-разработчика.</p><ul><li>Python</li><li>PostgreSQL</li></ul>"
    assert cl.extract_requirements(cl.description_lines(plain)) == []
    assert cl.requirements_block([]) == ""


def test_codeword_at_start_and_anywhere():
    reqs = cl.extract_requirements(cl.description_lines(DESC))
    cw = cl.extract_codeword(reqs)
    assert cw == ("Питон", True)
    assert cl.ensure_codeword("Здравствуйте! Пишу на Python 4 года.", cw).startswith("Питон\n")
    assert cl.ensure_codeword("Питон. Здравствуйте!", cw) == "Питон. Здравствуйте!"
    anywhere = cl.extract_codeword(["Укажите в отклике кодовое слово \"синий кит\""])
    assert anywhere == ("синий кит", False)
    assert cl.ensure_codeword("Здравствуйте! Кодовое слово: синий кит.", anywhere).startswith("Здравствуйте")
    assert cl.ensure_codeword("Здравствуйте!", None) == "Здравствуйте!"
