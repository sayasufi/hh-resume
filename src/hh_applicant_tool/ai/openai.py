import asyncio
import logging
from dataclasses import KW_ONLY, dataclass

import httpx

from .base import AIError

logger = logging.getLogger(__package__)


DEFAULT_COMPLETION_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# --- глобальный (на весь сервис) лимит одновременных LLM-запросов ---
# Кросс-процессный: все процессы/контейнеры/flow-раны делят N слотов через
# Postgres advisory-locks. Число слотов берётся из настройки llm.max_concurrent
# (_global, дефолт 8). Fail-open: если БД недоступна — НЕ блокируем LLM.
_LLM_NS = 919191  # namespace для pg_advisory_lock (int4)
_WAIT_TIMEOUT = 90.0  # сколько ждать свободный слот, потом идём без лимита


def _max_concurrent() -> int:
    try:
        from hh_applicant_tool.storage import pgconn
        return max(1, int(pgconn.get_setting("llm.max_concurrent", "8", account="_global")))
    except Exception:
        return 8


def _sync_acquire(n):
    """Возврат: conn (с ._llm_slot) если слот взят; 'busy' если все заняты; None при ошибке БД (fail-open)."""
    try:
        from hh_applicant_tool.storage import pgconn
        conn = pgconn.connect()
    except Exception:
        return None
    try:
        cur = conn.cursor()
        for slot in range(n):
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LLM_NS, slot))
            if cur.fetchone()[0]:
                conn._llm_slot = slot
                return conn
        conn.close()
        return "busy"
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


def _sync_release(conn):
    try:
        slot = getattr(conn, "_llm_slot", None)
        if slot is not None:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", (_LLM_NS, slot))
            conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def _acquire_llm_slot():
    """Ждёт свободный слот. Возврат: conn-объект (освободить через _sync_release) либо None (без лимита)."""
    try:
        n = await asyncio.to_thread(_max_concurrent)
        loop = asyncio.get_event_loop()
        start = loop.time()
        waited = False
        while True:
            res = await asyncio.to_thread(_sync_acquire, n)
            if res is None:            # БД недоступна -> fail-open
                return None
            if res == "busy":
                if loop.time() - start > _WAIT_TIMEOUT:
                    logger.warning("LLM-лимит: ждали слот >%ss — идём без лимита", int(_WAIT_TIMEOUT))
                    return None
                if not waited:
                    logger.debug("LLM-лимит: все %s слотов заняты, ждём", n)
                    waited = True
                await asyncio.sleep(0.3)
                continue
            return res                 # conn со слотом
    except Exception:
        return None


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

        _slot = await _acquire_llm_slot()  # глобальный лимит одновременных запросов
        try:
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
        finally:
            if _slot is not None:
                await asyncio.to_thread(_sync_release, _slot)
