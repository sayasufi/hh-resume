from __future__ import annotations

import logging

import psycopg

logger: logging.Logger = logging.getLogger(__package__)


def init_db(conn: psycopg.Connection) -> None:
    """Схема/таблицы создаются в pgconn.connect() (идемпотентно), поэтому здесь
    ничего делать не нужно."""
    return None


def list_migrations() -> list[str]:
    return []


def apply_migration(conn: psycopg.Connection, name: str) -> None:
    logger.warning("Миграции не поддерживаются на Postgres-бэкенде: %s", name)
