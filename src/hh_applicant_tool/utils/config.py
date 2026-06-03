from __future__ import annotations

import platform
from functools import cache
from os import getenv
from pathlib import Path
from threading import Lock
from typing import Any


@cache
def get_config_path() -> Path:
    match platform.system():
        case "Windows":
            return Path(getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
        case "Darwin":
            return Path.home() / "Library" / "Application Support"
        case _:
            return Path(getenv("XDG_CONFIG_HOME", Path.home() / ".config"))


class Config(dict):
    """Конфиг, хранящийся в Postgres (таблица app_config: key text, value jsonb)
    в схеме текущего юзера (HH_DB_SCHEMA). Совместим со старым API: .get(),
    config["key"] (None если нет), .save(key=value)."""

    def __init__(self, config_path: str | Path | None = None):
        # config_path игнорируется (оставлен для совместимости вызова)
        self._lock = Lock()
        self.load()

    def load(self) -> None:
        from ..storage.pgconn import connect, get_account

        conn = connect()
        try:
            with conn.cursor() as cur:
                # web_state (~650KB Playwright storage_state) не нужен утилите —
                # его читает только apply_tests через pgconn.app_config(). Не тянем.
                cur.execute(
                    "SELECT key, value FROM app_config "
                    "WHERE account=%s AND key <> 'web_state'",
                    (get_account(),),
                )
                with self._lock:
                    for key, value in cur.fetchall():
                        self[key] = value
        finally:
            conn.close()

    def save(self, *args: Any, **kwargs: Any) -> None:
        import json as _json

        from ..storage.pgconn import connect, get_account

        changed = dict(*args, **kwargs)
        self.update(changed)
        items = changed.items() if changed else self.items()
        acc = get_account()
        conn = connect()
        try:
            with conn.cursor() as cur:
                for key, value in items:
                    cur.execute(
                        "INSERT INTO app_config(account, key, value) "
                        "VALUES (%s, %s, %s::jsonb) ON CONFLICT(account, key) "
                        "DO UPDATE SET value = excluded.value, updated_at = now()",
                        (acc, key, _json.dumps(value, ensure_ascii=False)),
                    )
            conn.commit()
        finally:
            conn.close()

    __getitem__ = dict.get

    def __repr__(self) -> str:
        return f"Config(pg:{getenv('HH_DB_SCHEMA', 'public')})"
