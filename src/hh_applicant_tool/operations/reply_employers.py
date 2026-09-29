from __future__ import annotations

import argparse
import collections
import logging
import random
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

from ..ai.base import AIError
from ..api import ApiError, datatypes
from ..main import BaseNamespace, BaseOperation
from ..storage import pgconn
from ..utils.date import parse_api_datetime
from ..utils import dialog_rules as rules
from ..utils import prefs as cprefs
from ..utils.string import rand_text

# Классификатор хэндоффа: приглашение на ЖИВОЙ разговор с человеком -> человеку.
HANDOFF_SYS = (
    "Тебе дают ПОСЛЕДНЕЕ сообщение работодателя в чате на hh.ru. Ответь РОВНО одним "
    "словом ДА или НЕТ: приглашает ли работодатель кандидата на ЖИВОЙ разговор с "
    "ЧЕЛОВЕКОМ — собеседование/созвон/видеовстреча с сотрудником компании, или "
    "предлагает конкретное время для звонка/встречи с живым человеком?\n"
    "Отвечай НЕТ, если это: автоматический скрининг («пройти интервью с ботом-"
    "рекрутёром», «первичное интервью с ГигаРекрутером», интервью/тест ПО ССЫЛКЕ или "
    "в Telegram-боте); просьба заполнить анкету/тест; обычный вопрос про опыт/навыки/"
    "зарплату/формат; благодарность; «рассмотрим резюме»; отказ."
)

# Ключевые слова-префильтр: без них точно не приглашение (экономим LLM-вызов).
# Финальное решение всё равно за LLM (HANDOFF_SYS) — префильтр лишь пропускает
# кандидатов. Добавлены «голые» звонки/время: «перезвоните», «наберите», телефон,
# а также время вида 17:00 ловим отдельным regex (_TIME_RE).
_INVITE_KW = (
    "собеседован", "интервью", "созвон", "созвонимся", "созвониться", "звонок",
    "позвон", "перезвон", "наберите", "набрать вас", "свяжемся", "связаться с вами",
    "ваш телефон", "номер телефон", "встреч", "zoom", "зум", "teams", "тимс",
    "телемост", "видеосвяз", "видеозвон", "когда удоб", "удобное время",
    "во сколько", "в какое время", "будет удобно", "приглаша", "ждём вас", "ждем вас",
)

# Время вида 17:00 / 9.30 — часто = предложение времени созвона/встречи.
_TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")

# Маркеры АВТО-скрининга (бот/ссылка): это НЕ живой хэндофф — пусть notify_actions
# отдаст это как 🟡 (внешняя задача). Если есть в сообщении — не эскалируем.
_BOT_MARKERS = (
    "ботом-рекрут", "бот-рекрут", "гигарекрут", "giga", "t.me/", "telegram.me/",
    "_bot", "по ссылке", "перейдите по ссылк", "пройти по ссылк", "@",
)

DEFAULT_PERIOD_DAYS = 21  # чаты старше — не трогаем (сортировка по updated_at, дальше break)
STALE_DAYS = 3            # сообщение работодателя старше — поздно отвечать ботом (напр. после простоя LLM)
LLM_DOWN_MIN_ERRORS = 3   # столько AIError при 0 ответов LLM за прогон -> алерт «бот молчит»

# Контекст для LLM, когда чат ведёт авто-анкета hh.
_QUESTIONNAIRE_HINT = (
    "\n\nКОНТЕКСТ: это автоматическая анкета hh. Она принимает только короткий ответ. "
    "На закрытый вопрос ответь ОДНИМ словом «Да» или «Нет» без пояснений; на открытый — "
    "одной короткой фразой (число/факт). Нет ответа в резюме — выведи ASK."
)
# Повторная попытка, если черновик нарушил правила (обещание/созвон/скобки).
_FIX_HINT = (
    "\n\nТвой прошлый вариант нарушил правила (обещание за кандидата, предложение созвона "
    "или шаблон в скобках). Перепиши: ответь по существу из резюме, без обещаний и без "
    "созвона. Нельзя так ответить — выведи ASK."
)

