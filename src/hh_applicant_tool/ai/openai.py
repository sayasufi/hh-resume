import logging
from dataclasses import KW_ONLY, dataclass

import httpx

from .base import AIError

logger = logging.getLogger(__package__)


DEFAULT_COMPLETION_ENDPOINT = "https://api.openai.com/v1/chat/completions"


class OpenAIError(AIError):
    pass


@dataclass
class ChatOpenAI:
    token: str
    _: KW_ONLY
    system_prompt: str | None = None
    timeout: float = 30.0
    temperature: float = 0.7
    max_completion_tokens: int = 1000
    model: str | None = None
    completion_endpoint: str = None
    proxy: str | None = None

    def __post_init__(self) -> None:
        self.completion_endpoint = (
            self.completion_endpoint or DEFAULT_COMPLETION_ENDPOINT
        )

    _resolved_model: str | None = None

    def _default_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _resolve_model(self, client: "httpx.AsyncClient") -> str | None:
        """Если model == 'auto' (или пусто) — берём текущую модель с сервера
        (/v1/models). Так не нужно хардкодить имя: меняешь модель в vLLM —
        бот подхватывает сам."""
        if self.model and self.model != "auto":
            return self.model
        if self._resolved_model:
            return self._resolved_model
        base = self.completion_endpoint.rsplit("/chat/completions", 1)[0]
        try:
            r = await client.get(
                base + "/models", headers=self._default_headers()
            )
            r.raise_for_status()
            ids = [m["id"] for m in r.json().get("data", [])]
            self._resolved_model = ids[0] if ids else None
        except Exception:
            self._resolved_model = None
        return self._resolved_model

    async def send_message(self, message: str) -> str:
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": message})

        payload = {
            "messages": messages,
            "temperature": self.temperature,
            "max_completion_tokens": self.max_completion_tokens,
        }

        try:
            async with httpx.AsyncClient(
                proxy=self.proxy, timeout=self.timeout
            ) as client:
                model = await self._resolve_model(client)
                if not model:
                    raise OpenAIError(
                        "LLM недоступна: не удалось определить модель "
                        "(vLLM пуст/недоступен)"
                    )
                payload["model"] = model
                response = await client.post(
                    self.completion_endpoint,
                    json=payload,
                    headers=self._default_headers(),
                )
                response.raise_for_status()
                data = response.json()
            if "error" in data:
                raise OpenAIError(data["error"]["message"])
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPError as ex:
            raise OpenAIError(f"Network error: {ex}") from ex
