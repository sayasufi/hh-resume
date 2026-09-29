from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Iterator

from ..ai import ChatOpenAI
from ..ai.base import AIError
from ..api import BadResponse, Redirect, datatypes
from ..api.datatypes import PaginatedItems, SearchVacancy
from ..api.errors import ApiError, LimitExceeded
from ..utils import cover_letter as cl
from ..utils import prefs as cprefs
from ..utils import search_queries as sq

# Дефолтные стоп-слова в НАЗВАНИИ вакансии (если apply.excluded_title_terms не задан):
# отсекаем явно не-айтишные профессии по заголовку. Стемы (подстрока) ловят склонения.
# Без «тренер/наставник» — есть AI-тренер / тех-наставник. Переопределяется настройкой.
DEFAULT_TITLE_STOP = (
    "преподавател", "учител", "репетитор", "воспитател", "педагог", "вожат", "методист",
)
# Фильтр «вакансия по профилю резюме» (LLM ДА/НЕТ). similar_vacancies от hh со временем
# уплывает: 27.09.2026 у backend-разработчика 49 из 52 откликов ушли в продажи, плюс PHP/1С/iOS.
# Стоп-слова по названию ловят только очевидное; стек и профессию решает этот фильтр.
FIT_SYS = (
    "Ты фильтр вакансий для соискателя. По профилю кандидата и вакансии ответь ОДНИМ словом: "
    "ДА или НЕТ.\n"
    "НЕТ — если вакансия относится к ДРУГОЙ профессии, чем у кандидата (например, продажи, "
    "работа с клиентами, колл-центр, поддержка пользователей, маркетинг, SMM, рекрутинг, "
    "преподавание, авторство курсов/контента — для инженера, разработчика или аналитика), ИЛИ "
    "требует другого ОСНОВНОГО стека/специализации, которых нет в профиле (например, PHP, 1С, "
    "Java, C#, iOS, Unity, C++, embedded для Python/Go-бэкендера; программист или консультант 1С "
    "для аналитика данных).\n"
    "ДА — если профессия та же или смежная и основной стек/направление совпадает хотя бы "
    "частично. Если сомневаешься — ДА."
)
# Источник «поиск по запросам» (см. utils/search_queries.py): окно и глубина выдачи.
SEARCH_PERIOD_DAYS = 14
SEARCH_PAGES = 2  # по 100 вакансий на запрос; свежие — первыми
# Бережно к hh (частые запросы подряд -> капча): не больше 8 поисковых запросов за прогон
# и пауза между ними.
SEARCH_MAX_REQUESTS = 8
SEARCH_PAUSE_SEC = (1.5, 3.0)
# Мягкий тайм-бюджет прогона: с AI-письмами 200 откликов не влезают в жёсткий kill
# оркестратора (1800с) -> выходим ЧИСТО заранее, остаток добьётся часовыми прогонами.
APPLY_MAX_RUNTIME_SEC = int(os.getenv("APPLY_MAX_RUNTIME_SEC", "1500"))
from ..main import BaseNamespace, BaseOperation
from ..storage.repositories.errors import RepositoryError
from ..utils.string import (
    bool2str,
    rand_text,
    shorten,
    unescape_string,
)

if TYPE_CHECKING:
    from ..main import HHApplicantTool


_CLICHES = {
    "«меня заинтересовала вакансия»": re.compile(r"меня\s+(?:очень\s+|крайне\s+)?заинтересовал", re.I),
    "«мне близок»": re.compile(r"мне\s+близ(?:ок|ка|ко|ки)", re.I),
    "«глубокий опыт/экспертиза»": re.compile(r"глубок\w+\s+(?:опыт|экспертиз|понимани)|экспертиз", re.I),
    "«в моём багаже»": re.compile(r"в\s+мо[её]м\s+багаже", re.I),
    "«буду рад применить свои навыки»": re.compile(r"буду\s+рад\w*\s+применить", re.I),
    "«несмотря на»": re.compile(r"несмотря\s+на", re.I),
}


def _letter_cliches(text: str) -> list[str]:
    """Шаблонные обороты, по которым письмо с первой строки читается как бот."""
    return [name for name, rx in _CLICHES.items() if rx.search(text or "")]


def _norm_title(name: str) -> str:
    return " ".join((name or "").lower().split())


def _strip_tags(s: str | None) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").split())


def _ensure_hh_apps_columns() -> None:
    """hh_apps: работодатель и название — для дедупа «та же вакансия того же работодателя»."""
    from ..storage import pgconn
    try:
        conn = pgconn.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE hh_apps ADD COLUMN IF NOT EXISTS employer_id text")
                cur.execute("ALTER TABLE hh_apps ADD COLUMN IF NOT EXISTS title text")
                # откуда вакансия: search (поиск по запросам) | similar (похожие hh)
                cur.execute("ALTER TABLE hh_apps ADD COLUMN IF NOT EXISTS source text")
                # сколько требований работодателя к отклику нашли в вакансии (0 — не было)
                cur.execute("ALTER TABLE hh_apps ADD COLUMN IF NOT EXISTS reqs int")
            conn.commit()
        finally:
            conn.close()
    except Exception as ex:
        logger.debug("hh_apps: колонки не добавлены: %r", ex)


def _already_applied_same(employer_id: str, title: str, vacancy_id) -> bool:
    """Уже откликались на вакансию с тем же названием у того же работодателя (перевыложенная
    вакансия или дубль): раньше одному работодателю уходило по 3–4 одинаковых отклика."""
    from ..storage import pgconn
    try:
        conn = pgconn.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM hh_apps WHERE account=%s AND employer_id=%s AND "
                    "lower(regexp_replace(title, '\\s+', ' ', 'g'))=%s "
                    "AND created_at > now() - interval '60 days' "
                    "UNION ALL "
                    "SELECT 1 FROM negotiations n JOIN vacancies v ON v.id=n.vacancy_id "
                    "WHERE n.account=%s AND n.employer_id=%s AND n.vacancy_id<>%s AND "
                    "lower(regexp_replace(v.name, '\\s+', ' ', 'g'))=%s LIMIT 1",
                    (pgconn.get_account(), str(employer_id), title,
                     pgconn.get_account(), int(employer_id), int(vacancy_id), title),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as ex:
        logger.debug("дедуп откликов недоступен: %r", ex)
        return False


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    resume_id: str | None
    message_list_path: Path
    ignore_employers: Path | None
    force_message: bool
    use_ai: bool
    first_prompt: str
    prompt: str
    order_by: str
    search: str
    schedule: str
    dry_run: bool
    # Пошли доп фильтры, которых не было
    experience: str
    employment: list[str] | None
    area: list[str] | None
    metro: list[str] | None
    professional_role: list[str] | None
    industry: list[str] | None
    employer_id: list[str] | None
    excluded_employer_id: list[str] | None
    currency: str | None
    salary: int | None
    only_with_salary: bool
    label: list[str] | None
    period: int | None
    date_from: str | None
    date_to: str | None
    top_lat: float | None
    bottom_lat: float | None
    left_lng: float | None
    right_lng: float | None
    sort_point_lat: float | None
    sort_point_lng: float | None
    no_magic: bool
    premium: bool
    per_page: int
    total_pages: int
    excluded_terms: str | None


