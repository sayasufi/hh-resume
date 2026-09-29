"""Правила hh-чатов на реальных фразах из переписок (сентябрь 2026)."""
from hh_applicant_tool.utils import dialog_rules as R

HDR = ("Здравствуйте! Спасибо за интерес к вакансии. Чтобы работодатель узнал о вас больше, "
       "пожалуйста, ответьте на несколько вопросов. Это займет всего пару минут. Начнем?")


def test_questionnaire_detection():
    assert R.is_hh_questionnaire([HDR, "Готовы ли вы к оформлению по ИП?"])
    assert R.is_hh_questionnaire(["Здравствуйте, Семен! Я ИИ-помощник hh. Спасибо..."])
    assert not R.is_hh_questionnaire(["Семен, здравствуйте! Давайте созвонимся завтра."])
    assert R.is_questionnaire_start(HDR)
    assert R.is_questionnaire_start("Используем эти ответы?")


def test_yes_no_questions():
    yes_no = [
        "Готовы ли вы работать с нами по самозанятости, оформив её на себя?",
        "Был ли у вас релевантный опыт работы с Python/FastAPI + Next.js/TypeScript?",
        "Работали ли вы с микросервисной архитектурой и брокерами сообщений (Kafka, RabbitMQ или аналоги)?",
        "Подскажите, пожалуйста, был ли опыт работы с MariaDB?",
        "- Был ли опыт работы через ИП?",
        "У вас есть опыт работы с холодными звонками?",
        "Are you ready to work from 16-24 Moscow time?",
        "Добрый день! Подскажите, есть ли у вас опыт работы с библиотеками для написания ИИ-агентов?",
    ]
    for q in yes_no:
        assert R.is_yes_no_question(q), q
    open_q = [
        "Сколько лет вы писали Go в продакшене?",
        "Какими инструментами вы привлекали B2B-клиентов?",
        "Расскажите, какое у вас рабочее устройство и какова пропускная способность вашего интернета?",
        "Ваши зарплатные ожидания?",
        "Какой сертификат у вас имеется",
    ]
    for q in open_q:
        assert not R.is_yes_no_question(q), q


def test_no_reply_needed():
    assert R.is_no_reply_needed("Семен Александрович, здравствуйте! Рассмотрим ваше резюме. Если навыки и опыт подойдут для позиции, мы свяжемся с вами.")
    assert R.is_no_reply_needed("Вакансия не прошла проверку и была удалена. Вы можете откликаться на другие вакансии")
    assert R.is_no_reply_needed("Ответьте на приглашение, даже если оно вам не интересно. Так мы сможем рекомендовать")
    assert R.is_no_reply_needed("Благодарим за отклик. К сожалению, в этот раз мы приняли решение не продолжать с вашей кандидатурой.")
    assert not R.is_no_reply_needed("Давайте созвонимся, да. Сегодня или завтра в любое время.")


def test_funnel_spam():
    assert R.is_funnel_spam("Пройдите демонстрацию вакансии на 100% >> https://offer-for-job.ru/hh/SD9iCg0")
    assert R.is_funnel_spam("Семен, приглашаем Вас пройти ознакомительное демо по вакансии «Амбассадор».")
    assert not R.is_funnel_spam("Заполните, пожалуйста, анкету: https://forms.gle/abc")


def test_sensitive():
    for q in ("В связи с СВО и возможно у нас след. проект будет в этой сфере, если бы вам подобный оффер пришел - ваши действия?",
              "Готовы ли вы работать с нами по самозанятости, оформив её на себя?",
              "Готовы ли вы к оформлению по ИП?",
              "Are you ready to pay taxes on your own?",
              "Готовы ли вы рассматривать работу без фиксированной оплаты — за долю в проекте?",
              "Рассматриваете оформление по ИП или СЗ или ГПХ с физ лицом?"):
        assert R.is_sensitive(q), q
    for q in ("Работали ли вы с Kafka?", "Кадровая политика компании простая", "Сколько лет вы писали Go?",
              "компания в сфере «Красота и Здоровье»", "при возрастающей нагрузке"):
        assert not R.is_sensitive(q), q


def test_promises_and_call_offers():
    assert R.has_promise("Хорошо, спасибо! Сейчас напишу вам в Telegram, прикреплю резюме")
    assert R.has_promise("Спасибо за вопросы. Я изучу их и вернусь к вам с ответами.")
    assert not R.has_promise("Да, работал с Kafka и RabbitMQ в продакшене.")
    assert R.offers_call_unprompted("Опыта с ИП не было, готов обсудить на созвоне", "- Был ли опыт работы через ИП?")
    assert not R.offers_call_unprompted("Спасибо, удобно завтра", "Давайте созвонимся завтра?")


def test_placeholders():
    assert R.has_template_placeholder("Опыт с Go — [количество] лет")
    assert R.has_template_placeholder("Здравствуйте, <имя>!")
    assert not R.has_template_placeholder("[+] Опыт с SQL\n[-] Security Vision\n[ ] Jira")


def test_normalize_yes_no():
    assert R.normalize_yes_no("Да, работал.") == "Да"
    assert R.normalize_yes_no("нет") == "Нет"
    assert R.normalize_yes_no("Yes.") == "Да"
    assert R.normalize_yes_no("По этому вопросу готов обсудить") is None


def test_ask_skip():
    assert R.is_ask("ASK")
    assert R.is_ask("ask.")
    assert R.is_skip("SKIP")
    assert R.is_skip("")
    assert not R.is_skip("Да")


def test_short_title():
    t = "Разработчик расчетных кодов / инженер-расчетчик в атомной энергетике"
    assert R.short_title(t) == t
    long = "Integration / AI Automation Developer — Python/API/n8n (Разработчик автоматизации бизнеса)"
    s = R.short_title(long, 60)
    assert s.endswith("…") and len(s) <= 61 and not s[:-1].endswith(" ")


def test_ball_on_our_side():
    assert R.ball_on_our_side(["Напишите нам по указанным контактам @hr_example (Tlg)"], ["письмо"])
    assert R.ball_on_our_side(["Направляем Вам техническое задание для ознакомления."], ["письмо"])
    assert R.ball_on_our_side(["Рассмотрим ваше резюме."], ["письмо", "Сейчас напишу вам в Telegram"])
    assert not R.ball_on_our_side(["Рассмотрим ваше резюме. Если подойдёте — свяжемся."], ["письмо", "Спасибо!"])
