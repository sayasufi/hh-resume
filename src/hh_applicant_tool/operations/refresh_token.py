from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    pass


class Operation(BaseOperation):
    """Получает новый access_token."""

    __aliases__ = ["refresh"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        pass

    async def run(self, tool: HHApplicantTool) -> None:
        if tool.api_client.is_access_expired:
            # refresh_access_token идёт через locked_token_refresh (advisory-lock):
            # он сам сохраняет токен в PG под локом. save_token — фолбэк для пути
            # без хука; его False здесь означает «уже сохранено», а не ошибку (#7).
            await tool.api_client.refresh_access_token()
            tool.save_token()
            print("✅ Токен успешно обновлен.")
        else:
            # logger.debug("Токен валиден, игнорируем обновление.")
            print("ℹ️ Токен не истек, обновление не требуется.")
