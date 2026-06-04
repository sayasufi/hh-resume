from __future__ import annotations

from logging import getLogger
from os import getenv
from typing import TYPE_CHECKING

from ..ai.openai import ChatOpenAI
from ..ai.openrouter import ChatOpenRouter

if TYPE_CHECKING:
    from ..main import HHApplicantTool

log = getLogger(__package__)


class ChatOpenAISupport:
    def get_openai_chat(
        self: HHApplicantTool,
        system_prompt: str,
    ) -> ChatOpenAI:
        c = self.config.get("openai", {})
        if not (token := c.get("token")):
            raise ValueError("Токен для OpenAI не задан")
        return ChatOpenAI(
            token=token,
            model=c.get("model"),
            temperature=c.get("temperature", 0.7),
            max_completion_tokens=c.get("max_completion_tokens", 1000),
            system_prompt=system_prompt,
            completion_endpoint=c.get("completion_endpoint"),
        )

    def get_openrouter_chat(
        self: HHApplicantTool,
        system_prompt: str,
    ) -> ChatOpenRouter:
        c = self.config.get("openrouter", {})
        if not (token := c.get("token")):
            raise ValueError("Токен для OpenRouter не задан")
        proxy_url = (
            c.get("proxy_url") or getenv("HTTPS_PROXY") or getenv("HTTP_PROXY")
        )
        return ChatOpenRouter(
            token=token,
            model=c.get("model", "x-ai/grok-4.1-fast"),
            referer=c.get("referer"),
            title=c.get("title"),
            system_prompt=system_prompt,
            proxy=proxy_url,
        )


class MegaTool(ChatOpenAISupport):
    # Телеметрия апстрима (отправка данных на сторонний сервер) и проверка
    # версии на PyPI отключены намеренно: приватность + автономный деплой.
    async def _check_system(self: HHApplicantTool) -> None:
        return None
