"""Правила ведения hh-чатов: чистые функции без сети/БД (покрыты tests/test_dialog_rules.py).

Используются reply_employers (что отвечать и когда звать человека), followup_stalled
(когда не напоминать о себе) и notify_actions (что не класть в «Дела»).
"""
from __future__ import annotations

import re

# --- авто-анкета hh («Чтобы работодатель узнал о вас больше… Начнем?», ИИ-помощник hh) ---
_QUESTIONNAIRE_MARKERS = (
    "чтобы работодатель узнал о вас больше",
    "похоже, вы отвечали на часть вопросов",
    "используем эти ответы",
    "я ии-помощник hh",
    "я ассистент первичного отбора",
)


def is_hh_questionnaire(employer_texts: list[str]) -> bool:
    """Чат ведёт автоматическая анкета hh (а не живой HR)."""
    return any(m in (t or "").lower() for t in employer_texts for m in _QUESTIONNAIRE_MARKERS)


# Открытый вопрос (сколько/какой/расскажите…) — «Да/Нет» тут не ответ.
_OPEN_Q = re.compile(
    r"^\W*(?:[а-яё]+,\s*)?(сколько|какой|какая|какое|какие|каким|какого|как\b|где|когда|почему|зачем|"
    r"что\b|чем\b|кто|расскажите|опишите|приведите|укажите|перечислите|назовите|напишите|"
    r"how|what|which|why|where|when|who|describe|tell)", re.I)
# Закрытый вопрос: «Готовы ли…», «Был ли…», «Есть ли…», «Работали ли…», «Are you…».
_YESNO = re.compile(
    r"\bли\b|^\W*(готовы|согласны|есть у вас|у вас есть|имеется|вы готовы|подходит|"
    r"работали|разрабатывали|использовали|владеете|знакомы|are you|do you|have you|"
    r"can you|will you|is it|would you)\b", re.I)


def is_yes_no_question(text: str) -> bool:
    t = " ".join((text or "").split())
    if not t or len(t) > 300 or _OPEN_Q.search(t):
        return False
    return bool(_YESNO.search(t))


def is_questionnaire_start(text: str) -> bool:
    """«Начнем?» / «Используем эти ответы?» — ответ всегда «Да»."""
    low = (text or "").lower()
    return low.rstrip().endswith("начнем?") or low.rstrip().endswith("начнём?") or \
        "используем эти ответы" in low


# --- сообщения, на которые отвечать НЕ нужно (служебные hh, авто-шаблоны, отказы) ---
_NO_REPLY = (
    "рассмотрим ваше резюме", "мы сохранили ваше резюме", "мы обязательно рассмотрим",
    "взяли ваше резюме в работу", "свяжемся с вами в случае", "уже закрыли эту позицию",
    "вакансия не прошла проверку", "ответьте на приглашение, даже если оно вам не интересно",
    "ваши ответы отправлены работодателю", "ии-помощник завершил работу",
    "работа ии-помощника завершена", "моя часть работы завершена", "в разговоре с кандидатом я узнал",
    "передам её представителю", "передам ваши ответы представителю", "передаю информацию работодателю",
    "всё записал! передам", "всё зафиксировал", "не готовы пригласить", "приняли решение не продолжать",
    "не можем пригласить вас", "вынуждены отклонить", "продолжим с другими кандидатами",
    "рассматриваем другой опыт", "выбрали другого кандидата", "не проходим по вилке",
)


def is_no_reply_needed(text: str) -> bool:
    low = " ".join((text or "").lower().split())
    return any(p in low for p in _NO_REPLY)


# --- воронки-«демонстрации»/офферы по ссылке: не отвечаем и не кладём в «Дела» ---
_FUNNEL = re.compile(
    r"offer-job\.ru|offer-for-job\.ru|neirorealty\.ru|ознакомительное демо|"
    r"демонстраци\w+ (?:вакансии|должности)|пройдите демонстрацию|получите от нас оффер", re.I)


def is_funnel_spam(text: str) -> bool:
    return bool(_FUNNEL.search(text or ""))


