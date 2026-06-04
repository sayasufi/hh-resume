# Этот модуль можно использовать как образец для других
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
    """Проверить прокси"""

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        pass

    async def run(self, applicant_tool: HHApplicantTool) -> None:
        import httpx

        proxy = applicant_tool._proxy_url()
        assert proxy, "Прокси не заданы"
        async with httpx.AsyncClient(proxy=proxy, timeout=15) as client:
            r = await client.get("https://icanhazip.com")
            print(r.text)
