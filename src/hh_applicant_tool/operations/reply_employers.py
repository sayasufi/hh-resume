from __future__ import annotations

import argparse
import logging
import random
from datetime import datetime
from typing import TYPE_CHECKING

from ..ai.base import AIError
from ..api import ApiError, datatypes
from ..main import BaseNamespace, BaseOperation
from ..utils.date import parse_api_datetime
from ..utils.string import rand_text

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
                "Отвечай ОТ ПЕРВОГО ЛИЦА, вежливо, кратко (2–4 предложения), по делу, без канцелярита. "
                "Не упоминай, что ты ИИ.\n"
                "СТРОГИЕ ЗАПРЕТЫ:\n"
                "- НИКОГДА не называй и не подтверждай конкретные дату, время, адрес или место встречи/собеседования, "
                "если работодатель сам их не указал в этой переписке. Не пиши фразы вида «буду на собеседовании <дата/время>» с выдуманным временем.\n"
                "- НИКОГДА не называй конкретную желаемую зарплату, дату выхода или срок, если их нет в резюме/переписке.\n"
                "- Не выдумывай факты об опыте, которых нет в резюме. Не бери на себя обязательств, в которых не уверен.\n"
                "- Если спрашивают ЛИЧНЫЙ факт, которого нет в резюме (военный билет, гражданство, готовность к переезду, "
                "семейное положение, наличие водительских прав/чего-либо) — НЕ выдумывай «да/нет»; ответь нейтрально, "
                "что готов уточнить эту деталь на созвоне/собеседовании.\n"
                "ЧТО ДЕЛАТЬ:\n"
                "- Если спрашивают, актуальна ли вакансия / интересно ли — подтверди интерес и предложи согласовать удобное время для созвона (без конкретных дат).\n"
                "- Если приглашают на интервью, а работодатель НЕ назвал время — поблагодари, подтверди готовность и попроси предложить удобное время.\n"
                "- Если работодатель САМ предложил конкретное время — можешь подтвердить, что оно подходит, или вежливо попросить альтернативу, но НЕ придумывай другое.\n"
                "- Вопрос про опыт/навыки — отвечай строго по фактам из резюме; чего нет — предложи обсудить на созвоне.\n"
                "- Если это отказ — коротко поблагодари за ответ."
            ),
        )
        parser.add_argument(
            "--prompt",
            help="Промпт для генерации сообщения",
            default="Сформулируй ответ на ПОСЛЕДНЕЕ сообщение работодателя в этой переписке.",
        )

    async def run(self, tool: HHApplicantTool) -> None:
        args: Namespace = tool.args
        self.tool = tool
        self.api_client = tool.api_client
        self.resume_id = await tool.first_resume_id()
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
                            await self.api_client.put(
                                f"/employers/blacklisted/{employer['id']}"
                            )
                            blacklist.add(employer["id"])
                            print(
                                "🚫 Работодатель заблокирован",
                                employer.get("alternate_url"),
                            )
                            continue
                        elif send_message.startswith("/cancel"):
                            _, decline_msg = send_message.split("/cancel", 1)
                            await self.api_client.delete(
                                f"/negotiations/active/{nid}",
                                with_decline_message=decline_msg.strip(),
                            )
                            print("❌ Отмена заявки", vacancy["alternate_url"])
                            continue

                    # Финальная отправка текста
                    if self.dry_run:
                        logger.debug(
                            "dry-run: отклик на",
                            vacancy["alternate_url"],
                            send_message,
                        )
                        continue

                    await self.api_client.post(
                        f"/negotiations/{nid}/messages",
                        message=send_message,
                        delay=random.uniform(1, 3),
                    )
                    print(f"📨 Отправлено для {vacancy['alternate_url']}")

            except ApiError as ex:
                logger.error(ex)

        print("📝 Сообщения разосланы!")
