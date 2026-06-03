from __future__ import annotations

import asyncio
import dataclasses
import json as _json
import logging
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal, TypeVar
from urllib.parse import urlencode, urljoin

import httpx

from hh_applicant_tool.api.user_agent import generate_android_useragent

from . import errors
from .client_keys import (
    ANDROID_CLIENT_ID,
    ANDROID_CLIENT_SECRET,
)
from .datatypes import AccessToken

__all__ = ("ApiClient", "OAuthClient")

HH_API_URL = "https://api.hh.ru/"
HH_OAUTH_URL = "https://hh.ru/oauth/"
DEFAULT_DELAY = 0.345

AllowedMethods = Literal["GET", "POST", "PUT", "DELETE"]
T = TypeVar("T")


logger = logging.getLogger(__package__)


@dataclass
class BaseClient:
    base_url: str
    _: dataclasses.KW_ONLY
    user_agent: str | None = None
    client: httpx.AsyncClient | None = None
    proxy: str | None = None
    delay: float | None = None
    _previous_request_time: float = 0.0

    def __post_init__(self) -> None:
        assert self.base_url.endswith("/"), "base_url must ends with /"
        self.delay = self.delay or DEFAULT_DELAY
        self.user_agent = self.user_agent or generate_android_useragent()
        if not self.client:
            logger.debug("create new httpx.AsyncClient")
            self.client = httpx.AsyncClient(
                proxy=self.proxy, timeout=30.0, follow_redirects=False
            )
        self.lock = asyncio.Lock()

    @property
    def proxies(self):
        # совместимость со старым кодом (некоторые места читали .proxies)
        return {"https": self.proxy, "http": self.proxy} if self.proxy else {}

    def _default_headers(self) -> dict[str, str]:
        return {
            "user-agent": self.user_agent,
            "x-hh-app-active": "true",
        }

    async def request(
        self,
        method: AllowedMethods,
        endpoint: str,
        params: dict[str, Any] | None = None,
        delay: float | None = None,
        as_json: bool = False,
        **kwargs: Any,
    ) -> T:
        assert method in AllowedMethods.__args__
        params = dict(params or {})
        params.update(kwargs)
        url = self.resolve_url(endpoint)
        async with self.lock:
            wait = (
                (self.delay if delay is None else delay)
                - time.monotonic()
                + self._previous_request_time
            )
            if wait > 0:
                logger.debug("wait %fs before request", wait)
                await asyncio.sleep(wait)
            has_body = method in ["POST", "PUT"]
            req_kwargs: dict[str, Any] = {}
            if has_body:
                req_kwargs["json" if as_json else "data"] = params
            else:
                req_kwargs["params"] = params
            try:
                response = await self.client.request(
                    method,
                    url,
                    headers=self._default_headers(),
                    **req_kwargs,
                )
                try:
                    rv = response.json() if response.text else {}
                except _json.JSONDecodeError as ex:
                    raise errors.BadResponse(
                        f"Can't decode JSON: {method} {url} ({response.status_code})"
                    ) from ex
            finally:
                logger.debug(
                    "%s %s with params: %.1000s",
                    method,
                    url,
                    params or "-",
                )
                self._previous_request_time = time.monotonic()
        errors.ApiError.raise_for_status(response, rv)
        assert 300 > response.status_code >= 200, (
            f"Unexpected status code for {method} {url}: {response.status_code}"
        )
        return rv

    async def get(self, *args, **kwargs) -> T:
        return await self.request("GET", *args, **kwargs)

    async def post(self, *args, **kwargs) -> T:
        return await self.request("POST", *args, **kwargs)

    async def put(self, *args, **kwargs) -> T:
        return await self.request("PUT", *args, **kwargs)

    async def delete(self, *args, **kwargs) -> T:
        return await self.request("DELETE", *args, **kwargs)

    async def aclose(self) -> None:
        if self.client:
            await self.client.aclose()

    def resolve_url(self, url: str) -> str:
        return urljoin(self.base_url, url.lstrip("/"))


