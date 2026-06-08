from __future__ import annotations

import argparse
import logging
import random
import re
from datetime import datetime
from typing import TYPE_CHECKING

from ..ai.base import AIError
from ..api import ApiError, datatypes
from ..main import BaseNamespace, BaseOperation
from ..storage import pgconn
from ..utils.date import parse_api_datetime
from ..utils.string import rand_text

MAX_REJECT_REPLIES = 6  # вежливых ответов на отказы за прогон (чтобы не залпом по накопленным)
REJECT_TEMPLATES = (
    "Спасибо за ответ! Буду рад, если вспомните обо мне, когда появятся подходящие позиции. Удачи в поиске!",
    "Благодарю за рассмотрение моей кандидатуры. Если появится что-то подходящее — буду рад вернуться к диалогу.",
    "Спасибо, что рассмотрели! Остаюсь открытым к общению, если в будущем будет интересная вакансия.",
    "Понял, спасибо за обратную связь! Успехов в поиске, и буду признателен, если вспомните обо мне позже.",
)

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
                "Ты ведёшь переписку с работодателем на hh.ru от лица соискателя (кандидата). "
                "Это живой чат — отвечай ОТ ПЕРВОГО ЛИЦА ПРОСТЫМ человеческим разговорным языком, "
                "как пишешь в мессенджере: вежливо, коротко (2–4 предложения), по делу, обычными "
                "словами. БЕЗ канцелярита и пустых бузвордов («масштабные задачи», «экспертиза», "
                "«синергия», «реализация решений», «проекты полного цикла») — говори по-простому, "
                "как живой человек. Не упоминай, что ты ИИ.\n"
                "СТРОГИЕ ЗАПРЕТЫ:\n"
                "- НИКОГДА не называй, не подтверждай и не соглашайся на конкретные дату/время/адрес/место встречи "
                "или собеседования — ДАЖЕ если работодатель их предложил. Время собеседования кандидат согласует сам, "
                "лично. Не пиши «буду на собеседовании <дата/время>» и не подтверждай предложенный работодателем слот.\n"
                "- НИКОГДА не называй конкретную желаемую зарплату, дату выхода или срок, если их нет в резюме/переписке.\n"
                "- Не выдумывай факты об опыте, которых нет в резюме. Не бери на себя обязательств, в которых не уверен.\n"
                "- Если работодатель просит выполнить ВНЕШНЕЕ действие (тест/тестовое/анкета/опрос/форма/регистрация/"
                "переход в бот/заявка по ссылке): НЕ обещай его выполнить (никаких «заполню/обязательно заполню/пройду/"
                "ознакомлюсь/перейду в бот/пришлю результаты/приступлю/сделаю»), НО и НЕ отказывайся — НЕ пиши "
                "«неактуально», «не интересно», «не подходит», «не актуально». Сохрани интерес: поблагодари за информацию "
                "и предложи продолжить общение или обсудить детали на созвоне. Само задание кандидат выполнит сам отдельно.\n"
                "- НИКОГДА не отказывайся от вакансии и не пиши, что предложение «неактуально»/«не интересно»/«не подходит». "
                "Кандидат заинтересован — твоя задача поддержать диалог, а не закрыть его. Отказ уместен ТОЛЬКО если "
                "работодатель сам прислал отказ кандидату (тогда коротко поблагодари за ответ).\n"
                "- Если спрашивают ЛИЧНЫЙ факт, которого нет в резюме (военный билет, гражданство, готовность к переезду, "
                "семейное положение, наличие водительских прав/чего-либо) — НЕ выдумывай «да/нет»; ответь нейтрально, "
                "что готов уточнить эту деталь на созвоне/собеседовании.\n"
                "ЧТО ДЕЛАТЬ:\n"
                "- Если спрашивают, актуальна ли вакансия / интересно ли — подтверди интерес и предложи согласовать удобное время для созвона (без конкретных дат).\n"
                "- Если приглашают на интервью/созвон или речь о ВРЕМЕНИ: НЕ выбирай и НЕ подтверждай время сам. "
                "Поблагодари за приглашение, подтверди интерес и напиши, что согласуешь удобное время и свяжешься лично "
                "(например: «Спасибо за приглашение! Согласую удобное время и свяжусь с вами»). О конкретном времени "
                "кандидат договаривается сам — даже если работодатель уже назвал слот, не соглашайся на него за кандидата.\n"
                "- Вопрос про опыт/навыки — отвечай строго по фактам из резюме; чего нет — предложи обсудить на созвоне.\n"
                "- Если это отказ — коротко поблагодари за ответ."
            ),
        )
        parser.add_argument(
            "--prompt",
            help="Промпт для генерации сообщения",
            default=(
                "Сформулируй ответ на ПОСЛЕДНЕЕ сообщение работодателя в этой переписке. "
                "Выведи РОВНО готовый текст сообщения от лица кандидата — и больше НИЧЕГО: "
                "без пояснений, рассуждений, разбора ситуации, вариантов на выбор, кавычек-ёлочек "
                "и без фраз вида «можно написать», «если хотите», «отвечать не требуется», «в данной ситуации». "
                "Если отвечать НЕ нужно — последнее сообщение это простое подтверждение/благодарность "
                "(«ок», «хорошо», «договорились», «спасибо», «принято», «до связи», смайл) ИЛИ переписка уже "
                "завершена / продолжается в другом канале — выведи РОВНО одно слово: SKIP"
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
        # Формат работы из резюме с приоритетом удалёнка > гибрид > офис
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
        self.period = args.period

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
        resume_map = {r["id"]: r for r in resumes}

        base_placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
        }

        reject_replies = 0
        reject_seen = set(map(str, pgconn.seen_keys("reject_reply")))

        async for negotiation in self.tool.get_negotiations():
            try:
                # try:
                #     self.tool.storage.negotiations.save(negotiation)
                # except RepositoryError as e:
                #     logger.exception(e)

                if not (resume := resume_map.get(negotiation["resume"]["id"])):
                    continue

                updated_at = parse_api_datetime(negotiation["updated_at"])

                # Пропуск откликов, которые не обновлялись более N дней (при просмотре они обновляются вроде)
                if (
                    self.period
                    and (datetime.now(updated_at.tzinfo) - updated_at).days
                    > self.period
                ):
                    continue

                state_id = negotiation["state"]["id"]
                if state_id == "discard":
                    # Вежливый ответ на отказ = активность аккаунта. Только свежие
                    # (<=7 дней), с лимитом за прогон и дедупом (не дважды один отказ).
                    nid = negotiation["id"]
                    if reject_replies >= MAX_REJECT_REPLIES:
                        continue
                    if (datetime.now(updated_at.tzinfo) - updated_at).days > 7:
                        continue
                    if str(nid) in reject_seen:
                        continue
                    try:
                        await self.api_client.post(
                            f"/negotiations/{nid}/messages",
                            message=random.choice(REJECT_TEMPLATES),
                            delay=random.uniform(1, 3),
                        )
                        pgconn.add_seen("reject_reply", str(nid))
                        reject_seen.add(str(nid))
                        pgconn.bump_activity("reply", 1)
                        reject_replies += 1
                        print(
                            "🙏 Ответ на отказ",
                            (negotiation.get("vacancy") or {}).get("alternate_url", nid),
                        )
                    except ApiError as ex:
                        logger.warning("reject-reply %s: %s", nid, ex)
                    continue

                if self.only_invitations and not state_id.startswith("inv"):
                    continue

                nid = negotiation["id"]
                vacancy = negotiation["vacancy"]
                employer = vacancy.get("employer") or {}
                salary = vacancy.get("salary") or {}

                if employer.get("id") in blacklist:
                    print(
                        "🚫 Пропускаем заблокированного работодателя",
                        employer.get("alternate_url"),
                    )
                    continue

                # Чат уже передан тебе (хэндофф по интервью) — бот в него не лезет.
                if str(nid) in self.handoff_seen:
                    continue

                placeholders = {
                    "vacancy_name": vacancy.get("name", ""),
                    "employer_name": employer.get("name", ""),
                    "resume_title": resume.get("title") or "",
                    **base_placeholders,
                }

                logger.debug(
                    "Вакансия %(vacancy_name)s от %(employer_name)s"
                    % placeholders
                )

                page: int = 0
                last_message: datatypes.Message | None = None
                message_history: list[str] = []
                while True:
                    messages_res: datatypes.PaginatedItems[
                        datatypes.Message
                    ] = await self.api_client.get(
                        f"/negotiations/{nid}/messages", page=page
                    )
                    if not messages_res["items"]:
                        break

                    last_message = messages_res["items"][-1]
                    for message in messages_res["items"]:
                        if not message.get("text"):
                            continue
                        author = (
                            "Работодатель"
                            if message["author"]["participant_type"]
                            == "employer"
                            else "Я"
                        )
                        message_date = parse_api_datetime(
                            message.get("created_at")
                        ).strftime("%d.%m.%Y %H:%M:%S")

                        message_history.append(
                            f"[ {message_date} ] {author}: {message['text']}"
                        )

                    if page + 1 >= messages_res["pages"]:
                        break
                    page = messages_res["pages"] - 1

                if not last_message:
                    continue

                is_employer_message = (
                    last_message["author"]["participant_type"] == "employer"
                )

                # Отвечаем ТОЛЬКО когда работодатель реально написал последним.
                # (Раньше также срабатывало на "не просмотрено" — это слало
                # сообщения по свежим неоткрытым откликам, т.е. спам.)
                if is_employer_message:
                    # Приглашение на интервью -> эскалация тебе (🔴), бот в чате
                    # молчит, чат помечается переданным (больше не трогаем).
                    last_text = last_message.get("text") or ""
                    if await self._is_interview_invite(last_text):
                        chat_id = negotiation.get("chat_id") or nid
                        link = f"https://hh.ru/chat/{chat_id}"
                        if not self.dry_run:
                            pgconn.notify(
                                pgconn.PRIORITY_HIGH,
                                f"Интервью: {placeholders['vacancy_name']}"
                                f" — {placeholders['employer_name']}",
                                category="interview",
                                link=link,
                                dedup_key=f"interview:{nid}",
                            )
                            pgconn.add_seen("handoff", [str(nid)])
                            self.handoff_seen.add(str(nid))
                        print(f"🔔 ИНТЕРВЬЮ -> эскалация тебе, бот молчит: {link}")
                        continue

                    send_message = ""
                    if self.reply_message:
                        send_message = (
                            rand_text(self.reply_message) % placeholders
                        )
                        logger.debug(f"Template message: {send_message}")
                    elif self.openai_chat:
                        try:
                            ai_query = (
                                f"Вакансия: {placeholders['vacancy_name']}\n"
                                f"История переписки:\n"
                                + "\n".join(message_history[-10:])
                                + f"\n\nИнструкция: {self.pre_prompt}"
                            )
                            send_message = await self.openai_chat.send_message(
                                ai_query
                            )
                            logger.debug(f"AI message: {send_message}")
                        except AIError as ex:
                            logger.warning(
                                f"Ошибка OpenAI для чата {nid}: {ex}"
                            )
                            continue
                        # GUARD: LLM иногда выдаёт мета-рассуждение/варианты вместо самого
                        # ответа («отвечать не требуется… можно написать: …») — такое НЕЛЬЗЯ
                        # слать работодателю. Лучше промолчать, чем отправить мусор.
                        send_message = (send_message or "").strip().strip('"«». ')
                        _low = send_message.lower()
                        _META = (
                            "отвечать не требуется", "не требует ответа", "отвечать не нужно",
                            "можно не отвечать", "можно написать", "можно ответить",
                            "вы можете написать", "достаточно написать", "переписка завершена",
                            "переписка фактически", "в данной ситуации", "в этой ситуации",
                            "в качестве ассистента", "как ассистент", "я ассистент",
                            "как ии", "я не могу", "следующий шаг",
                        )
                        if (not send_message or send_message.upper().startswith("SKIP")
                                or send_message in ("-", "—")):
                            logger.debug("AI: ответ не требуется (SKIP) — чат %s", nid)
                            continue
                        if any(p in _low for p in _META) or len(send_message) > 600:
                            print(f"⚠️ AI вернул мета-текст — НЕ отправляю (чат {nid})")
                            logger.warning("meta-reply skipped %s: %.200s", nid, send_message)
                            continue
                        # GUARD: LLM иногда вставляет контакт-плейсхолдер («@username (замените на ваш
                        # реальный ник)») — это шаблон-заглушка, слать работодателю НЕЛЬЗЯ.
                        _PLACEHOLDER = (
                            "@username", "замените на", "замени на", "реальный ник", "ваш ник",
                            "ваш реальный", "укажите ваш", "укажите свой", "впишите", "вставьте ваш",
                            "your_username", "your username", "[ваш", "<ваш", "[имя", "<имя", "вашник",
                        )
                        if any(p in _low for p in _PLACEHOLDER):
                            print(f"⚠️ AI вставил плейсхолдер-контакт — НЕ отправляю (чат {nid})")
                            logger.warning("placeholder-reply skipped %s: %.200s", nid, send_message)
                            continue
                    else:
                        print("🏢", placeholders["employer_name"])
                        print("💼", placeholders["vacancy_name"])
                        if salary:
                            print(
                                "💵 от",
                                salary.get("from") or salary.get("to") or 0,
                                "до",
                                salary.get("to") or salary.get("from") or 0,
                                salary.get("currency", "RUR"),
                            )

                        print("\nПоследние сообщения чата:")
                        print()
                        for msg in (
                            message_history[-5:]
                            if len(message_history) > 5
                            else message_history
                        ):
                            print(msg)

                        try:
                            print("-" * 40)
                            print("Активное резюме:", resume.get("title") or "")
                            print(
                                "/ban, /cancel необязательное сообщение для отмены"
                            )
                            send_message = input("Ваше сообщение: ").strip()
                        except EOFError:
                            continue

                        if not send_message:
                            print("🚶 Пропускаем чат")
                            continue

                        if send_message.startswith("/ban"):
                            if not self.dry_run:
                                await self.api_client.put(
                                    f"/employers/blacklisted/{employer['id']}"
                                )
                                blacklist.add(employer["id"])
                            print(
                                "🚫 Работодатель заблокирован"
                                + (" (dry-run)" if self.dry_run else ""),
                                employer.get("alternate_url"),
                            )
                            continue
                        elif send_message.startswith("/cancel"):
                            _, decline_msg = send_message.split("/cancel", 1)
                            if not self.dry_run:
                                await self.api_client.delete(
                                    f"/negotiations/active/{nid}",
                                    with_decline_message=decline_msg.strip(),
                                )
                            print(
                                "❌ Отмена заявки"
                                + (" (dry-run)" if self.dry_run else ""),
                                vacancy["alternate_url"],
                            )
                            continue

                    # Финальная отправка текста
                    if self.dry_run:
                        logger.debug(
                            "dry-run: ответ на %s: %s",
                            vacancy["alternate_url"],
                            send_message,
                        )
                        continue

                    await self.api_client.post(
                        f"/negotiations/{nid}/messages",
                        message=send_message,
                        delay=random.uniform(1, 3),
                    )
                    pgconn.bump_activity("reply", 1)
                    print(f"📨 Отправлено для {vacancy['alternate_url']}")

            except ApiError as ex:
                logger.error(ex)

        print("📝 Сообщения разосланы!")
