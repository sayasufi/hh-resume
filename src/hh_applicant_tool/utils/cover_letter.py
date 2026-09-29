"""Что работодатель просит сделать в отклике: вопросы, кодовое слово, «начните письмо с…».

Часть работодателей прямо пишет «отклики без ответов на вопросы в вакансии не
рассматриваются» или прячет кодовое слово, чтобы отсеять рассылки ботов. Раньше письмо
это игнорировало. Здесь — детерминированное извлечение таких требований из описания
вакансии (без LLM) и проверка кодового слова в готовом письме.
"""
from __future__ import annotations

import html
import re

MAX_ITEMS = 8
MAX_ITEM_LEN = 300

_TRIGGER = re.compile(
    r"сопроводительн|в отклике|в письме|кодов\w*\s+слов|ответьте на|ответить на (?:следующие|несколько|вопрос)|"
    r"начните (?:письмо|отклик|сообщение|ответ)|в начале (?:письма|отклика|сообщения)|"
    r"(?:напишите|укажите|приложите|пришлите) в (?:отклике|письме|сопроводительном)|"
    r"без сопроводительного|cover letter|in your (?:application|message|cover)|"
    r"start your (?:message|application|letter)|include the (?:word|phrase)",
    re.I,
)
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)]|[-•*–—])\s*\S")


def description_lines(description_html: str) -> list[str]:
    """HTML описания -> строки: границы абзацев/пунктов списка становятся переводами строк."""
    s = re.sub(r"(?i)<\s*(?:br|/p|/li|/h\d|/div)\s*/?>", "\n", description_html or "")
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return [" ".join(line.split()) for line in s.split("\n") if line.strip()]


def extract_requirements(lines: list[str]) -> list[str]:
    """Строки-требования к отклику + вопросы-пункты сразу после «ответьте на вопросы:»."""
    out: list[str] = []
    i = 0
    while i < len(lines) and len(out) < MAX_ITEMS:
        line = lines[i]
        if _TRIGGER.search(line):
            out.append(line[:MAX_ITEM_LEN])
            # «…ответьте на вопросы:» — следом идут сами вопросы списком / с «?»
            j = i + 1
            while (j < len(lines) and len(out) < MAX_ITEMS and j - i <= 6
                   and (_LIST_ITEM.match(lines[j]) or lines[j].rstrip().endswith("?"))
                   and (line.rstrip().endswith(":") or lines[j].rstrip().endswith("?"))):
                out.append(lines[j][:MAX_ITEM_LEN])
                j += 1
            i = j
            continue
        i += 1
    return out


_QUOTED = r"[«\"“'']\s*([^»\"”'']{1,40}?)\s*[»\"”'']"
# (шаблон, слово обязано стоять В НАЧАЛЕ письма)
_CODEWORD = [
    (re.compile(r"начните\s+(?:письмо|отклик|сообщение|ответ)\s+(?:со\s+слова|с\s+фразы|со?)\s*" + _QUOTED, re.I), True),
    (re.compile(r"в\s+начале\s+(?:письма|отклика|сообщения)[^«\"“'']{0,30}" + _QUOTED, re.I), True),
    (re.compile(r"start your (?:message|application|letter) with\s*" + _QUOTED, re.I), True),
    (re.compile(r"кодов\w*\s+(?:слов\w*|фраз\w*)[^«\"“'']{0,40}" + _QUOTED, re.I), False),
    (re.compile(r"include the (?:word|phrase)\s*" + _QUOTED, re.I), False),
]


def extract_codeword(requirements: list[str]) -> tuple[str, bool] | None:
    """(кодовое слово, обязано ли оно открывать письмо) или None."""
    for item in requirements:
        for rx, at_start in _CODEWORD:
            if (m := rx.search(item)):
                return m.group(1).strip(), at_start
    return None


def ensure_codeword(letter: str, codeword: tuple[str, bool] | None) -> str:
    """Кодовое слово обязано быть в письме (а «начните с…» — в самом начале); если модель
    его потеряла или поставила не туда — ставим первой строкой."""
    if not codeword:
        return letter
    word, at_start = codeword
    text = (letter or "").strip()
    ok = (text.lower().startswith(word.lower()) if at_start
          else word.lower() in text.lower())
    return text if ok else f"{word}\n\n{text}"


def requirements_block(requirements: list[str]) -> str:
    if not requirements:
        return ""
    return (
        "\n\nТРЕБОВАНИЯ РАБОТОДАТЕЛЯ К ОТКЛИКУ (из текста вакансии — выполни каждое):\n"
        + "\n".join(f"- {r}" for r in requirements)
    )
