"""Анти-бот джиттер: случайная задержка перед стартом флоу."""
import asyncio
import secrets


async def human_jitter(max_seconds: int) -> None:
    if max_seconds and max_seconds > 0:
        await asyncio.sleep(secrets.randbelow(max_seconds + 1))
