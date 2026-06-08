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
        ac = tool.api_client
        # Аккаунт без hh-токена (напр. аккаунт-краулер) — обновлять нечего. Выходим ЧИСТО
        # (rc=0), а не падаем rc=1 и не шумим в логах оркестратора.
        if not (getattr(ac, "access_token", None) or getattr(ac, "refresh_token", None)):
            print("ℹ️ Нет hh-токена — обновлять нечего (не hh-аккаунт).")
            return
        if ac.is_access_expired:
            # refresh_access_token идёт через locked_token_refresh (advisory-lock):
            # он сам сохраняет токен в PG под локом. save_token — фолбэк для пути
            # без хука; его False здесь означает «уже сохранено», а не ошибку (#7).
            await ac.refresh_access_token()
            tool.save_token()
            print("✅ Токен успешно обновлен.")
        else:
            # logger.debug("Токен валиден, игнорируем обновление.")
            print("ℹ️ Токен не истек, обновление не требуется.")