# --- вопросы, на которые отвечает только сам кандидат (бот не решает за него) ---
_SENSITIVE = re.compile(
    r"\bсво\b|военн|мобилизац|оборонн|оборонк|вооруж|\bарми[ияю]|воинск|"
    r"military|army|defen[cs]e\b|политическ|религи|судимост|"
    r"без оплаты|без фиксированной оплаты|за долю|опцион|equity|"
    r"самозанят|\bип\b|\bсз\b|\bгпх\b|оформлени\w+ (?:по|через)|налог|taxes|self-employ|"
    r"гражданств|релокац|переезд|relocat|паспорт|состояни\w+ здоровья|беремен|"
    r"семейн\w+ положени|ваш возраст|сколько вам лет",
    re.I)


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text or ""))


# --- обязательства, которые бот не имеет права давать за кандидата ---
_PROMISE = re.compile(
    r"\b(напишу|свяжусь|пришлю|отправлю|подготовлю|заполню|пройду|выполню|сделаю|вернусь|"
    r"изучу|ознакомлюсь|посмотрю|перейду|согласую|позвоню|наберу|прикреплю|"
    r"i will (?:send|write|fill|prepare|contact|call))\b", re.I)
_CALL_OFFER = re.compile(r"созвон|голосом|на звонке|обсудим по телефону|в формате звонка|"
                         r"видеозвон|\bcall\b", re.I)
_CALL_ASKED = re.compile(r"созвон|звон|встреч|собесед|интервью|zoom|зум|телемост|call|meet", re.I)


def has_promise(text: str) -> bool:
    return bool(_PROMISE.search(text or ""))


def offers_call_unprompted(reply: str, employer_text: str) -> bool:
    """Бот сам зовёт на созвон, хотя работодатель о звонке не говорил (увиливание)."""
    return bool(_CALL_OFFER.search(reply or "")) and not _CALL_ASKED.search(employer_text or "")


# Скобки-заготовки ([количество], <имя>) — нельзя слать. Чек-бокс [+]/[-]/[x]/[ ] — можно.
_BRACKET = re.compile(r"\[(?![+\-xх✓✔ ]\])[^\]\n]{1,40}\]")
_ANGLE = re.compile(r"<[а-яёa-z][^>\n]{0,38}>")


def has_template_placeholder(text: str) -> bool:
    return bool(_BRACKET.search(text or "") or _ANGLE.search((text or "").lower()))


def normalize_yes_no(reply: str) -> str | None:
    """Ответ LLM на закрытый вопрос анкеты -> ровно «Да»/«Нет» (None — не распознали)."""
    w = re.sub(r"[^a-zа-яё]", "", ((reply or "").strip().split() or [""])[0].lower())
    if w in ("да", "yes"):
        return "Да"
    if w in ("нет", "no"):
        return "Нет"
    return None


# --- ask-сигнал от LLM: «нужен ответ кандидата» ---
def is_ask(reply: str) -> bool:
    return (reply or "").strip().upper().rstrip(".!").startswith("ASK")


def is_skip(reply: str) -> bool:
    r = (reply or "").strip()
    return not r or r.upper().startswith("SKIP") or r in ("-", "—")


# --- фоллоуап: название вакансии без обрыва на полуслове ---
def short_title(name: str, limit: int = 80) -> str:
    name = " ".join((name or "").split())
    if len(name) <= limit:
        return name
    cut = name[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-/(")
    return cut + "…"


# Мяч на нашей стороне: работодатель попросил что-то сделать вне чата -> «напомню о себе» неуместно.
_EXTERNAL_ASK = re.compile(
    r"t\.me/|telegram|телеграм|\bтг\b|напишите (?:мне|нам)|позвоните|наберите|"
    r"анкет|forms\.|опрос|тестов\w+ задани|техническое задание|\bтз\b|по ссылке|@[a-z0-9_]{4,}", re.I)


def ball_on_our_side(employer_texts: list[str], applicant_texts: list[str]) -> bool:
    if any(_EXTERNAL_ASK.search(t or "") for t in employer_texts):
        return True
    last_ours = applicant_texts[-1] if applicant_texts else ""
    return len(applicant_texts) > 1 and has_promise(last_ours)
