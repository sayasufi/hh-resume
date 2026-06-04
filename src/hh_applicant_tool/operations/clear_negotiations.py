from __future__ import annotations

import argparse
import datetime as dt
import logging
from typing import TYPE_CHECKING

from ..api.errors import ApiError
from ..main import BaseNamespace, BaseOperation
from ..utils.date import parse_api_datetime

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    cleanup: bool
    blacklist_discard: bool
    older_than: int
    dry_run: bool


class Operation(BaseOperation):
    """Удаляет отказы либо старые отклики."""

    __aliases__ = ["clear-negotiations", "delete-negotiations"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-b",
            "--blacklist-discard",
            "--blacklist",
            action=argparse.BooleanOptionalAction,
            help="Блокировать работодателя за отказ",
        )
        parser.add_argument(
            "-o",
            "--older-than",
            type=int,
            help="С флагом --clean удаляет любые отклики старше N дней",
        )
        parser.add_argument(
            "-n",
            "--dry-run",
            action=argparse.BooleanOptionalAction,
            help="Тестовый запуск без реального удаления",
        )

    async def run(self, tool: HHApplicantTool) -> None:
        self.tool = tool
        self.args: Namespace = tool.args
        await self.clear()

    async def clear(self) -> None:
        blacklisted = set(await self.tool.get_blacklisted())
        async for negotiation in self.tool.get_negotiations():
            vacancy = negotiation["vacancy"]

            # Если работодателя блокируют, то он превращается в null
            # ХХ позволяет скрывать компанию, когда id нет, а вместо имени "Крупная российская компания"
            # sqlite3.IntegrityError: NOT NULL constraint failed: negotiations.employer_id
            # try:
            #     storage.negotiations.save(negotiation)
            # except RepositoryError as e:
            #     logger.exception(e)

            if self.args.older_than:
                updated_at = parse_api_datetime(negotiation["updated_at"])
                # А хз какую временную зону сайт возвращает
                days_passed = (
                    dt.datetime.now(updated_at.tzinfo) - updated_at
                ).days
                logger.debug(f"{days_passed = }")
                if days_passed <= self.args.older_than:
                    continue
            elif negotiation["state"]["id"] != "discard":
                continue
            try:
                if not self.args.dry_run:
                    await self.tool.api_client.delete(
                        f"/negotiations/active/{negotiation['id']}",
                        with_decline_message=True,
                    )

                print(
                    "🗑️ Отменили отклик на вакансию:",
                    vacancy["alternate_url"],
                    vacancy["name"],
                )

                employer = vacancy.get("employer", {})
                employer_id = employer.get("id")

                if (
                    self.args.blacklist_discard
                    and employer
                    and employer_id
                    and employer_id not in blacklisted
                ):
                    if not self.args.dry_run:
                        await self.tool.api_client.put(
                            f"/employers/blacklisted/{employer_id}"
                        )
                        blacklisted.add(employer_id)

                    print(
                        "🚫 Работодатель заблокирован:",
                        employer["name"],
                        employer["alternate_url"],
                    )
            except ApiError as err:
                logger.error(err)

        print("✅ Удаление откликов завершено.")
