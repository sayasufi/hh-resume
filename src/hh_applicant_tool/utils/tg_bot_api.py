"""Минимальный синхронный вызов Telegram Bot API (без aiogram): отправить сообщение с
inline-кнопками из джобы. Ответы на кнопки/реплаи обрабатывает общий бот (tg_connect_bot)."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__package__)


def send_message(token: str, chat_id, text: str, reply_markup: dict | None = None) -> int | None:
    """message_id отправленного сообщения или None (ошибка — не роняем джобу)."""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True,
                  **({"reply_markup": reply_markup} if reply_markup else {})},
            timeout=20,
        )
        data = r.json()
        if not data.get("ok"):
            logger.warning("telegram sendMessage: %s", str(data)[:200])
            return None
        return int(data["result"]["message_id"])
    except Exception as ex:
        logger.warning("telegram sendMessage failed: %r", ex)
        return None
