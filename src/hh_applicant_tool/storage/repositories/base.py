from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, AsyncIterator, ClassVar, Mapping, Self, Type

import psycopg

from ..models.base import BaseModel
from .errors import wrap_db_errors

DEFAULT_PRIMARY_KEY = "id"

logger = logging.getLogger(__package__)


@dataclass
class BaseRepository:
    model: ClassVar[Type[BaseModel] | None] = None
    pkey: ClassVar[str] = DEFAULT_PRIMARY_KEY
    conflict_columns: ClassVar[tuple[str, ...] | None] = None
    update_excludes: ClassVar[tuple[str, ...]] = ("created_at", "updated_at")
    __table__: ClassVar[str | None] = None

    conn: psycopg.AsyncConnection
    auto_commit: bool = True

    @property
    def table_name(self) -> str:
        return self.__table__ or self.model.__name__

    @wrap_db_errors
    async def commit(self):
        await self.conn.commit()

    @wrap_db_errors
    async def rollback(self):
        await self.conn.rollback()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()
        return False

    async def maybe_commit(self, commit: bool | None = None) -> None:
        if commit is not None and commit or self.auto_commit:
            await self.commit()

    async def find(self, **kwargs: Any) -> AsyncIterator[BaseModel]:
        operators = {
            "lt": "<", "le": "<=", "gt": ">", "ge": ">=", "ne": "!=",
            "eq": "=", "like": "LIKE", "is": "IS", "is_not": "IS NOT",
            "in": "IN", "not_in": "NOT IN",
        }
        conditions = []
        sql_params: dict[str, Any] = {}
        for key, value in kwargs.items():
            try:
                key, op = key.rsplit("__", 1)
            except ValueError:
                op = "eq"
            if op in ("in", "not_in"):
                if not isinstance(value, (list, tuple)):
                    value = [value]
                in_placeholders = []
                for i, v in enumerate(value, 1):
                    p_name = f"{key}_{i}"
                    in_placeholders.append(f"%({p_name})s")
                    sql_params[p_name] = v
                conditions.append(
                    f"{key} {operators[op]} ({', '.join(in_placeholders)})"
                )
            else:
                sql_params[key] = value
                conditions.append(f"{key} {operators[op]} %({key})s")
        sql = f"SELECT * FROM {self.table_name}"
        if conditions:
            sql += f" WHERE {' AND '.join(conditions)}"
        sql += " ORDER BY ctid DESC;"
        results: list[BaseModel] = []
        try:
            cur = await self.conn.execute(sql, sql_params)
            cols = [c[0] for c in cur.description]
            rows = await cur.fetchall()
        except psycopg.Error:
            logger.warning("SQL ERROR: %s", sql)
            raise
        for row in rows:
            data = {col: value for col, value in zip(cols, row)}  # noqa: B905
            results.append(self.model.from_db(data))
        for r in results:
            yield r

    @wrap_db_errors
    async def get(self, pk: Any) -> BaseModel | None:
        # Прямой запрос по PK без ORDER BY (одна строка) — без лишней сортировки
        sql = f"SELECT * FROM {self.table_name} WHERE {self.pkey} = %s LIMIT 1"
        cur = await self.conn.execute(sql, (pk,))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        data = {col: value for col, value in zip(cols, row)}  # noqa: B905
        return self.model.from_db(data)

    @wrap_db_errors
    async def count_total(self) -> int:
        cur = await self.conn.execute(
            f"SELECT count(*) FROM {self.table_name};"
        )
        row = await cur.fetchone()
        return row[0]

    @wrap_db_errors
    async def delete(
        self, obj_or_pkey: Any, /, commit: bool | None = None
    ) -> None:
        sql = f"DELETE FROM {self.table_name} WHERE {self.pkey} = %s"
        pk_value = (
            getattr(obj_or_pkey, self.pkey)
            if isinstance(obj_or_pkey, BaseModel)
            else obj_or_pkey
        )
        await self.conn.execute(sql, (pk_value,))
        await self.maybe_commit(commit=commit)

    remove = delete

    @wrap_db_errors
    async def clear(self, commit: bool | None = None):
        await self.conn.execute(f"DELETE FROM {self.table_name};")
        await self.maybe_commit(commit)

    clean = clear

    async def _insert(
        self,
        data: Mapping[str, Any] | list[Mapping[str, Any]],
        /,
        batch: bool = False,
        upsert: bool = True,
        conflict_columns: Sequence[str] | None = None,
        update_excludes: Sequence[str] | None = None,
        commit: bool | None = None,
    ):
        conflict_columns = conflict_columns or self.conflict_columns
        update_excludes = update_excludes or self.update_excludes

        if batch and not data:
            return

        columns = list(dict(data[0] if batch else data).keys())
        placeholders = ", ".join(f"%({c})s" for c in columns)
        sql = (
            f"INSERT INTO {self.table_name} ({', '.join(columns)})"
            f" VALUES ({placeholders})"
        )

        if upsert:
            cols_set = set(columns)
            if conflict_columns:
                conflict_set = set(conflict_columns) & cols_set
            else:
                conflict_set = {self.pkey} & cols_set

            if conflict_set:
                sql += f" ON CONFLICT({', '.join(conflict_set)})"
                update_set = (
                    cols_set
                    - conflict_set
                    - {self.pkey}
                    - set(update_excludes or [])
                )
                if update_set:
                    update_clause = ", ".join(
                        f"{c} = excluded.{c}" for c in update_set
                    )
                    sql += f" DO UPDATE SET {update_clause}"
                else:
                    sql += " DO NOTHING"

        sql += ";"
        try:
            if batch:
                async with self.conn.cursor() as cur:
                    await cur.executemany(sql, list(data))
            else:
                await self.conn.execute(sql, data)
        except psycopg.Error:
            logger.warning("SQL ERROR: %s", sql)
            raise
        await self.maybe_commit(commit)

    @wrap_db_errors
    async def save(
        self,
        obj: BaseModel | Mapping[str, Any],
        /,
        **kwargs: Any,
    ) -> None:
        if isinstance(obj, Mapping):
            obj = self.model.from_api(obj)
        data = obj.to_db()
        await self._insert(data, **kwargs)

    @wrap_db_errors
    async def save_batch(
        self,
        items: list[BaseModel | Mapping[str, Any]],
        /,
        **kwargs: Any,
    ) -> None:
        if not items:
            return
        data = [
            (self.model.from_api(i) if isinstance(i, Mapping) else i).to_db()
            for i in items
        ]
        await self._insert(data, batch=True, **kwargs)
