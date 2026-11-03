"""Запуск существующей операции как subprocess для одной цели (платформа+target),
со стримингом вывода в логи Prefect и пробросом ненулевого кода как ошибки.
Контекст цели выставляется через PLATFORM_ENV (hh -> HH_ACCOUNT)."""
import asyncio
import os
import signal

from prefect import get_run_logger

from .targets import PLATFORM_ENV


async def run_op(command: list[str], platform: str, target: str, timeout: int = 1800) -> int:
    logger = get_run_logger()
    ctx_env = PLATFORM_ENV.get(platform)
    if not ctx_env:
        raise ValueError(f"no context env mapping for platform: {platform}")
    env = {**os.environ, ctx_env: target}
    env.pop("HH_DB_SCHEMA", None)  # единая схема: изоляция по контексту цели
    # /app/services на пути — для сиблинг-импортов перенесённых скриптов
    env["PYTHONPATH"] = "/app/services" + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    assert proc.stdout is not None

    async def _pump() -> None:
        async for raw in proc.stdout:
            logger.info("[%s/%s] %s", platform, target, raw.decode("utf-8", "replace").rstrip())

    try:
        await asyncio.wait_for(asyncio.gather(_pump(), proc.wait()), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError(
            f"{' '.join(command)} (platform={platform} target={target}) timed out after {timeout}s"
        ) from exc
    rc = proc.returncode or 0
    if rc != 0:
        raise RuntimeError(
            f"{' '.join(command)} (platform={platform} target={target}) exited rc={rc}"
        )
    return rc
