"""Use the optional Telegram proxy only for Telegram connections."""
import os
from urllib.parse import unquote, urlsplit


def telegram_proxy() -> str | None:
    return os.getenv("TELEGRAM_PROXY") or None


def bot_session():
    from aiogram.client.session.aiohttp import AiohttpSession

    proxy = telegram_proxy()
    return AiohttpSession(proxy=proxy) if proxy else None


def telethon_proxy() -> dict | None:
    proxy = telegram_proxy()
    if not proxy:
        return None
    url = urlsplit(proxy)
    if url.scheme not in ("http", "socks4", "socks5") or not url.hostname:
        raise ValueError("TELEGRAM_PROXY must be an HTTP or SOCKS proxy URL")
    return {
        "proxy_type": url.scheme,
        "addr": url.hostname,
        "port": url.port or (80 if url.scheme == "http" else 1080),
        "username": unquote(url.username) if url.username else None,
        "password": unquote(url.password) if url.password else None,
    }
