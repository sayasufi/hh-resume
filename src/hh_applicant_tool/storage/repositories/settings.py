import json as _json
from typing import TypeVar

from ..models.setting import SettingModel
from .base import BaseRepository

Default = TypeVar("Default")


class SettingsRepository(BaseRepository):
    """settings — общая таблица, разделение по колонке account (env HH_ACCOUNT).
    Значения хранятся json-кодированными (как в SettingModel / pgconn.get_setting):
    set_value json-кодирует, get_value json-декодирует (с фолбэком на сырое)."""

    __table__ = "settings"
    pkey: str = "key"
    model = SettingModel

    @staticmethod
    def _acc() -> str:
        from ..pgconn import get_account
        return get_account()

    async def get_value(
        self, key: str, /, default: Default = None
    ) -> str | Default:
        from .. import _cfgmap as M
        acc = self._acc()
        kind = M.resolve_setting(key)
        async with self.conn.cursor() as cur:
            if kind[0] == "feature":
                await cur.execute("SELECT enabled FROM user_features WHERE account=%s AND feature=%s", (acc, kind[1]))
                r = await cur.fetchone()
                return bool(r[0]) if r else default
            if kind[0] == "health":
                await cur.execute("SELECT ts, ok, detail FROM health WHERE account=%s AND feature=%s", (acc, kind[1]))
                r = await cur.fetchone()
                return {"ts": r[0], "ok": r[1], "detail": r[2]} if r else default
            if kind[0] == "users":
                await cur.execute(f"SELECT {kind[1]} FROM users WHERE account=%s", (acc,))
                r = await cur.fetchone()
                return r[0] if (r and r[0] is not None) else default
            await cur.execute("SELECT value FROM settings WHERE account=%s AND key=%s", (acc, key))
            row = await cur.fetchone()
        if not row:
            return default
        try:
            return _json.loads(row[0])
        except (ValueError, TypeError):
            return row[0]

    async def set_value(
        self, key: str, value: str, /, commit: bool | None = None
    ) -> None:
        from .. import _cfgmap as M
        acc = self._acc()
        kind = M.resolve_setting(key)
        async with self.conn.cursor() as cur:
            if kind[0] == "feature":
                await cur.execute(
                    "INSERT INTO user_features(account, feature, enabled) VALUES (%s, %s, %s) "
                    "ON CONFLICT(account, feature) DO UPDATE SET enabled=excluded.enabled",
                    (acc, kind[1], bool(value)),
                )
            elif kind[0] == "health":
                v = value if isinstance(value, dict) else {}
                await cur.execute(
                    "INSERT INTO health(account, feature, ts, ok, detail) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT(account, feature) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, detail=excluded.detail",
                    (acc, kind[1], v.get("ts"), v.get("ok"), str(v.get("detail") or "")[:300]),
                )
            elif kind[0] == "users":
                col = kind[1]
                await cur.execute(
                    f"INSERT INTO users(account, {col}) VALUES (%s, %s) "
                    f"ON CONFLICT(account) DO UPDATE SET {col}=excluded.{col}, updated_at=now()",
                    (acc, M.coerce_user(col, value)),
                )
            else:
                await cur.execute(
                    "INSERT INTO settings(account, key, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT(account, key) DO UPDATE SET value=excluded.value",
                    (acc, key, _json.dumps(value, ensure_ascii=False)),
                )
        await self.maybe_commit(commit)

    async def delete_value(
        self, key: str, /, commit: bool | None = None
    ) -> None:
        from .. import _cfgmap as M
        acc = self._acc()
        kind = M.resolve_setting(key)
        async with self.conn.cursor() as cur:
            if kind[0] == "feature":
                await cur.execute("DELETE FROM user_features WHERE account=%s AND feature=%s", (acc, kind[1]))
            elif kind[0] == "health":
                await cur.execute("DELETE FROM health WHERE account=%s AND feature=%s", (acc, kind[1]))
            elif kind[0] == "users":
                await cur.execute(f"UPDATE users SET {kind[1]}=NULL WHERE account=%s", (acc,))
            else:
                await cur.execute("DELETE FROM settings WHERE account=%s AND key=%s", (acc, key))
        await self.maybe_commit(commit)