class Operation(BaseOperation):
    """Откликнуться на все подходящие вакансии."""

    __aliases__ = ("apply",)

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--resume-id", help="Идентефикатор резюме")
        parser.add_argument(
            "--search",
            help="Строка поиска для фильтрации вакансий, например, 'москва бухгалтер 100500'",  # noqa: E501
            type=str,
        )
        parser.add_argument(
            "-L",
            "--message-list-path",
            "--message-list",
            help="Путь до файла, где хранятся сообщения для отклика на вакансии. Каждое сообщение — с новой строки. Символы \\n будут заменены на переносы.",  # noqa: E501
            type=Path,
        )
        parser.add_argument(
            "-f",
            "--force-message",
            "--force",
            help="Всегда отправлять сообщение при отклике",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--use-ai",
            "--ai",
            help="Использовать AI для генерации сообщений",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--first-prompt",
            help="Начальный помпт чата для генерации сопроводительного письма",
            default=(
                "Ты пишешь короткое сопроводительное письмо для отклика на hh.ru от лица "
                "кандидата — так, как написал бы живой инженер/аналитик рекрутеру, а не "
                "по шаблону. Его прочитают за 10 секунд.\n"
                "\n"
                "КАК ПИСАТЬ:\n"
                "- 3–5 коротких предложений, 350–700 знаков, один абзац, без заголовков и списков.\n"
                "- Начни с «Здравствуйте!» и СРАЗУ с самого сильного факта из опыта, который "
                "закрывает главное требование вакансии (стек, домен, задача). Не пересказывай "
                "вакансию и не повторяй её название.\n"
                "- Дальше 1–2 конкретных факта: что делал, на чём, в какой компании. Цифры и "
                "результаты — ТОЛЬКО если они дословно есть во входных данных; не придумывай "
                "проценты и метрики.\n"
                "- Если в описании вакансии есть что-то конкретное, что правда связано с опытом "
                "кандидата, — одна фраза об этом. Общих слов про «подход компании» не пиши.\n"
                "- Не пиши о своей готовности к формату, графику, занятости (part-time, проект), "
                "переезду, оформлению и другим условиям — только об опыте.\n"
                "- Если во входных данных есть блок «ТРЕБОВАНИЯ РАБОТОДАТЕЛЯ К ОТКЛИКУ» — это "
                "главное: выполни каждое требование. На вопросы ответь по пунктам, коротко и "
                "по фактам из резюме (опыта нет — честно так и напиши; ЗП и контакт — из данных "
                "ниже); кодовое слово вставь дословно, «начните письмо с…» — начни ровно с этого. "
                "Длина тогда до 1500 знаков, остальные правила сохраняются.\n"
                "- Закончи простой фразой вроде «Готов рассказать подробнее» и подписью ТОЛЬКО "
                "именем (без фамилии и отчества).\n"
                "- Пиши просто, от первого лица. Не упоминай, что ты ИИ, без плейсхолдеров.\n"
                "\n"
                "ЗАПРЕЩЁННЫЕ КЛИШЕ (так пишут все боты): «меня (очень) заинтересовала вакансия», "
                "«мне близок/близка/близко», «глубокий опыт», «экспертиза», «в моём багаже», "
                "«буду рад применить свои навыки», «масштаб задач», «амбициозн», «несмотря на», "
                "«коррелирует», «драйвит», «уверен, что смогу», «ценный вклад».\n"
                "\n"
                "Выводи только готовый текст письма."
            ),  # noqa: E501
        )
        parser.add_argument(
            "--prompt",
            help="Промпт для генерации сопроводительного письма",
            default=(
                "Напиши сопроводительное письмо к этой вакансии по правилам выше: коротко, "
                "с первого предложения — релевантный факт из моего опыта под главное "
                "требование, без клише и выдуманных цифр."
            ),  # noqa: E501
        )
        parser.add_argument(
            "--total-pages",
            "--pages",
            help="Количество обрабатываемых страниц поиска",  # noqa: E501
            default=20,
            type=int,
        )
        parser.add_argument(
            "--per-page",
            help="Сколько должно быть результатов на странице",  # noqa: E501
            default=100,
            type=int,
        )
        parser.add_argument(
            "--dry-run",
            help="Не отправлять отклики, а только выводить информацию",
            action=argparse.BooleanOptionalAction,
        )

        # Дальше идут параметры в точности соответствующие параметрам запроса
        # при поиске подходящих вакансий
        search_params_group = parser.add_argument_group(
            "Параметры поиска вакансий",
            "Эти параметры напрямую соответствуют фильтрам поиска HeadHunter API",
        )

        search_params_group.add_argument(
            "--order-by",
            help="Сортировка вакансий",
            choices=[
                "publication_time",
                "salary_desc",
                "salary_asc",
                "relevance",
                "distance",
            ],
            # default="relevance",
        )
        search_params_group.add_argument(
            "--experience",
            help="Уровень опыта работы (noExperience, between1And3, between3And6, moreThan6)",
            type=str,
            default=None,
        )
        search_params_group.add_argument(
            "--schedule",
            help="Тип графика (fullDay, shift, flexible, remote, flyInFlyOut)",
            type=str,
        )
        search_params_group.add_argument(
            "--employment", nargs="+", help="Тип занятости"
        )
        search_params_group.add_argument(
            "--area", nargs="+", help="Регион (area id)"
        )
        search_params_group.add_argument(
            "--metro", nargs="+", help="Станции метро (metro id)"
        )
        search_params_group.add_argument(
            "--professional-role", nargs="+", help="Проф. роль (id)"
        )
        search_params_group.add_argument(
            "--industry", nargs="+", help="Индустрия (industry id)"
        )
        search_params_group.add_argument(
            "--employer-id", nargs="+", help="ID работодателей"
        )
        search_params_group.add_argument(
            "--excluded-employer-id", nargs="+", help="Исключить работодателей"
        )
        search_params_group.add_argument(
            "--currency", help="Код валюты (RUR, USD, EUR)"
        )
        search_params_group.add_argument(
            "--salary", type=int, help="Минимальная зарплата"
        )
        search_params_group.add_argument(
            "--only-with-salary",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        search_params_group.add_argument(
            "--label", nargs="+", help="Метки вакансий (label)"
        )
        search_params_group.add_argument(
            "--period", type=int, help="Искать вакансии за N дней"
        )
        search_params_group.add_argument(
            "--date-from", help="Дата публикации с (YYYY-MM-DD)"
        )
        search_params_group.add_argument(
            "--date-to", help="Дата публикации по (YYYY-MM-DD)"
        )
        search_params_group.add_argument(
            "--top-lat", type=float, help="Гео: верхняя широта"
        )
        search_params_group.add_argument(
            "--bottom-lat", type=float, help="Гео: нижняя широта"
        )
        search_params_group.add_argument(
            "--left-lng", type=float, help="Гео: левая долгота"
        )
        search_params_group.add_argument(
            "--right-lng", type=float, help="Гео: правая долгота"
        )
        search_params_group.add_argument(
            "--sort-point-lat",
            type=float,
            help="Координата lat для сортировки по расстоянию",
        )
        search_params_group.add_argument(
            "--sort-point-lng",
            type=float,
            help="Координата lng для сортировки по расстоянию",
        )
        search_params_group.add_argument(
            "--no-magic",
            action="store_true",
            help="Отключить авторазбор текста запроса",
        )
        search_params_group.add_argument(
            "--premium",
            default=False,
            action=argparse.BooleanOptionalAction,
            help="Только премиум вакансии",
        )
        search_params_group.add_argument(
            "--search-field",
            nargs="+",
            help="Поля поиска (name, company_name и т.п.)",
        )
        search_params_group.add_argument(
            "--excluded-terms",
            type=str,
            help="Исключить вакансии, если название или snippet содержит любую из подстрок (через запятую, например, junior, bitrix, дружный коллектив). Это принудительный фильтр для результатов поиска",
        )

    async def run(
        self,
        tool: HHApplicantTool,
    ) -> None:
        # Тумблер Mini App: отклики можно выключить
        from ..storage import pgconn
        if not pgconn.feature_enabled("apply"):
            print("feat.apply выключен в Mini App — пропуск apply-similar")
            return
        # Проверяем, что процесс запущен в Docker контейнере
        import os
        from pathlib import Path
        
        # Проверяем наличие маркера Docker контейнера или переменной окружения из docker-compose
        is_docker = (
            Path("/.dockerenv").exists() or
            os.getenv("CONFIG_DIR") == "/app/config"
        )
        
        if not is_docker:
            logger.error(
                "Команда apply-similar должна запускаться только внутри Docker контейнера. "
                "Используйте: docker compose run -u docker hh_applicant_tool hh-applicant-tool apply-similar"
            )
            print("❌ Ошибка: команда должна запускаться только в Docker контейнере!")
            print("💡 Используйте: docker compose run -u docker hh_applicant_tool hh-applicant-tool apply-similar")
            raise SystemExit(1)
        
        self.tool = tool
        self.api_client = tool.api_client
        args: Namespace = tool.args
        
        # Загружаем сохраненные настройки, если аргументы не указаны
        if not args.resume_id:
            args.resume_id = await tool.storage.settings.get_value("apply.resume_id") or None
        if args.use_ai is None:
            use_ai_value = await tool.storage.settings.get_value("apply.use_ai")
            if use_ai_value is not None:
                # Может быть bool или str
                if isinstance(use_ai_value, bool):
                    args.use_ai = use_ai_value
                else:
                    args.use_ai = str(use_ai_value).lower() in ("true", "1", "yes")
        if not args.force_message:
            force_value = await tool.storage.settings.get_value("apply.force_message")
            if force_value is not None:
                if isinstance(force_value, bool):
                    args.force_message = force_value
                else:
                    args.force_message = (
                        str(force_value).lower() in ("true", "1", "yes")
                    )
        self.application_messages = self._get_application_messages(
            args.message_list_path
        )
        self.area = args.area
        # Города для откликов — per-user настройка apply.area (CSV с id регионов hh).
        # ВАЖНО: параметр area у /similar_vacancies работает мягко (режет found, но items
        # всё равно приносит из других городов), поэтому в поиск его НЕ передаём, а режем
        # у себя — детерминированно, как стоп-слова. Пусто -> географию не ограничиваем.
        _area = await tool.storage.settings.get_value("apply.area")
        self.allowed_areas = {a.strip() for a in str(_area or "").split(",") if a.strip()}
        self.bottom_lat = args.bottom_lat
        self.currency = args.currency
        self.date_from = args.date_from
        self.date_to = args.date_to
        self.dry_run = args.dry_run
        self.employer_id = args.employer_id
        self.employment = args.employment
        self.excluded_employer_id = args.excluded_employer_id
        self.experience = args.experience
        self.force_message = args.force_message
        self.industry = args.industry
        self.label = args.label
        self.left_lng = args.left_lng
        self.metro = args.metro
        self.no_magic = args.no_magic
        self.only_with_salary = args.only_with_salary
        self.order_by = args.order_by
        self.per_page = args.per_page
        self.period = args.period
        self.pre_prompt = args.prompt
        self.premium = args.premium
        self.professional_role = args.professional_role
        self.resume_id = args.resume_id
        self.right_lng = args.right_lng
        self.salary = args.salary
        self.schedule = args.schedule
        self.search = args.search
        self.search_field = args.search_field
        # excluded_terms: из аргумента, иначе per-user из settings (apply.excluded_terms)
        _excl = args.excluded_terms or await tool.storage.settings.get_value(
            "apply.excluded_terms"
        )
        self.excluded_terms = self._parse_excluded_terms(_excl)
        # Стоп-слова в НАЗВАНИИ (отдельно от excluded_terms, который матчит и описание):
        # отсекаем по заголовку — напр. «преподаватель». Per-account из
        # apply.excluded_title_terms; пусто/не задано -> дефолтный список.
        _title = await tool.storage.settings.get_value("apply.excluded_title_terms")
        self.excluded_title_terms = (self._parse_excluded_terms(_title)
                                     if _title else list(DEFAULT_TITLE_STOP))
        # Общий формат работы кандидата -> мягкий client-side фильтр ниже: режем вакансию
        # ТОЛЬКО при ЯВНОМ несовпадении формата. Вакансии без указанного формата и без ЗП
        # НЕ отсекаем; зарплата выдачу не фильтрует вовсе (только письма/TG-матч).
        _prefs = (getattr(tool, "config", None) or {}).get("preferences") or {}
        self.wanted_wf = cprefs.wanted_formats(_prefs)
        self.sort_point_lat = args.sort_point_lat
        self.sort_point_lng = args.sort_point_lng
        self.top_lat = args.top_lat
        self.total_pages = args.total_pages
        self.openai_chat = (
            tool.get_openai_chat(args.first_prompt) if args.use_ai else None
        )
        # Фильтр по профилю резюме (apply.fit_check, по умолчанию ВКЛ). Отказы кэшируем в
        # seen_keys('fit_no'), чтобы не спрашивать LLM про ту же вакансию каждый час.
        _fit =await tool.storage.settings.get_value("apply.fit_check")
        fit_on = str(_fit).lower() not in ("false", "0", "no") if _fit is not None else True
        _oa = tool.config.get("openai") or {}
        self.fit_chat = (
            ChatOpenAI(token=_oa["token"], model=_oa.get("model"),
                       completion_endpoint=_oa.get("completion_endpoint"),
                       system_prompt=FIT_SYS, temperature=0, max_completion_tokens=8)
            if (args.use_ai and fit_on and _oa.get("token")) else None
        )
        self._fit_no = pgconn.seen_keys("fit_no") if self.fit_chat else set()
        # Источник «поиск по запросам» (apply.search_source, по умолчанию ВКЛ): запросы по
        # резюме собирает LLM один раз на версию профиля и хранит в apply.search_queries.
        _src = await tool.storage.settings.get_value("apply.search_source")
        self.search_source = (str(_src).lower() not in ("false", "0", "no")
                              if _src is not None else True)
        self.query_chat = (
            ChatOpenAI(token=_oa["token"], model=_oa.get("model"),
                       completion_endpoint=_oa.get("completion_endpoint"),
                       system_prompt=sq.QUERY_SYS, temperature=0.2, max_completion_tokens=200)
            if (args.use_ai and self.search_source and _oa.get("token")) else None
        )
        self._applied_titles: set[tuple[str, str]] = set()  # (employer_id, название) за прогон
        self._letter_ai_errors = 0
        # Для ответов на требования работодателя в письме (если вакансия спрашивает ЗП/контакт)
        self._pref_salary = ((tool.config.get("preferences") or {}).get("salary") or "")
        self._tg_username = ((await tool.storage.settings.get_value("tg_username")) or "").strip()
        await asyncio.to_thread(_ensure_hh_apps_columns)
        # Дневной лимит откликов — per-user из settings (apply.max_per_day), дефолт 100
        mpd = await tool.storage.settings.get_value("apply.max_per_day")
        try:
            self.max_applications_per_day = int(mpd) if mpd is not None else 100
        except (TypeError, ValueError):
            self.max_applications_per_day = 100
        # Мягкий дедлайн прогона — чтобы не ловить жёсткий kill оркестратора (см. константу)
        self._run_deadline = time.monotonic() + APPLY_MAX_RUNTIME_SEC
        # Только вакансии по договору ГПХ (поле civil_law_contracts непустое)
        self.civil_law_only = bool(
            await tool.storage.settings.get_value("apply.civil_law_only", False)
        )
        await self._init_daily_counter()
        await self._apply_similar()

    async def _init_daily_counter(self) -> None:
        """Инициализирует счетчик откликов за день."""
        today = date.today().isoformat()
        pause_until = await self.tool.storage.settings.get_value(
            "_applications_pause_until", ""
        )
        if pause_until:
            # Если еще действует пауза - останавливаем рассылку
            if pause_until > today:
                count_str = await self.tool.storage.settings.get_value("_applications_count", "0")
                self.applications_count = int(count_str) if count_str.isdigit() else 0
                self.daily_limit_reached = True
                logger.info(
                    "Рассылка на паузе до %s из-за лимита откликов.",
                    pause_until,
                )
                return
            # Пауза истекла - очищаем флаг
            await self.tool.storage.settings.set_value("_applications_pause_until", "")
        last_date = await self.tool.storage.settings.get_value("_applications_date", "")

        if last_date != today:
            # Новая дата - сбрасываем счетчик
            await self.tool.storage.settings.set_value("_applications_date", today)
            await self.tool.storage.settings.set_value("_applications_count", "0")
            self.applications_count = 0
        else:
            # Та же дата - загружаем счетчик из базы
            count_str = await self.tool.storage.settings.get_value("_applications_count", "0")
            self.applications_count = int(count_str) if count_str.isdigit() else 0

        self.daily_limit_reached = (
            self.applications_count >= self.max_applications_per_day
        )

    async def _pause_until_next_day(self) -> None:
        pause_until = (date.today() + timedelta(days=1)).isoformat()
        await self.tool.storage.settings.set_value(
            "_applications_pause_until", pause_until
        )
        self.daily_limit_reached = True
        logger.info("Рассылка остановлена до %s из-за лимита откликов.", pause_until)

    async def _apply_similar(self) -> None:
        if self.daily_limit_reached:
            logger.info(
                "Лимит откликов за день достигнут (%s/%s). "
                "Повторный запуск будет возможен завтра.",
                self.applications_count,
                self.max_applications_per_day,
            )
            print(
                "⏸️ Лимит откликов за день достигнут. "
                "Запуск автоматически возобновится завтра."
            )
            return

        resumes: list[datatypes.Resume] = await self.tool.get_resumes()
        try:
            await self.tool.storage.resumes.save_batch(resumes)
        except RepositoryError as ex:
            logger.exception(ex)
        resumes = (
            list(filter(lambda x: x["id"] == self.resume_id, resumes))
            if self.resume_id
            else resumes
        )
        # Выбираем только опубликованные
        resumes = list(
            filter(lambda x: x["status"]["id"] == "published", resumes)
        )
        if not resumes:
            logger.warning("У вас нет опубликованных резюме")
            return

        me: datatypes.User = await self.tool.get_me()
        seen_employers = set()

        for resume in resumes:
            await self._apply_resume(
                resume=resume,
                user=me,
                seen_employers=seen_employers,
            )

        # Синхронизация откликов
        # for neg in self.tool.get_negotiations():
        #     try:
        #         self.tool.storage.negotiations.save(neg)
        #     except RepositoryError as e:
        #         logger.warning(e)

        # LLM лежит -> письма молча уходят шаблоном (так было весь простой 19.08–18.09).
        if self._letter_ai_errors >= 3 and not self.dry_run:
            from ..storage import pgconn
            pgconn.notify_admins(  # инфраструктура — только владельцу системы
                pgconn.PRIORITY_HIGH,
                f"LLM недоступна — {self._letter_ai_errors} сопроводительных ушли шаблоном. "
                "Проверь локальную LLM.",
                dedup_key=f"llm_down_apply:{date.today().isoformat()}",
            )

        print("📝 Отклики на вакансии разосланы!")

    async def _apply_resume(
        self,
        resume: datatypes.Resume,
        user: datatypes.User,
        seen_employers: set[str],
    ) -> None:
        if time.monotonic() > self._run_deadline:
            logger.info("apply: тайм-бюджет прогона исчерпан — резюме «%s» пропускаю", resume.get("title"))
            return
        if self.applications_count >= self.max_applications_per_day:
            logger.info("apply: дневной лимит уже достигнут (%d/%d) — резюме «%s» пропускаю",
                        self.applications_count, self.max_applications_per_day, resume.get("title"))
            self.daily_limit_reached = True
            return
        logger.info("Начинаю рассылку откликов для резюме: %s (%s)", resume["alternate_url"], resume["title"])
        print("🚀 Начинаю рассылку откликов для резюме:", resume["title"])

        # Получаем полное резюме с опытом, навыками и образованием
        try:
            full_resume = await self.api_client.get(f"/resumes/{resume['id']}")
        except Exception as ex:
            logger.warning(f"Не удалось получить полное резюме через API: {ex}. Используется fallback из файла.")
            full_resume = {}
        
        # Резюме-текст из PG-конфига (раньше был файл resume.txt) как доп. контекст
        resume_file_content = self.tool.config.get("resume_text") or None
        if resume_file_content:
            logger.debug(
                "Загружено резюме из PG (%d символов)",
                len(resume_file_content),
            )
        
        placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "middle_name": user.get("middle_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
            "resume_title": resume.get("title") or "",
        }
        
        # Сохраняем полное резюме и содержимое файла для использования в промпте
        self._full_resume = full_resume
        self._resume_file_content = resume_file_content
        self._profile_text = self._build_profile(resume, full_resume, resume_file_content)

        do_apply = True

        async for vacancy, source in self._iter_candidates(resume):
            # Дневной лимит проверяем НА КАЖДОМ отклике, а не только на входе в прогон:
            # иначе один прогон выгребал вакансии до упора (у пользователя было 115 при лимите 15).
            if self.applications_count >= self.max_applications_per_day:
                logger.info("apply: дневной лимит откликов достигнут (%d/%d) — останавливаюсь",
                            self.applications_count, self.max_applications_per_day)
                print(f"⏸️ Дневной лимит откликов достигнут "
                      f"({self.applications_count}/{self.max_applications_per_day}) — останавливаюсь")
                self.daily_limit_reached = True
                break

            if time.monotonic() > self._run_deadline:
                logger.info("apply: тайм-бюджет прогона (%dс) исчерпан на %d откликах — "
                            "останавливаюсь чисто, остаток добьётся следующим часовым прогоном",
                            APPLY_MAX_RUNTIME_SEC, self.applications_count)
                print(f"⏳ Тайм-бюджет прогона исчерпан (сделано {self.applications_count}) — останавливаюсь чисто")
                break

            try:
                employer = vacancy.get("employer", {})
                
                # Реквизиты из snippet — они уже есть в выдаче поиска, без доп.
                # запроса. Полное описание (GET /vacancies/{id}) тянем ЛЕНИВО, ниже
                # внутри блока AI-письма — только для вакансий, прошедших ВСЕ фильтры
                # и реально требующих письма (#21). Раньше этот запрос делался для
                # каждой вакансии, в т.ч. пропущенной (has_test/archived/relations/…).
                vacancy_description = ""
                _reqs: list[str] = []          # требования работодателя к отклику
                _codeword: str | None = None   # кодовое слово, которое просят указать
                vacancy_requirements = ""
                vacancy_responsibilities = ""
                if vacancy.get("snippet"):
                    snippet = vacancy["snippet"]
                    if snippet.get("requirement"):
                        vacancy_requirements = snippet["requirement"][:500]
                    if snippet.get("responsibility"):
                        vacancy_responsibilities = snippet["responsibility"][:500]

                message_placeholders = {
                    "vacancy_name": vacancy.get("name", ""),
                    "employer_name": employer.get("name", ""),
                    "vacancy_description": vacancy_description,
                    "vacancy_requirements": vacancy_requirements,
                    "vacancy_responsibilities": vacancy_responsibilities,
                    **placeholders,
                }

                storage = self.tool.storage

                try:
                    await storage.vacancies.save(vacancy)
                except RepositoryError as ex:
                    logger.debug(ex)

                # По факту контакты можно получить только здесь?!
                if vacancy.get("contacts"):
                    logger.debug(
                        f"Найдены контакты в вакансии: {vacancy['alternate_url']}"
                    )

                    try:
                        # logger.debug(vacancy)
                        await storage.vacancy_contacts.save(vacancy)
                    except RepositoryError as ex:
                        logger.exception(ex)

                    employer_id = employer.get("id")
                    if employer_id and employer_id not in seen_employers:
                        employer_profile: datatypes.Employer = (
                            await self.api_client.get(f"/employers/{employer_id}")
                        )

                        try:
                            await storage.employers.save(employer_profile)
                        except RepositoryError as ex:
                            logger.exception(ex)

                if not do_apply:
                    continue

                if vacancy.get("has_test"):
                    logger.debug(
                        "Пропускаем вакансию с тестом: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                if vacancy.get("archived"):
                    logger.debug(
                        "Пропускаем вакансию в архиве: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                if redirect_url := vacancy.get("response_url"):
                    logger.debug(
                        "Пропускаем вакансию %s с перенаправлением: %s",
                        vacancy["alternate_url"],
                        redirect_url,
                    )
                    continue

                # Фильтр «только ГПХ»: пропускаем вакансии без договора ГПХ
                if self.civil_law_only and not vacancy.get("civil_law_contracts"):
                    logger.debug(
                        "Пропускаем не-ГПХ вакансию: %s", vacancy["alternate_url"]
                    )
                    continue

                # Жёсткий фильтр формата работы (общая настройка кандидата): пропускаем,
                # только если у вакансии формат УКАЗАН и не пересекается с выбранным.
                if self.wanted_wf:
                    _vf = [w.get("id") for w in (vacancy.get("work_format") or [])]
                    if (_sch := (vacancy.get("schedule") or {}).get("id")):
                        _vf.append(_sch)
                    if not cprefs.format_ok(_vf, self.wanted_wf):
                        logger.debug(
                            "Пропускаем по формату работы: %s", vacancy["alternate_url"]
                        )
                        continue

                vacancy_id = vacancy["id"]

                relations = vacancy.get("relations", [])

                if relations:
                    logger.debug(
                        "Пропускаем вакансию с откликом: %s",
                        vacancy["alternate_url"],
                    )
                    if "got_rejection" in relations:
                        logger.debug(
                            "Вы получили отказ от %s",
                            vacancy["alternate_url"],
                        )
                        print("⛔ Пришел отказ от", vacancy["alternate_url"])
                    continue

                if self.allowed_areas:
                    _a = vacancy.get("area") or {}
                    if str(_a.get("id")) not in self.allowed_areas:
                        logger.warning("Пропуск по городу (%s): %s", _a.get("name"),
                                       vacancy.get("name") or vacancy["alternate_url"])
                        continue

                if self._is_title_excluded(vacancy):
                    logger.warning("Пропуск по стоп-слову в названии: %s",
                                   vacancy.get("name") or vacancy["alternate_url"])
                    continue

                if self._is_excluded(vacancy):
                    logger.warning("Вакансия содержит недопустимые словосочетания: %s",vacancy["alternate_url"])
                    continue

                _emp_id = str(employer.get("id") or "")
                _title_key = (_emp_id, _norm_title(vacancy.get("name") or ""))
                if _emp_id and (
                    _title_key in self._applied_titles
                    or await asyncio.to_thread(
                        _already_applied_same, _emp_id, _title_key[1], vacancy_id)
                ):
                    logger.warning("Пропуск дубля: уже откликались на «%s» (%s)",
                                   vacancy.get("name"), employer.get("name"))
                    continue

                if not await self._fits_profile(vacancy):
                    continue

                params = {
                    "resume_id": resume["id"],
                    "vacancy_id": vacancy_id,
                    "message": "",
                }

                logger.debug(
                    "force_message=%s, response_letter_required=%s, openai_chat=%s for vacancy %s",
                    self.force_message,
                    vacancy.get("response_letter_required"),
                    bool(self.openai_chat),
                    vacancy_id,
                )

                if self.force_message or vacancy.get(
                    "response_letter_required"
                ):
                    if self.openai_chat:
                        # Ленивая загрузка полного описания вакансии (#21): только
                        # здесь оно реально нужно — для качественного AI-письма.
                        try:
                            full_vacancy = await self.api_client.get(
                                f"/vacancies/{vacancy['id']}"
                            )
                            # Что просят сделать в отклике (вопросы, кодовое слово) — из ПОЛНОГО
                            # описания: такие просьбы обычно в конце, а в промпт идёт 2000 знаков.
                            _reqs = cl.extract_requirements(
                                cl.description_lines(full_vacancy.get("description") or ""))
                            _codeword = cl.extract_codeword(_reqs)
                            if full_vacancy.get("description"):
                                desc = re.sub(
                                    r"<[^>]+>", "", full_vacancy["description"]
                                )
                                desc = (
                                    desc.replace("&nbsp;", " ")
                                    .replace("&amp;", "&")
                                    .replace("&lt;", "<")
                                    .replace("&gt;", ">")
                                )
                                message_placeholders["vacancy_description"] = (
                                    desc[:2000] + "..."
                                    if len(desc) > 2000
                                    else desc
                                )
                        except Exception as ex:
                            logger.debug(
                                "Не удалось получить полное описание вакансии %s: %s",
                                vacancy.get("id"),
                                ex,
                            )

                        # Формируем полное имя пользователя (Фамилия Имя Отчество)
                        full_name_parts = []
                        if message_placeholders.get("last_name"):
                            full_name_parts.append(message_placeholders["last_name"])
                        if message_placeholders.get("first_name"):
                            full_name_parts.append(message_placeholders["first_name"])
                        if message_placeholders.get("middle_name"):
                            full_name_parts.append(message_placeholders["middle_name"])
                        full_name = " ".join(full_name_parts) if full_name_parts else ""
                        
                        # Формируем описание опыта работы
                        experience_text = ""
                        if self._full_resume.get("experience"):
                            exp_items = []
                            for exp in self._full_resume["experience"]:
                                exp_str = f"- {exp.get('position', '')} в {exp.get('company', '')}"
                                if exp.get("start"):
                                    exp_str += f" ({exp.get('start', '')}"
                                    if exp.get("end"):
                                        exp_str += f" - {exp.get('end', '')})"
                                    else:
                                        exp_str += " - настоящее время)"
                                if exp.get("description"):
                                    desc = exp.get("description", "").strip()
                                    if desc:
                                        # Ограничиваем длину описания
                                        if len(desc) > 500:
                                            desc = desc[:500] + "..."
                                        exp_str += f"\n  {desc}"
                                exp_items.append(exp_str)
                            if exp_items:
                                experience_text = "Опыт работы:\n" + "\n".join(exp_items)
                        
                        # Формируем навыки
                        skills_text = ""
                        if self._full_resume.get("skills"):
                            skills = self._full_resume["skills"]
                            if isinstance(skills, str) and skills.strip():
                                skills_text = f"Навыки: {skills}"
                            elif isinstance(skills, list):
                                skills_text = f"Навыки: {', '.join(skills)}"
                        
                        # Формируем образование
                        education_text = ""
                        if self._full_resume.get("education"):
                            edu = self._full_resume["education"]
                            edu_parts = []
                            if edu.get("primary"):
                                primary = edu["primary"]
                                if isinstance(primary, list) and primary:
                                    primary = primary[0]
                                if isinstance(primary, dict):
                                    org = primary.get("organization", "")
                                    name = primary.get("name", "")
                                    year = primary.get("year", "")
                                    if org or name:
                                        edu_str = f"Образование: {name}"
                                        if org:
                                            edu_str += f" ({org})"
                                        if year:
                                            edu_str += f", {year}"
                                        edu_parts.append(edu_str)
                            if edu_parts:
                                education_text = "\n".join(edu_parts)
                        
                        # Используем файл резюме как fallback, если данных из API недостаточно
                        resume_file_text = ""
                        if self._resume_file_content:
                            # Если нет опыта работы из API или он пустой, используем файл как основной источник
                            if not experience_text and not skills_text and not education_text:
                                resume_file_text = f"\nПолное резюме:\n{self._resume_file_content}"
                                logger.info("Используется полное резюме из файла как fallback (данные из API недоступны)")
                            # Если есть данные из API, но файл содержит больше информации, добавляем его
                            elif len(self._resume_file_content) > 1000:
                                # Ограничиваем длину, если файл слишком большой
                                resume_file_text = f"\nДополнительная информация из резюме:\n{self._resume_file_content[:3000]}..."
                                logger.debug("Добавлена дополнительная информация из файла резюме")
                        
                        msg = self.pre_prompt + "\n\n"
                        msg += f"Название вакансии: {message_placeholders['vacancy_name']}\n"
                        if message_placeholders.get('employer_name'):
                            msg += f"Компания: {message_placeholders['employer_name']}\n"
                        
                        # Добавляем описание вакансии
                        if message_placeholders.get('vacancy_description'):
                            msg += f"\nОписание вакансии:\n{message_placeholders['vacancy_description']}\n"
                        elif message_placeholders.get('vacancy_requirements') or message_placeholders.get('vacancy_responsibilities'):
                            msg += "\nИнформация о вакансии:\n"
                            if message_placeholders.get('vacancy_responsibilities'):
                                msg += f"Обязанности: {message_placeholders['vacancy_responsibilities']}\n"
                            if message_placeholders.get('vacancy_requirements'):
                                msg += f"Требования: {message_placeholders['vacancy_requirements']}\n"
                        
                        msg += f"\nНазвание моего резюме: {message_placeholders['resume_title']}\n"
                        # Подпись — только имя: «Рябов Семен Александрович» под письмом звучит
                        # как канцелярия (и hh всё равно показывает ФИО в отклике).
                        if message_placeholders.get("first_name"):
                            msg += f"Имя для подписи: {message_placeholders['first_name']}\n"
                        elif full_name:
                            msg += f"Имя для подписи: {full_name}\n"
                        # Город — из hh-резюме (area.name), уже есть в self._full_resume
                        # (без доп. запроса). Чтобы письмо не привязывало кандидата к
                        # городу вуза из resume_text (напр. Волгоград вместо Москвы).
                        _city = (self._full_resume.get("area") or {}).get("name")
                        if _city:
                            msg += f"Мой город: {_city}\n"
                        if experience_text:
                            msg += f"\n{experience_text}\n"
                        if skills_text:
                            msg += f"{skills_text}\n"
                        if education_text:
                            msg += f"{education_text}\n"
                        if resume_file_text:
                            msg += resume_file_text
                        if _reqs:
                            # Работодатель просит что-то в отклике — даём данные для ответа
                            # (ЗП/контакт — только здесь, в обычное письмо их не пишем).
                            msg += cl.requirements_block(_reqs)
                            if self._pref_salary:
                                msg += f"\nМои зарплатные ожидания: {self._pref_salary}"
                            if self._tg_username:
                                msg += f"\nМой Telegram: {self._tg_username}"
                            if _codeword:
                                msg += (f"\nКодовое слово: «{_codeword[0]}» — "
                                        + ("начни письмо ровно с него." if _codeword[1]
                                           else "вставь его дословно."))

                        logger.debug("Full name in prompt: %s", full_name)
                        logger.debug("prompt length: %d chars", len(msg))
                        try:
                            _prompt = msg
                            msg = await self.openai_chat.send_message(_prompt)
                            # LLM всё равно скатывается в шаблон «Меня заинтересовала… мне
                            # близок…» — одна перегенерация с явным указанием, что поправить.
                            if (_bad := _letter_cliches(msg)):
                                logger.debug("письмо с клише %s — перегенерирую", _bad)
                                _retry = await self.openai_chat.send_message(
                                    _prompt + "\n\nНЕ используй: " + ", ".join(_bad)
                                    + ". Начни сразу с факта из опыта.")
                                if len(_letter_cliches(_retry)) < len(_bad):
                                    msg = _retry
                            msg = cl.ensure_codeword(msg, _codeword)
                            if _reqs:
                                logger.info("Письмо с учётом требований работодателя (%d): %s",
                                            len(_reqs), vacancy.get("alternate_url"))
                        except AIError as ex:
                            self._letter_ai_errors += 1
                            logger.warning(
                                f"Ошибка при генерации письма через AI: {ex}. "
                                "Используется шаблонное сообщение."
                            )
                            # Fallback на шаблонное сообщение при ошибке AI
                            msg = (
                                rand_text(random.choice(self.application_messages))
                                % message_placeholders
                            )
                    else:
                        msg = unescape_string(
                            rand_text(random.choice(self.application_messages))
                            % message_placeholders
                        )

                    logger.debug(msg)
                    params["message"] = msg
                    # Ссылку на вакансию в hh-письмо НЕ добавляем: в hh-чате вакансия и так
                    # видна в шапке, а лишняя строка выглядит роботом. Ссылку прикладываем
                    # только в холодных ЛС в Telegram (tg_channels/auto_screen).

                try:
                    if not self.dry_run:
                        logger.debug(
                            "SENDING POST /negotiations: vacancy_id=%s, message_len=%d, message_preview=%.100s",
                            params.get("vacancy_id"),
                            len(params.get("message", "")),
                            params.get("message", "")[:100],
                        )
                        res = await self.api_client.post(
                            "/negotiations",
                            params,
                            delay=random.uniform(10, 15),
                        )
                        assert res == {}
                        self.applications_count += 1
                        self._applied_titles.add(_title_key)
                        # Сохраняем счетчик в базу данных
                        await self.tool.storage.settings.set_value("_applications_count", str(self.applications_count))
                        # Счётчик активности Mini App (best-effort; pgconn — этот метод
                        # ниже run(), поэтому импортируем локально, не из run-скоупа)
                        from ..storage import pgconn
                        pgconn.bump_activity("apply", 1)
                        # Per-application лог: связь отклик -> вакансия/резюме/письмо,
                        # чтобы измерять конверсию (AI-письмо vs шаблон, резюме -> собес).
                        # best-effort, не валит отклик при ошибке записи.
                        try:
                            _conn = pgconn.connect()
                            with _conn.cursor() as _cur:
                                _cur.execute(
                                    "INSERT INTO hh_apps(account, vacancy_id, resume_id, "
                                    "used_ai, letter_len, model, employer_id, title, source, reqs) "
                                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                                    "ON CONFLICT(account, vacancy_id) DO NOTHING",
                                    (pgconn.get_account(), int(vacancy_id),
                                     self.resume_id, bool(self.openai_chat),
                                     len(params.get("message", "")), "",
                                     _emp_id or None, vacancy.get("name") or None, source,
                                     len(_reqs)),
                                )
                            _conn.commit()
                            _conn.close()
                        except Exception as _ex:
                            logger.debug("hh_apps лог не записан: %r", _ex)
                        logger.debug(
                            "Откликнулись на %s с резюме %s (отклик #%d за сегодня)",
                            vacancy["alternate_url"],
                            resume["alternate_url"],
                            self.applications_count,
                        )
                    print(
                        "📨 Отправили отклик для резюме",
                        resume["alternate_url"],
                        "на вакансию",
                        vacancy["alternate_url"],
                        "(",
                        shorten(vacancy["name"]),
                        ")",
                    )
                except Redirect:
                    logger.warning(
                        f"Игнорирую перенаправление на форму: {vacancy['alternate_url']}"  # noqa: E501
                    )
            except LimitExceeded:
                logger.info("Достигли лимита на отклики для резюме: %s", resume["alternate_url"])
                print("⚠️ Достигли лимита рассылки для резюме", resume["alternate_url"])
                do_apply = False
                await self._pause_until_next_day()
                break
            except ApiError as ex:
                logger.warning(ex)
            except (BadResponse, AIError) as ex:
                logger.error(ex)

        logger.info("Закончили рассылку откликов для резюме: %s (%s)", resume["alternate_url"], resume["title"])
        print("✅️ Закончили рассылку откликов для резюме:", resume["title"])

    def _get_search_params(self, page: int) -> dict:
        params = {
            "page": page,
            "per_page": self.per_page,
        }
        if self.order_by:
            params |= {"order_by": self.order_by}
        if self.search:
            params["text"] = self.search
        if self.schedule:
            params["schedule"] = self.schedule
        if self.experience:
            params["experience"] = self.experience
        if self.currency:
            params["currency"] = self.currency
        if self.salary:  # только явный CLI --salary; ЗП из настроек выдачу НЕ режет
            params["salary"] = self.salary
        if self.period:
            params["period"] = self.period
        if self.date_from:
            params["date_from"] = self.date_from
        if self.date_to:
            params["date_to"] = self.date_to
        if self.top_lat:
            params["top_lat"] = self.top_lat
        if self.bottom_lat:
            params["bottom_lat"] = self.bottom_lat
        if self.left_lng:
            params["left_lng"] = self.left_lng
        if self.right_lng:
            params["right_lng"] = self.right_lng
        if self.sort_point_lat:
            params["sort_point_lat"] = self.sort_point_lat
        if self.sort_point_lng:
            params["sort_point_lng"] = self.sort_point_lng
        if self.search_field:
            params["search_field"] = list(self.search_field)
        if self.employment:
            params["employment"] = list(self.employment)
        if self.area:
            params["area"] = list(self.area)
        if self.metro:
            params["metro"] = list(self.metro)
        if self.professional_role:
            params["professional_role"] = list(self.professional_role)
        if self.industry:
            params["industry"] = list(self.industry)
        if self.employer_id:
            params["employer_id"] = list(self.employer_id)
        if self.excluded_employer_id:
            params["excluded_employer_id"] = list(self.excluded_employer_id)
        if self.label:
            params["label"] = list(self.label)
        if self.only_with_salary:
            params["only_with_salary"] = bool2str(self.only_with_salary)
        # if self.clusters:
        #     params["clusters"] = bool2str(self.clusters)
        if self.no_magic:
            params["no_magic"] = bool2str(self.no_magic)
        if self.premium:
            params["premium"] = bool2str(self.premium)
        # if self.responses_count_enabled is not None:
        #     params["responses_count_enabled"] = bool2str(self.responses_count_enabled)

        return params

    async def _iter_candidates(
        self, resume: datatypes.Resume
    ) -> AsyncIterator[tuple[SearchVacancy, str]]:
        """Кандидаты на отклик: сначала поиск по запросам (свежие первыми), затем похожие hh.
        Дубли по id между источниками отбрасываются."""
        seen: set[str] = set()
        if self.search_source:
            async for vacancy in self._get_search_vacancies():
                if str(vacancy["id"]) not in seen:
                    seen.add(str(vacancy["id"]))
                    yield vacancy, "search"
        async for vacancy in self._get_similar_vacancies(resume_id=resume["id"]):
            if str(vacancy["id"]) not in seen:
                seen.add(str(vacancy["id"]))
                yield vacancy, "similar"

    async def _get_search_vacancies(self) -> AsyncIterator[SearchVacancy]:
        """GET /vacancies по запросам пользователя: по названию, за SEARCH_PERIOD_DAYS, с его
        форматом работы и городами. Сбой LLM/поиска -> источник пропускается, не валит прогон."""
        fr = self._full_resume or {}
        key = sq.profile_key(
            fr.get("title") or "", fr.get("skill_set") or [],
            [e.get("position") for e in (fr.get("experience") or [])],
        )
        try:
            queries = await sq.ensure_queries(
                self.tool.storage.settings, self.query_chat, self._profile_text, key)
        except AIError as ex:
            logger.warning("Поисковые запросы не собраны (%s) — только похожие вакансии", ex)
            return
        # Запрос, который противоречит стоп-листу пользователя (напр. «бизнес-аналитик» при
        # исключённых бизнес-аналитиках), не ищем вовсе: генератор про стоп-лист не знает.
        _stop = [self._norm_words(t) for t in self.excluded_title_terms]
        dropped = [q for q in queries if any(t and t in self._norm_words(q) for t in _stop)]
        queries = [q for q in queries if q not in dropped]
        if dropped:
            logger.info("Запросы против стоп-слов пропущены: %s", dropped)
        if not queries:
            return
        base = {"search_field": "name", "period": SEARCH_PERIOD_DAYS,
                "order_by": "publication_time", "per_page": 100}
        if (wf := cprefs.hh_work_format_ids(self.wanted_wf)):
            base["work_format"] = wf
        if self.allowed_areas:
            base["area"] = sorted(self.allowed_areas)
        found: dict[str, SearchVacancy] = {}
        budget = SEARCH_MAX_REQUESTS
        # первая страница каждого запроса важнее вторых: при многих запросах — по одной
        pages = SEARCH_PAGES if len(queries) * SEARCH_PAGES <= budget else 1
        for q in queries:
            for page in range(pages):
                if budget <= 0:
                    break
                budget -= 1
                await asyncio.sleep(random.uniform(*SEARCH_PAUSE_SEC))
                try:
                    res = await self.api_client.get("/vacancies", {**base, "text": q, "page": page})
                except ApiError as ex:
                    logger.warning("Поиск «%s» не удался: %s", q, ex)
                    break
                for item in res.get("items") or []:
                    found.setdefault(str(item["id"]), item)
                if page + 1 >= (res.get("pages") or 0):
                    break
        items = sorted(found.values(), key=lambda v: v.get("published_at") or "", reverse=True)
        logger.info("Поиск по запросам %s: %d вакансий", queries, len(items))
        print(f"🔎 Поиск по запросам: {len(items)} вакансий ({'; '.join(queries)})")
        for item in items:
            yield item

    async def _get_similar_vacancies(
        self, resume_id: str
    ) -> AsyncIterator[SearchVacancy]:
        for page in range(self.total_pages):
            logger.debug(
                f"Загружаем подходящие вакансии со страницы: {page + 1}"
            )
            params = self._get_search_params(page)
            res: PaginatedItems[SearchVacancy] = await self.api_client.get(
                f"/resumes/{resume_id}/similar_vacancies",
                params,
            )

            logger.debug(f"Количество подходящих вакансий: {res['found']}")

            if not res["items"]:
                return

            for item in res["items"]:
                yield item

            if page >= res["pages"] - 1:
                return

    @staticmethod
    def _parse_excluded_terms(excluded_terms: str | None) -> list[str]:
        if not excluded_terms:
            return []
        return [
            x.strip() for x in excluded_terms.lower().split(",") if x.strip()
        ]

    @staticmethod
    def _build_profile(resume: dict, full_resume: dict, resume_text: str | None) -> str:
        """Короткий профиль кандидата для фильтра релевантности."""
        parts = [f"Резюме: {resume.get('title') or ''}"]
        skills = full_resume.get("skill_set") or full_resume.get("skills") or []
        if isinstance(skills, list) and skills:
            parts.append("Навыки: " + ", ".join(map(str, skills[:40])))
        elif isinstance(skills, str) and skills.strip():
            parts.append("Навыки: " + skills.strip()[:600])
        exp = [f"{e.get('position', '')} в {e.get('company', '')}"
               for e in (full_resume.get("experience") or [])[:4]]
        if exp:
            parts.append("Опыт: " + "; ".join(exp))
        # Детали опыта — из полного текста резюме: без них фильтр не видел, например, что
        # бэкендер работает с Asterisk/телефонией, и отсекал «VoIP / Asterisk Engineer».
        if resume_text:
            parts.append("Из резюме:\n" + " ".join(resume_text.split())[:1500])
        return "\n".join(parts)

    async def _fits_profile(self, vacancy: SearchVacancy) -> bool:
        """LLM-фильтр: вакансия по профессии/стеку кандидата? Сбой LLM -> пропускаем в отклик
        (fail-open: грубый мусор уже отсекли стоп-слова)."""
        if not self.fit_chat:
            return True
        vid = str(vacancy.get("id"))
        if vid in self._fit_no:
            return False
        snippet = vacancy.get("snippet") or {}
        roles = ", ".join(r.get("name", "") for r in vacancy.get("professional_roles") or [])
        query = (
            f"КАНДИДАТ:\n{self._profile_text}\n\n"
            f"ВАКАНСИЯ: {vacancy.get('name') or ''}\n"
            f"Компания: {(vacancy.get('employer') or {}).get('name') or ''}\n"
            + (f"Проф. роль: {roles}\n" if roles else "")
            + f"Требования: {_strip_tags(snippet.get('requirement'))[:400]}\n"
            f"Обязанности: {_strip_tags(snippet.get('responsibility'))[:400]}"
        )
        try:
            ans = (await self.fit_chat.send_message(query)).strip().upper()
        except AIError as ex:
            logger.debug("fit-check недоступен (%s) — пропускаю в отклик", ex)
            return True
        if ans.startswith(("НЕТ", "NO")):
            from ..storage import pgconn
            self._fit_no.add(vid)
            await asyncio.to_thread(pgconn.add_seen, "fit_no", vid)
            logger.warning("Пропуск: не по профилю резюме: %s (%s)", vacancy.get("name"),
                           (vacancy.get("employer") or {}).get("name"))
            return False
        return True

    @staticmethod
    def _norm_words(s: str) -> str:
        """«Бизнес-аналитик» == «бизнес аналитик»: дефисы как пробелы, регистр не важен."""
        return " ".join(re.sub(r"[-‐–—/]", " ", (s or "").lower()).split())

    def _is_title_excluded(self, vacancy: SearchVacancy) -> bool:
        """Стоп-слова ТОЛЬКО по названию вакансии (не по описанию)."""
        name = self._norm_words(vacancy.get("name") or "")
        return any(self._norm_words(t) in name for t in self.excluded_title_terms)

    def _is_excluded(self, vacancy: SearchVacancy) -> bool:
        snippet = vacancy.get("snippet") or {}
        combined = " ".join(
            [
                vacancy.get("name") or "",
                snippet.get("requirement") or "",
                snippet.get("responsibility") or "",
            ]
        ).lower()

        return any(v in combined for v in self.excluded_terms)

    def _get_application_messages(self, path: Path | None) -> list[str]:
        return (
            list(
                filter(
                    None,
                    map(
                        str.strip,
                        path.open(encoding="utf-8", errors="replace"),
                    ),
                )
            )
            if path
            else [
                "Здравствуйте, меня зовут %(first_name)s. {Меня заинтересовала|Мне понравилась} ваша вакансия «%(vacancy_name)s». Хотелось бы {пообщаться|задать вопросы} о ней.",
                "{Прошу|Предлагаю} рассмотреть {мою кандидатуру|мое резюме «%(resume_title)s»} на вакансию «%(vacancy_name)s». С уважением, %(first_name)s.",  # noqa: E501
            ]
        )
