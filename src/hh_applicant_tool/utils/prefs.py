"""Общие предпочтения кандидата — желаемая ЗП и форматы работы.

Одно место правды, читают ВЕЗДЕ: поиск/фильтр вакансий и заземление писем на всех
платформах (hh, GetMatch, Habr, TG). Хранится per-аккаунт в users.preferences:
  preferences.salary       — строка («300000», «300 000 руб», «300к»)
  preferences.work_format  — список из {"remote","hybrid","onsite"} (любое подмножество)

Правило формата — «гибрид»: жёстко отбрасываем вакансию ТОЛЬКО если у неё формат
указан и не пересекается с выбранным; формат не выбран или у вакансии не указан -> ок.
ЗП — мягкая: ориентир в поиске и письмах, по ней ничего не выкидываем.
"""
import re

# канон -> (hh work_format id, hh schedule id, человекочитаемо)
WF = {
    "remote": ("REMOTE", "remote", "удалённо"),
    "hybrid": ("HYBRID", None, "гибрид"),
    "onsite": ("ON_SITE", "fullDay", "офис"),
}
WF_ORDER = ("remote", "hybrid", "onsite")

# алиасы из разных источников (hh work_format/schedule, getmatch, habr, ввод) -> канон
_ALIAS = {
    "remote": "remote", "удал": "remote", "удалённо": "remote", "удаленно": "remote",
    "удалёнка": "remote", "удаленка": "remote", "remote_work": "remote",
    "hybrid": "hybrid", "гибрид": "hybrid", "гибридный": "hybrid",
    "onsite": "onsite", "on_site": "onsite", "fullday": "onsite", "office": "onsite",
    "офис": "onsite", "в офисе": "onsite", "field_work": "onsite",
    "flyinflyout": "onsite", "shift": "onsite", "flexible": "remote",
}


def canon_wf(v) -> str | None:
    """Любое обозначение формата -> канон ('remote'/'hybrid'/'onsite') или None."""
    if not v:
        return None
    s = str(v).strip().lower()
    if s in WF:
        return s
    return _ALIAS.get(s)


def wanted_formats(prefs: dict | None) -> set:
    """Выбранные кандидатом форматы из preferences.work_format -> set канонов (или пусто)."""
    lst = (prefs or {}).get("work_format") or []
    if isinstance(lst, str):
        lst = [lst]
    return {c for c in (canon_wf(x) for x in lst) if c}


def format_ok(vac_formats, wanted: set) -> bool:
    """Жёсткий фильтр формата (правило «гибрид»): True = вакансия подходит.
    vac_formats — любые обозначения формата вакансии (list/str/None).
    Пропускаем (False) только если выбор задан И у вакансии формат указан И не совпал."""
    if not wanted:
        return True
    if isinstance(vac_formats, str):
        vac_formats = [vac_formats]
    vf = {c for c in (canon_wf(x) for x in (vac_formats or [])) if c}
    if not vf:
        return True
    return bool(vf & wanted)


def hh_work_format_ids(wanted: set) -> list:
    """Каноны -> id для параметра поиска hh work_format (REMOTE/HYBRID/ON_SITE)."""
    return [WF[c][0] for c in WF_ORDER if c in wanted]


def parse_salary(s) -> int | None:
    """'300000' / '300 000 руб' / '300к' / 300000 -> 300000 (int) или None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s) or None
    t = str(s).lower().replace(" ", "").replace(" ", "")
    m = re.search(r"(\d+)\s*(к|k|тыс|т\.р|тр)?", t)
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2):
        n *= 1000
    return n or None


def labels_ru(wanted: set) -> str:
    """'удалённо, гибрид' — для писем/промптов."""
    return ", ".join(WF[c][2] for c in WF_ORDER if c in wanted)