# GUARD: LLM иногда выдаёт мета-рассуждение/варианты вместо самого ответа — такое не шлём.
_META = (
    "отвечать не требуется", "не требует ответа", "отвечать не нужно",
    "можно не отвечать", "можно написать", "можно ответить",
    "вы можете написать", "достаточно написать", "переписка завершена",
    "переписка фактически", "в данной ситуации", "в этой ситуации",
    "в качестве ассистента", "как ассистент", "я ассистент",
    "как ии", "я не могу", "следующий шаг",
)
# GUARD: контакт-плейсхолдеры («@username (замените на ваш ник)») — шаблон-заглушка.
_PLACEHOLDER = (
    "@username", "замените на", "замени на", "реальный ник", "ваш ник",
    "ваш реальный", "укажите ваш", "укажите свой", "впишите", "вставьте ваш",
    "your_username", "your username", "[ваш", "<ваш", "[имя", "<имя", "вашник",
)

def _prune_seen(kind: str, days: int) -> None:
    """Кэш «чат уже обработан при этом updated_at» нужен только в окне period — старое чистим."""
    try:
        conn = pgconn.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seen_keys WHERE account=%s AND kind=%s AND "
                            "created_at < now() - make_interval(days => %s)",
                            (pgconn.get_account(), kind, days))
            conn.commit()
        finally:
            conn.close()
    except Exception as ex:
        logger.debug("seen_keys prune: %r", ex)


if TYPE_CHECKING:
    from ..main import HHApplicantTool


try:
    import readline

    readline.add_history("/cancel ")
    readline.add_history("/ban")
    readline.set_history_length(10_000)
except ImportError:
    pass


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    reply_message: str
    max_pages: int
    only_invitations: bool
    dry_run: bool
    use_ai: bool
    first_prompt: str
    prompt: str
    period: int