@dataclass
class OAuthClient(BaseClient):
    client_id: str | None = None
    client_secret: str | None = None
    _: dataclasses.KW_ONLY
    base_url: str = HH_OAUTH_URL
    state: str = ""
    scope: str = ""
    redirect_uri: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.client_id = self.client_id or ANDROID_CLIENT_ID
        self.client_secret = self.client_secret or ANDROID_CLIENT_SECRET

    @property
    def authorize_url(self) -> str:
        params = dict(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            scope=self.scope,
            state=self.state,
        )
        params_qs = urlencode({k: v for k, v in params.items() if v})
        return self.resolve_url(f"/authorize?{params_qs}")

    async def request_access_token(
        self, endpoint: str, params: dict[str, Any] | None = None, **kw: Any
    ) -> AccessToken:
        tok = await self.post(endpoint, params, **kw)
        return {
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token"),
            "access_expires_at": int(time.time()) + tok.pop("expires_in", 0),
        }

    async def authenticate(self, code: str) -> AccessToken:
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        return await self.request_access_token("/token", params)

    async def refresh_access_token(self, refresh_token: str) -> AccessToken:
        return await self.request_access_token(
            "/token",
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )


@dataclass
class ApiClient(BaseClient):
    access_token: str | None = None
    refresh_token: str | None = None
    access_expires_at: int = 0
    _: dataclasses.KW_ONLY
    client_id: str | None = None
    client_secret: str | None = None
    base_url: str = HH_API_URL
    # Координатор обновления токена (pgconn.locked_token_refresh) — под advisory-lock.
    # Если задан, refresh_access_token делегирует ему (защита от гонки + сохранение в PG).
    refresh_hook: Any = None
    # Выставляется хуком, когда токен уже записан в PG под локом — тогда внешнему
    # save_token повторная запись не нужна (см. #7: убираем вторую транзакцию).
    _token_persisted: bool = False

    @property
    def is_access_expired(self) -> bool:
        return time.time() >= (self.access_expires_at or 0)

    @cached_property
    def oauth_client(self) -> OAuthClient:
        return OAuthClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            user_agent=self.user_agent,
            client=self.client,
        )

    def _default_headers(self) -> dict[str, str]:
        headers = super()._default_headers()
        if not self.access_token:
            return headers
        # Раньше был assert (#22): он ронял процесс и отключается под `python -O`.
        # Мягкая проверка — HH-токены начинаются с 'USER'; иначе просто предупреждаем,
        # но запрос всё равно уходит (пусть HH сам вернёт 403, если токен битый).
        if not self.access_token.startswith("USER"):
            logger.warning(
                "access_token не начинается с 'USER' (len=%d) — возможно повреждён",
                len(self.access_token),
            )
        return headers | {"authorization": f"Bearer {self.access_token}"}

    async def request(
        self,
        method: AllowedMethods,
        endpoint: str,
        params: dict[str, Any] | None = None,
        delay: float | None = None,
        as_json: bool = False,
        **kwargs: Any,
    ) -> T:
        async def do_request():
            return await BaseClient.request(
                self, method, endpoint, params, delay, as_json, **kwargs
            )

        try:
            return await do_request()
        except errors.Forbidden as ex:
            if not self.is_access_expired or not self.refresh_token:
                raise ex
            logger.info("try to refresh access_token")
            await self.refresh_access_token()
            return await do_request()

    def handle_access_token(self, token: AccessToken) -> None:
        for f in ("access_token", "refresh_token", "access_expires_at"):
            if f in token and hasattr(self, f):
                setattr(self, f, token[f])

    async def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise ValueError("Refresh token required.")
        # Если задан координатор — обновляем под advisory-lock с перечитыванием
        # токена из PG (защита от гонки одновременных refresh).
        if self.refresh_hook is not None:
            await self.refresh_hook(self)
            return
        token = await self.oauth_client.refresh_access_token(self.refresh_token)
        self.handle_access_token(token)

    def get_access_token(self) -> AccessToken:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": self.access_expires_at,
        }
