"""Поисковые запросы hh по резюме — источник вакансий «поиск» для apply-similar.

Похожие вакансии hh (/resumes/{id}/similar_vacancies) со временем выдыхаются: 29.09.2026
у backend-разработчика 400 похожих не дали ни одной новой подходящей, а обычный поиск
«python OR golang OR backend» по названию за 14 дней нашёл 412 удалённых вакансий.

Запросы собирает LLM по профилю резюме и хранит в настройках пользователя
(apply.search_queries). Пересобираются, только когда меняется сам профиль
(название/навыки/должности), — поднятие резюме каждые 2 часа не в счёт.
"""
from __future__ import annotations

import hashlib
import re

MAX_QUERIES = 6
# Версия промпта входит в отпечаток: сменили правила — запросы пересоберутся у всех.
QUERY_VERSION = "4"

QUERY_SYS = (
    "Ты составляешь поисковые запросы для hh.ru — поиск идёт по НАЗВАНИЮ вакансии, под "
    "профиль кандидата. Выведи от 3 до 6 запросов, каждый на отдельной строке, без нумерации "
    "и пояснений.\n"
    "Как работает поиск hh: слова через пробел — ВСЕ должны быть в названии (И), поэтому "
    "несколько технологий через пробел почти ничего не находят. Варианты через OR — любой "
    "из них (ИЛИ). Фраза из нескольких слов — в двойных кавычках.\n"
    "Правила:\n"
    "- Один запрос = одно направление: роль ИЛИ ключевой язык/стек, с синонимами через OR, "
    "по-русски и по-английски, как реально пишут в названиях вакансий. Каждый запрос — на "
    "своей строке; символ | не используй.\n"
    "Примеры запросов (каждый — отдельная строка):\n"
    "python OR golang\n"
    "backend OR бэкенд\n"
    "\"аналитик данных\" OR \"data analyst\"\n"
    "dwh OR etl OR \"data engineer\"\n"
    "llm OR \"ai engineer\" OR \"ml engineer\"\n"
    "- Только профессия и стек кандидата: без уровня (junior/senior/lead), города, формата "
    "работы, слов «вакансия», «удалённо».\n"
    "- Не пиши слишком общих или многозначных слов, которые встречаются в названиях чужих "
    "профессий: «разработчик», «программист», «менеджер», «специалист», «инженер» без "
    "уточнения стека; «go» (ловит «Яндекс Go») — вместо него golang."
)


def profile_key(title: str, skills: list, positions: list) -> str:
    """Отпечаток профиля: меняется, только если поменялось содержание резюме."""
    src = "|".join([
        QUERY_VERSION,
        " ".join((title or "").lower().split()),
        ",".join(sorted(str(s).lower() for s in skills or [])),
        ",".join(" ".join(str(p).lower().split()) for p in positions or []),
    ])
    return hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]


_BARE_GO = re.compile(r'(?<![\w"])go(?![\w"])', re.I)


def _balanced(q: str) -> bool:
    return q.count("(") == q.count(")") and q.count('"') % 2 == 0


def parse_queries(raw: str) -> list[str]:
    """Ответ LLM -> чистый список запросов (без нумерации/маркеров/кавычек-ёлочек, дублей)."""
    out: list[str] = []
    seen: set[str] = set()
    # «a OR b | c OR d» — модель склеивает запросы через |, режем на отдельные
    parts = [p for line in (raw or "").splitlines() for p in line.split("|")]
    for line in parts:
        q = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("«»`'").strip()
        q = " ".join(q.split())
        q = _BARE_GO.sub("golang", q)  # голое go ловит «Курьер в Яндекс Go»
        if not (2 <= len(q) <= 80) or not _balanced(q) or q.endswith(":"):
            continue
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= MAX_QUERIES:
            break
    return out


def split_stored(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split("\n") if x.strip()]


async def ensure_queries(settings, chat, profile_text: str, key: str) -> list[str]:
    """Сохранённые запросы, если профиль не менялся; иначе собрать через LLM и сохранить.
    settings — async get_value/set_value (tool.storage.settings). Ошибка LLM или пустой
    ответ -> [] и ничего не сохраняем (источник «поиск» в этом прогоне просто пропускается)."""
    stored = split_stored(await settings.get_value("apply.search_queries"))
    if stored and await settings.get_value("apply.search_queries_key") == key:
        return stored
    if chat is None:
        return stored
    raw = await chat.send_message(f"ПРОФИЛЬ КАНДИДАТА:\n{profile_text}")
    queries = parse_queries(raw)
    if len(queries) < 2:
        return stored
    await settings.set_value("apply.search_queries", "\n".join(queries))
    await settings.set_value("apply.search_queries_key", key)
    return queries