class Operation(BaseOperation):
    """Ответ всем работодателям."""

    __aliases__ = ["reply-empls", "reply-chats", "reall"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--resume-id",
            help="Идентификатор резюме. Если не указан, то просматриваем чаты для всех резюме",
        )
        parser.add_argument(
            "-m",
            "--reply-message",
            "--reply",
            help="Отправить сообщение во все чаты. Если не передать сообщение, то нужно будет вводить его в интерактивном режиме.",  # noqa: E501
        )
        parser.add_argument(
            "--period",
            type=int,
            help="Игнорировать отклики, которые не обновлялись больше N дней",
        )
        parser.add_argument(
            "-p",
            "--max-pages",
            type=int,
            default=25,
            help="Максимальное количество страниц для проверки",
        )
        parser.add_argument(
            "-oi",
            "--only-invitations",
            help="Отвечать только на приглашения",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--dry-run",
            "--dry",
            help="Не отправлять сообщения, а только выводить параметры запроса",
            default=False,
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--use-ai",
            "--ai",
            help="Использовать AI для автоматической генерации ответов",
            action=argparse.BooleanOptionalAction,
        )
        parser.add_argument(
            "--first-prompt",
            help="Начальный промпт чата для AI",
            default=(
                "Ты ведёшь переписку с работодателем на hh.ru от лица соискателя (кандидата), от первого "
                "лица. Пиши как живой человек в мессенджере: вежливо, коротко (1–3 предложения), по делу, "
                "обычными словами, без канцелярита и бузвордов. Не упоминай, что ты ИИ.\n"
                "ГЛАВНОЕ — ОТВЕЧАЙ ПО СУЩЕСТВУ:\n"
                "- На вопрос об опыте/навыках/стеке отвечай прямо и конкретно, строго по фактам из резюме "
                "ниже. Закрытый вопрос (да/нет) начинай со слова «Да» или «Нет», дальше максимум одна "
                "короткая фраза-пояснение.\n"
                "- Если нужного опыта в резюме нет — честно скажи «Нет» (или «коммерческого опыта с X нет») "
                "и, если уместно, одной фразой назови близкий опыт из резюме. Не уходи от ответа.\n"
                "- НЕ предлагай созвон/звонок/встречу и не пиши «обсудим на созвоне», если работодатель "
                "сам не зовёт на разговор.\n"
                "- Отвечай на языке вопроса: вопрос по-английски — ответ по-английски.\n"
                "ЗАПРЕТЫ:\n"
                "- Не называй и не подтверждай дату/время/место встречи — кандидат договаривается сам.\n"
                "- Не бери обязательств за кандидата: никаких «напишу / свяжусь / пришлю / заполню / "
                "пройду / изучу / ознакомлюсь / подготовлю / вернусь / посмотрю / согласую». Если просят "
                "что-то сделать вне чата (анкета, тест, ТЗ, написать в Telegram/позвонить) — это сделает "
                "кандидат сам, ты на такое сообщение не отвечаешь (SKIP).\n"
                "- Не выдумывай факты, которых нет в резюме и данных ниже. Не оставляй шаблоны в скобках "
                "([X], [количество], <имя>).\n"
                "- Не отказывайся от вакансии и не пиши «неактуально/не интересно».\n"
                "КОГДА НЕ ПИСАТЬ ТЕКСТ, А ВЫВЕСТИ ОДНО СЛОВО:\n"
                "- SKIP — ответ не нужен: автоуведомление, благодарность/«ок», «рассмотрим резюме, "
                "свяжемся», отказ, просьба перейти по ссылке / в Telegram / заполнить анкету / пройти "
                "тест / изучить ТЗ, переписка завершена или ушла в другой канал.\n"
                "- ASK — ответить может только сам кандидат: условия оформления (самозанятость/ИП/ГПХ/"
                "налоги), работа без оплаты или за долю, военная/оборонная тематика, политика, личные "
                "обстоятельства, переезд, график/офис/объём времени, если их нет в данных ниже, стоимость "
                "и сроки работ, любой вопрос о решении кандидата, ответа на который нет в резюме и данных."
            ),
        )
        parser.add_argument(
            "--prompt",
            help="Промпт для генерации сообщения",
            default=(
                "Сформулируй ответ на ПОСЛЕДНЕЕ сообщение работодателя в этой переписке. "
                "Выведи РОВНО готовый текст сообщения от лица кандидата — и больше НИЧЕГО: "
                "без пояснений, рассуждений, вариантов на выбор и кавычек-ёлочек. "
                "Если отвечать не нужно — выведи РОВНО SKIP. Если ответить может только сам "
                "кандидат — выведи РОВНО ASK."
            ),
        )

    async def run(self, tool: HHApplicantTool) -> None:
        from ..storage import pgconn
        if not pgconn.feature_enabled("reply"):
            print("feat.reply выключен в Mini App — пропуск reply-employers")
            return
        args: Namespace = tool.args
        self.tool = tool
        self.api_client = tool.api_client
        # Отвечаем в чатах ТОГО ЖЕ резюме, под которым откликаемся (apply.resume_id из
        # кабинета), а не "первого попавшегося" first_resume_id(): иначе чаты с откликами
        # под другим резюме отсеиваются (resume_map.get -> None) и бот молчит.
        # (Баг Никиты: 496/500 негоциаций под apply-резюме, reply смотрел first -> 0 ответов.)
        apply_rid = await tool.storage.settings.get_value("apply.resume_id")
        self.resume_id = apply_rid or await tool.first_resume_id()
        self.reply_message = args.reply_message or tool.config.get(
            "reply_message"
        )
        self.max_pages = args.max_pages
        self.dry_run = args.dry_run
        self.only_invitations = args.only_invitations

        self.pre_prompt = args.prompt
        # Заземляем ответы на резюме кандидата (как в apply-similar),
        # чтобы AI отвечал по фактам, а не выдумывал.
        system_prompt = args.first_prompt
        candidate_name = ""
        resume_text = (self.tool.config.get("resume_text") or "").strip()
        if resume_text:
            candidate_name = resume_text.split("\n", 1)[0].strip()
            system_prompt += (
                "\n\nРезюме кандидата (опирайся только на эти факты):\n"
                + resume_text
            )
        salary = (self.tool.config.get("preferences") or {}).get("salary")
        if salary:
            system_prompt += (
                f"\n\nЗарплатные ожидания кандидата: {salary}. "
                "Если работодатель спрашивает про зарплату/ожидания — называй именно эту сумму."
            )
        # Город — из hh-резюме (area.name), а не угадывать по resume_text: там может
        # не быть текущего города, и AI брал его из строки про вуз и отвечал неверно
        # (напр. «Волгоград», когда кандидат в Москве).
        resume_obj = {}
        try:
            resume_obj = await self.api_client.get(f"/resumes/{self.resume_id}")
        except Exception as ex:
            logger.debug("резюме не получено: %r", ex)
        city = (resume_obj.get("area") or {}).get("name")
        if city:
            system_prompt += (
                f"\n\nГород проживания кандидата: {city}. На вопросы о городе/локации "
                "указывай именно этот город (не выдумывай другой по строке про вуз). "
                "Кандидат физически находится в этом городе."
            )
        # Формат работы: приоритет — общая настройка кандидата (preferences.work_format),
        # иначе из hh-резюме (удалёнка > гибрид > офис).
        _wanted = cprefs.wanted_formats(self.tool.config.get("preferences"))
        if _wanted:
            _wf = cprefs.labels_ru(_wanted)
        else:
            _wf_order = {"REMOTE": 0, "HYBRID": 1, "ON_SITE": 2,
                         "FIELD_WORK": 3, "FLY_IN_FLY_OUT": 4}
            _wf = ", ".join(
                w["name"]
                for w in sorted(
                    (w for w in (resume_obj.get("work_format") or []) if w.get("name")),
                    key=lambda w: _wf_order.get(w.get("id"), 9),
                )
            )
        if _wf:
            system_prompt += (
                f"\n\nФорматы работы, которые подходят кандидату (в порядке приоритета): {_wf}. "
                "Если спрашивают про формат работы / что удобнее — называй именно эти форматы; самый "
                "приоритетный (первый) указывай как предпочтительный, остальные — как тоже приемлемые. "
                "Форматы, которых нет в списке, не называй."
            )
        tg_username = (await self.tool.storage.settings.get_value("tg_username") or "").strip()
        if tg_username:
            system_prompt += (
                f"\n\nКОНТАКТ ДЛЯ СВЯЗИ: твой реальный ник в Telegram — {tg_username}. Если работодатель "
                f"просит контакт/Telegram/связаться вне hh — дай именно {tg_username}. НИКОГДА не пиши "
                "плейсхолдеры-заглушки вроде «@username», «(замените на ваш ник)», «ваш реальный ник», "
                "«укажите контакт», «[ваш ник]» — только реальный ник выше."
            )
        else:
            system_prompt += (
                "\n\nКОНТАКТ: у тебя НЕТ ника/телефона для передачи. Если работодатель просит "
                "Telegram/телефон/связаться вне hh — НЕ выдумывай контакт и НЕ пиши плейсхолдеры-заглушки; "
                "вежливо предложи продолжить общение здесь, в чате hh."
            )
        if candidate_name:
            system_prompt += (
                f"\n\nВАЖНО ПРО ИМЕНА: тебя (кандидата) зовут {candidate_name} — это ТЫ, а НЕ собеседник. "
                "Обращайся к работодателю по имени ТОЛЬКО если он САМ представился или подписался им именно в ЭТОЙ "
                "переписке (например «С уважением, Анна» или «Меня зовут Пётр»). Если имя собеседника в переписке прямо "
                "не названо — пиши просто «Здравствуйте!» без имени, НЕ придумывай и НЕ угадывай имя. "
                "НИКОГДА не обращайся к собеседнику по имени кандидата."
            )
            # per-user список «мусорных» имён из старых сообщений (settings reply.ignore_names)
            ignore_names = await self.tool.storage.settings.get_value(
                "reply.ignore_names"
            )
            if ignore_names:
                system_prompt += (
                    f"\nЕсли в истории встречаются имена [{ignore_names}] — это мусор "
                    "из старых сообщений, полностью ИГНОРИРУЙ их: это НЕ имя собеседника и НЕ твоё имя."
                )
        self.openai_chat = (
            tool.get_openai_chat(system_prompt) if args.use_ai else None
        )
        # Отдельный классификатор для хэндоффа + множество уже переданных чатов.
        self.handoff_chat = (
            tool.get_openai_chat(HANDOFF_SYS) if args.use_ai else None
        )
        self.handoff_seen = pgconn.seen_keys("handoff")
        # Смотрим чаты status=all (не только active: вопрос мог прийти в архивный отклик),
        # отсортированные по свежести, до чатов старше period дней (дефолт 21).
        self.period = args.period or DEFAULT_PERIOD_DAYS

        logger.debug(f"{self.reply_message = }")
        await self.reply_employers()

    async def reply_employers(self):
        blacklist = set(await self.tool.get_blacklisted())
        me: datatypes.User = await self.tool.get_me()
        resumes = await self.tool.get_resumes()
        resumes = (
            list(filter(lambda x: x["id"] == self.resume_id, resumes))
            if self.resume_id
            else resumes
        )
        resumes = list(
            filter(
                lambda resume: resume["status"]["id"] == "published", resumes
            )
        )
        await self._reply_chats(
            user=me, resumes=resumes, blacklist=blacklist
        )

    async def _is_interview_invite(self, text: str) -> bool:
        """Последнее сообщение работодателя — приглашение на интервью/созвон?
        Дешёвый keyword-префильтр, затем LLM ДА/НЕТ."""
        if not (self.handoff_chat and text):
            return False
        low = text.lower()
        if not (any(k in low for k in _INVITE_KW) or _TIME_RE.search(text)):
            return False
        if any(b in low for b in _BOT_MARKERS):
            return False  # авто-скрининг по ссылке/бот -> это не живой хэндофф (🟡)
        try:
            ans = (await self.handoff_chat.send_message(text)).strip().upper()
        except AIError:
            return False  # LLM недоступна — не эскалируем (не теряем, ответим позже)
        return ans.startswith("ДА")

    async def _reply_chats(
        self,
        user: datatypes.User,
        resumes: list[datatypes.Resume],
        blacklist: set[str],
    ) -> None:
        self._resume_map = {r["id"]: r for r in resumes}
        self._blacklist = blacklist
        self._base_placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
        }
        # Чат, где с прошлого прогона ничего не изменилось (тот же updated_at), не перечитываем:
        # раньше каждые 20 минут читались сотни чатов и заново звался LLM на одни и те же сообщения.
        if not self.dry_run:
            _prune_seen("reply_upd", days=DEFAULT_PERIOD_DAYS + 14)
        self._done_seen = pgconn.seen_keys("reply_upd")
        self._done_new: list[str] = []
        stats: collections.Counter = collections.Counter()
        self._stats = stats
        try:
            async for negotiation in self.tool.get_negotiations(
                status="all", order_by="updated_at"
            ):
                try:
                    outcome = await self._handle_negotiation(negotiation)
                except ApiError as ex:
                    logger.error(ex)
                    outcome = "api_error"
                stats[outcome] += 1
                if outcome == "old":
                    break  # дальше только более старые чаты
        finally:
            if self._done_new and not self.dry_run:
                pgconn.add_seen("reply_upd", self._done_new)

        print("📝 reply: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        # LLM лежит: раньше это выглядело как успешный прогон с 0 ответов (так прошёл месяц).
        if stats["ai_error"] >= LLM_DOWN_MIN_ERRORS and not stats["llm_ok"]:
            msg = (f"LLM недоступна — бот не ответил работодателям в {stats['ai_error']} "
                   "чатах. Проверь локальную LLM или ответь сам.")
            print("🔴", msg)
            if not self.dry_run:
                pgconn.notify(pgconn.PRIORITY_HIGH, msg, category="action",
                              dedup_key=f"llm_down:{date.today().isoformat()}")

    def _done(self, key: str) -> None:
        self._done_new.append(key)
        self._done_seen.add(key)

    def _ask(self, placeholders: dict, link: str, key: str, reason: str,
             question: str = "") -> None:
        """Вопрос, на который отвечает только кандидат, -> 🔴 уведомление, бот в чате молчит."""
        q = " ".join((question or "").split())
        text = (f"Нужен твой ответ — {reason}" + (f": «{q[:160]}»" if q else "")
                + f" — {placeholders['vacancy_name']} ({placeholders['employer_name']})")
        print("🙋", text, link)
        if not self.dry_run:
            pgconn.notify(pgconn.PRIORITY_HIGH, text, category="question", link=link,
                          dedup_key=key)

    async def _load_messages(self, nid) -> list[dict]:
        """Первая страница + последняя (там свежие сообщения). Пустые (вложения) не выкидываем."""
        res = await self.api_client.get(f"/negotiations/{nid}/messages", page=0, per_page=100)
        items = list(res.get("items") or [])
        pages = res.get("pages") or 1
        if pages > 1:
            last = await self.api_client.get(
                f"/negotiations/{nid}/messages", page=pages - 1, per_page=100)
            items += list(last.get("items") or [])
        return items

    @staticmethod
    def _answer_rejected(text_msgs: list[dict], last_text: str) -> bool:
        """Авто-анкета повторила вопрос сразу после нашего ответа -> ответ текстом не принят."""
        for j in range(1, len(text_msgs) - 1):
            prev, mine, nxt = text_msgs[j - 1], text_msgs[j], text_msgs[j + 1]
            if (mine["author"]["participant_type"] != "employer"
                    and prev["author"]["participant_type"] == "employer"
                    and nxt["author"]["participant_type"] == "employer"
                    and prev["text"].strip() == nxt["text"].strip() == last_text):
                return True
        return False

    def _check_reply(self, raw: str, last_text: str, yes_no: bool) -> tuple[str, str]:
        """Вердикт по черновику LLM: ok | skip | ask | fix (перепиши) | bad (мусор)."""
        s = (raw or "").strip().strip('"«»').strip()
        if rules.is_skip(s):
            return "skip", ""
        if rules.is_ask(s):
            return "ask", ""
        low = s.lower()
        max_len = 1200 if len(last_text) > 800 else 600  # на длинный чек-лист — длиннее ответ
        if any(p in low for p in _META) or len(s) > max_len:
            return "bad", s
        if (any(p in low for p in _PLACEHOLDER) or rules.has_template_placeholder(s)
                or rules.has_promise(s) or rules.offers_call_unprompted(s, last_text)):
            return "fix", s
        if yes_no:
            yn = rules.normalize_yes_no(s)
            return ("ok", yn) if yn else ("ask", s)
        return "ok", s

    async def _handle_negotiation(self, negotiation: dict) -> str:
        nid = negotiation["id"]
        updated_at = parse_api_datetime(negotiation["updated_at"])
        if (datetime.now(updated_at.tzinfo) - updated_at).days > self.period:
            return "old"
        resume = self._resume_map.get((negotiation.get("resume") or {}).get("id"))
        if not resume:
            return "other_resume"
        state_id = (negotiation.get("state") or {}).get("id") or ""
        if state_id == "discard":
            return "discard"
        if self.only_invitations and not state_id.startswith("inv"):
            return "not_invitation"
        # Чат уже передан тебе (хэндофф по интервью) — бот в него не лезет.
        if str(nid) in self.handoff_seen:
            return "handoff"
        vacancy = negotiation.get("vacancy") or {}
        employer = vacancy.get("employer") or {}
        if employer.get("id") in self._blacklist:
            return "blacklist"
        done_key = f"{nid}:{negotiation['updated_at']}"
        if done_key in self._done_seen:
            return "unchanged"
        # Писать в чат нельзя (disabled_by_employer / no_invitation) — не генерируем ответ впустую.
        if (negotiation.get("messaging_status") or "ok") != "ok":
            self._done(done_key)
            return "no_messaging"

        placeholders = {
            "vacancy_name": vacancy.get("name", ""),
            "employer_name": employer.get("name", ""),
            "resume_title": resume.get("title") or "",
            **self._base_placeholders,
        }
        messages = await self._load_messages(nid)
        if not messages:
            return "empty"
        last = messages[-1]
        # Отвечаем ТОЛЬКО когда работодатель реально написал последним.
        if last["author"]["participant_type"] != "employer":
            self._done(done_key)
            return "ours_last"

        text_msgs = [m for m in messages if (m.get("text") or "").strip()]
        employer_texts = [m["text"] for m in text_msgs
                          if m["author"]["participant_type"] == "employer"]
        applicant_texts = [m["text"] for m in text_msgs
                           if m["author"]["participant_type"] != "employer"]
        history = [
            f"[ {parse_api_datetime(m['created_at']).strftime('%d.%m.%Y %H:%M')} ] "
            f"{'Работодатель' if m['author']['participant_type'] == 'employer' else 'Я'}: {m['text']}"
            for m in text_msgs
        ]
        last_text = (last.get("text") or "").strip()
        link = f"https://hh.ru/chat/{negotiation.get('chat_id') or nid}"
        ask_key = f"ask:{nid}:{last.get('id')}"

        # Приглашение на живой разговор -> эскалация тебе (🔴), бот в чате молчит навсегда.
        if last_text and await self._is_interview_invite(last_text):
            if not self.dry_run:
                pgconn.notify(
                    pgconn.PRIORITY_HIGH,
                    f"Интервью: {placeholders['vacancy_name']} — {placeholders['employer_name']}",
                    category="interview", link=link, dedup_key=f"interview:{nid}",
                )
                pgconn.add_seen("handoff", [str(nid)])
                self.handoff_seen.add(str(nid))
            print(f"🔔 ИНТЕРВЬЮ -> эскалация тебе, бот молчит: {link}")
            return "interview"
        # Файл/вложение без текста (ТЗ, презентация) — бот его не видит.
        if not last_text:
            self._ask(placeholders, link, ask_key, "работодатель прислал файл, посмотри сам")
            self._done(done_key)
            return "attachment"
        # Авто-уведомления hh/шаблоны/отказы и воронки «пройдите демо» — ответ не нужен.
        if rules.is_no_reply_needed(last_text) or rules.is_funnel_spam(last_text):
            self._done(done_key)
            return "no_reply_needed"
        questionnaire = rules.is_hh_questionnaire(employer_texts)
        # Анкета hh повторила вопрос сразу после нашего ответа: текстом она его не принимает.
        if self._answer_rejected(text_msgs, last_text):
            self._ask(placeholders, link, ask_key,
                      "анкета hh не принимает ответ текстом, ответь сам", last_text)
            self._done(done_key)
            return "rejected"
        # Условия оформления, СВО, работа за долю и т.п. — решает только кандидат.
        if rules.is_sensitive(last_text):
            self._ask(placeholders, link, ask_key, "вопрос, на который отвечаешь только ты",
                      last_text)
            self._done(done_key)
            return "sensitive"
        # Анти-петля «бот против бота» (работодатель-бот повторяет один и тот же текст).
        if employer_texts.count(last_text) >= 3 and len(applicant_texts) >= 3:
            if not self.dry_run:
                pgconn.notify(
                    pgconn.PRIORITY_MED,
                    f"Диалог завис — работодатель-бот повторяет вопрос: "
                    f"{placeholders['vacancy_name']} — {placeholders['employer_name']}",
                    category="action", link=link, dedup_key=f"reply_loop:{nid}",
                )
            print(f"🔁 Петля в чате {nid}: работодатель повторяет вопрос — молчим")
            self._done(done_key)
            return "loop"
        # Давнее сообщение (бот лежал / не мог ответить): ботом через неделю отвечать поздно,
        # но и молча терять нельзя — отдаём человеку.
        sent_at = parse_api_datetime(last["created_at"])
        if (datetime.now(sent_at.tzinfo) - sent_at).days >= STALE_DAYS:
            self._ask(placeholders, link, ask_key,
                      f"работодатель ждёт ответа больше {STALE_DAYS} дней", last_text)
            self._done(done_key)
            return "stale"

        if questionnaire and rules.is_questionnaire_start(last_text):
            send_message = "Да"  # «Начнем?» / «Используем эти ответы?»
        elif self.reply_message:
            send_message = rand_text(self.reply_message) % placeholders
        elif self.openai_chat:
            yes_no = questionnaire and rules.is_yes_no_question(last_text)
            query = (
                f"Вакансия: {placeholders['vacancy_name']}\n"
                "История переписки:\n" + "\n".join(history[-10:])
                + f"\n\nИнструкция: {self.pre_prompt}"
                + (_QUESTIONNAIRE_HINT if questionnaire else "")
            )
            try:
                raw = await self.openai_chat.send_message(query)
                self._stats["llm_ok"] += 1
                verdict, send_message = self._check_reply(raw, last_text, yes_no)
                if verdict == "fix":  # одна попытка переписать без обещаний/созвона/скобок
                    raw = await self.openai_chat.send_message(query + _FIX_HINT)
                    verdict, send_message = self._check_reply(raw, last_text, yes_no)
            except AIError as ex:
                logger.warning(f"Ошибка OpenAI для чата {nid}: {ex}")
                return "ai_error"  # не помечаем — ответим, когда LLM оживёт
            if verdict == "skip":
                logger.debug("AI: ответ не требуется (SKIP) — чат %s", nid)
                self._done(done_key)
                return "skip"
            if verdict != "ok":
                logger.warning("reply %s -> человеку (%s): %.200s", nid, verdict, send_message)
                self._ask(placeholders, link, ask_key,
                          "бот не смог ответить сам" if verdict in ("bad", "fix")
                          else "вопрос, на который отвечаешь только ты", last_text)
                self._done(done_key)
                return "ask"
        else:
            send_message = self._interactive(negotiation, placeholders, history, resume)
            if send_message is None:
                return "manual_skip"
            if send_message.startswith("/ban"):
                if not self.dry_run:
                    await self.api_client.put(f"/employers/blacklisted/{employer['id']}")
                    self._blacklist.add(employer["id"])
                print("🚫 Работодатель заблокирован" + (" (dry-run)" if self.dry_run else ""),
                      employer.get("alternate_url"))
                return "ban"
            if send_message.startswith("/cancel"):
                _, decline_msg = send_message.split("/cancel", 1)
                if not self.dry_run:
                    await self.api_client.delete(
                        f"/negotiations/active/{nid}",
                        with_decline_message=decline_msg.strip(),
                    )
                print("❌ Отмена заявки" + (" (dry-run)" if self.dry_run else ""),
                      vacancy.get("alternate_url"))
                return "cancel"

        if self.dry_run:
            logger.debug("dry-run: ответ на %s: %s", vacancy.get("alternate_url"), send_message)
            print(f"🧪 dry-run {nid}: {send_message[:200]}")
            return "dry"

        await self.api_client.post(
            f"/negotiations/{nid}/messages",
            message=send_message,
            delay=random.uniform(1, 3),
        )
        pgconn.bump_activity("reply", 1)
        print(f"📨 Отправлено для {vacancy.get('alternate_url')}")
        return "sent"

    def _interactive(self, negotiation, placeholders, history, resume) -> str | None:
        """Ручной режим (без --use-ai и без --reply): спросить текст в терминале."""
        salary = (negotiation.get("vacancy") or {}).get("salary") or {}
        print("🏢", placeholders["employer_name"])
        print("💼", placeholders["vacancy_name"])
        if salary:
            print("💵 от", salary.get("from") or salary.get("to") or 0,
                  "до", salary.get("to") or salary.get("from") or 0,
                  salary.get("currency", "RUR"))
        print("\nПоследние сообщения чата:\n")
        for msg in history[-5:]:
            print(msg)
        try:
            print("-" * 40)
            print("Активное резюме:", resume.get("title") or "")
            print("/ban, /cancel необязательное сообщение для отмены")
            send_message = input("Ваше сообщение: ").strip()
        except EOFError:
            return None
        if not send_message:
            print("🚶 Пропускаем чат")
            return None
        return send_message
