import logging
from dataclasses import dataclass
from typing import ClassVar

import httpx

from .base import AIError

logger = logging.getLogger(__package__)


class OpenRouterError(AIError):
    pass


@dataclass
class ChatOpenRouter:
    chat_endpoint: ClassVar[str] = (
        "https://openrouter.ai/api/v1/chat/completions"
    )

    token: str
    model: str
    referer: str | None = None
    title: str | None = None
    system_prompt: str | None = None
    temperature: float = 0.7
    max_completion_tokens: int = 1000
    proxy: str | None = None

    def _default_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-Title"] = self.title
        return headers

    async def send_message(self, message: str) -> str:
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": message})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_completion_tokens": self.max_completion_tokens,
        }
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy, timeout=30.0
            ) as client:
                response = await client.post(
                    self.chat_endpoint,
                    json=payload,
                    headers=self._default_headers(),
                )
                response.raise_for_status()
                data = response.json()
            if "error" in data:
                raise OpenRouterError(
                    data["error"].get("message", "Unknown error")
                )
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPError as ex:
            raise OpenRouterError(f"Network error: {ex}") from ex
