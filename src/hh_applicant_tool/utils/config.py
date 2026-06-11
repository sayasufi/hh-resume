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
        # читаем из нормализованной таблицы users (через pgconn.app_config);
        # web_state (~650KB Playwright storage_state) утилите не нужен — выкидываем.
        from ..storage.pgconn import app_config, get_account

        cfg = app_config(get_account())
        cfg.pop("web_state", None)
        with self._lock:
            self.update(cfg)

    def save(self, *args: Any, **kwargs: Any) -> None:
        # пишем через pgconn.set_app_config -> нормализованная таблица users
        # (маршрутизация ключ->колонка в _cfgmap, единый источник).
        from ..storage.pgconn import set_app_config, get_account

        changed = dict(*args, **kwargs)
        self.update(changed)
        items = changed.items() if changed else self.items()
        acc = get_account()
        for key, value in items:
            set_app_config(key, value, acc)

    __getitem__ = dict.get

    def __repr__(self) -> str:
        return f"Config(pg:{getenv('HH_DB_SCHEMA', 'public')})"
