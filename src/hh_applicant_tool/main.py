from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterable, Sequence
from functools import cached_property
from importlib import import_module
from itertools import count
from os import getenv
from pathlib import Path
from pkgutil import iter_modules
from typing import Any

import psycopg

from . import ai, api, utils
from .storage import StorageFacade
from .storage.pgconn import aconnect, locked_token_refresh
from .utils.log import setup_logger
from .utils.mixins import MegaTool

DEFAULT_CONFIG_DIR = utils.get_config_path() / (__package__ or "").replace(
    "_", "-"
)
DEFAULT_CONFIG_FILENAME = "config.json"
DEFAULT_LOG_FILENAME = "log.txt"
DEFAULT_DATABASE_FILENAME = "data"

logger = logging.getLogger(__package__)


class BaseOperation:
    def setup_parser(self, parser: argparse.ArgumentParser) -> None: ...

    def run(
        self,
        tool: HHApplicantTool,
    ) -> None | int:
        raise NotImplementedError()


OPERATIONS = "operations"


class BaseNamespace(argparse.Namespace):
    profile_id: str
    config_dir: Path
    verbosity: int
    delay: float
    user_agent: str
    proxy_url: str


class HHApplicantTool(MegaTool):
    """Утилита для автоматизации действий соискателя на сайте hh.ru.

    Исходники и предложения: <https://github.com/s3rgeym/hh-applicant-tool>

    Группа поддержки: <https://t.me/hh_applicant_tool>
    """

    class ArgumentFormatter(
        argparse.ArgumentDefaultsHelpFormatter,
        argparse.RawDescriptionHelpFormatter,
    ):
        pass

    def _create_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=self.__doc__,
            formatter_class=self.ArgumentFormatter,
        )
        parser.add_argument(
            "-v",
            "--verbosity",
            help="При использовании от одного и более раз увеличивает количество отладочной информации в выводе",  # noqa: E501
            action="count",
            default=0,
        )
        parser.add_argument(
            "-c",
            "--config-dir",
            "--config",
            help="Путь до директории с конфигом",
            type=Path,
            default=None,
        )
        parser.add_argument(
            "--profile-id",
            "--profile",
            help="Используемый профиль — подкаталог в --config-dir. Так же можно передать через переменную окружения HH_PROFILE_ID.",
        )
        parser.add_argument(
            "-d",
            "--api-delay",
            "--delay",
            type=float,
            help="Задержка между запросами к API HH по умолчанию",
        )
        parser.add_argument(
            "--user-agent",
            help="User-Agent для каждого запроса",
        )
        parser.add_argument(
            "--proxy-url",
            help="Прокси, используемый для запросов и авторизации",
        )
        subparsers = parser.add_subparsers(help="commands")
        package_dir = Path(__file__).resolve().parent / OPERATIONS
        for _, module_name, _ in iter_modules([str(package_dir)]):
            if module_name.startswith("_"):
                continue
            mod = import_module(f"{__package__}.{OPERATIONS}.{module_name}")
            op: BaseOperation = mod.Operation()
            kebab_name = module_name.replace("_", "-")
            op_parser = subparsers.add_parser(
                kebab_name,
                aliases=getattr(op, "__aliases__", []),
                description=op.__doc__,
                formatter_class=self.ArgumentFormatter,
            )
            op_parser.set_defaults(run=op.run)
            op.setup_parser(op_parser)
        parser.set_defaults(run=None)
        return parser

    def __init__(self, argv: Sequence[str] | None):
        self._parse_args(argv)

        # Создаем путь до конфига
        self.config_path.mkdir(
            parents=True,
            exist_ok=True,
        )

    @cached_property
    def config_path(self) -> Path:
        return (
            (
                self.args.config_dir
                or Path(getenv("CONFIG_DIR", DEFAULT_CONFIG_DIR))
            )
            / (self.args.profile_id or getenv("HH_PROFILE_ID", "."))
        ).resolve()

    @cached_property
    def config(self) -> utils.Config:
        return utils.Config(self.config_path / DEFAULT_CONFIG_FILENAME)

    @cached_property
    def log_file(self) -> Path:
        return self.config_path / DEFAULT_LOG_FILENAME

    @cached_property
    def db_path(self) -> Path:
        return self.config_path / DEFAULT_DATABASE_FILENAME

    def _proxy_url(self) -> str | None:
        return (
            self.args.proxy_url
            or self.config.get("proxy_url")
            or getenv("HTTPS_PROXY")
            or getenv("HTTP_PROXY")
        )

    @cached_property
    def api_client(self) -> api.client.ApiClient:
        args = self.args
        config = self.config
        token = config.get("token", {})
        return api.client.ApiClient(
            client_id=config.get("client_id"),
            client_secret=config.get("client_id"),
            access_token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            access_expires_at=token.get("access_expires_at"),
            delay=args.api_delay or config.get("api_delay"),
            user_agent=args.user_agent or config.get("user_agent"),
            proxy=self._proxy_url(),
            refresh_hook=locked_token_refresh,
        )

    async def get_me(self) -> api.datatypes.User:
        return await self.api_client.get("/me")

    async def get_resumes(self) -> list[api.datatypes.Resume]:
        return (await self.api_client.get("/resumes/mine"))["items"]

    async def first_resume_id(self) -> str:
        resumes = await self.get_resumes()
        return resumes[0]["id"]

    async def get_blacklisted(self) -> list[str]:
        rv = []
        for page in count():
            r: api.datatypes.PaginatedItems[api.datatypes.EmployerShort] = (
                await self.api_client.get("/employers/blacklisted", page=page)
            )
            rv += [item["id"] for item in r["items"]]
            if page + 1 >= r["pages"]:
                break
        return rv

    async def get_negotiations(
        self, status: str = "active"
    ) -> AsyncIterable[api.datatypes.Negotiation]:
        for page in count():
            r: dict[str, Any] = await self.api_client.get(
                "/negotiations",
                page=page,
                per_page=100,
                status=status,
            )
            items = r.get("items", [])
            if not items:
                break
            for item in items:
                yield item
            if page + 1 >= r.get("pages", 0):
                break

    def save_token(self) -> bool:
        # Токен уже записан в PG под advisory-lock (locked_token_refresh) —
        # вторая транзакция не нужна (#7). Сохраняем только для пути без хука
        # (например, initial authorize), где _token_persisted остался False.
        if getattr(self.api_client, "_token_persisted", False):
            return False
        if self.api_client.access_token != self.config.get("token", {}).get(
            "access_token"
        ):
            self.config.save(token=self.api_client.get_access_token())
            return True
        return False

    def get_openai_chat(self, system_prompt: str) -> ai.ChatOpenAI:
        c = self.config.get("openai", {})
        if not (token := c.get("token")):
            raise ValueError("Токен для OpenAI не задан")
        return ai.ChatOpenAI(
            token=token,
            model=c.get("model"),
            temperature=c.get("temperature", 0.7),
            max_completion_tokens=c.get("max_completion_tokens", 1000),
            system_prompt=system_prompt,
            completion_endpoint=c.get("completion_endpoint"),
        )

    def run(self) -> None | int:
        return asyncio.run(self._arun())

    async def _arun(self) -> None | int:
        verbosity_level = max(
            logging.DEBUG,
            logging.WARNING - self.args.verbosity * 10,
        )

        setup_logger(logger, verbosity_level, self.log_file)

        logger.debug("Путь до профиля: %s", self.config_path)

        utils.setup_terminal()

        # Async-инициализация БД и storage (нельзя в cached_property)
        self._aconn = await aconnect()
        self.storage = StorageFacade(self._aconn)

        try:
            if self.args.run:
                try:
                    return await self.args.run(self)
                except KeyboardInterrupt:
                    logger.warning("Выполнение прервано пользователем!")
                except api.errors.CaptchaRequired as ex:
                    logger.error(f"Требуется ввод капчи: {ex.captcha_url}")
                except api.errors.InternalServerError:
                    logger.error(
                        "Сервер HH.RU не смог обработать запрос из-за высокой"
                        " нагрузки или по иной причине"
                    )
                except api.errors.Forbidden:
                    logger.error("Требуется авторизация")
                except psycopg.Error as ex:
                    logger.exception(ex)
                    logger.warning("Ошибка базы данных (Postgres).")
                except Exception as e:
                    logger.exception(e)
                finally:
                    # Токен мог автоматически обновиться
                    if self.save_token():
                        logger.info("Токен был сохранен после обновления.")
                return 1
            self._parser.print_help(file=sys.stderr)
            return 2
        finally:
            try:
                await self._check_system()
            except Exception:
                pass
            try:
                await self.api_client.aclose()
            except Exception:
                pass
            try:
                await self._aconn.close()
            except Exception:
                pass

    def _parse_args(self, argv) -> None:
        self._parser = self._create_parser()
        self.args = self._parser.parse_args(argv, namespace=BaseNamespace())


def main(argv: Sequence[str] | None = None) -> None | int:
    return HHApplicantTool(argv).run()
