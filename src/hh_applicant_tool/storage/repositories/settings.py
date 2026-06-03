from typing import TypeVar

from ..models.setting import SettingModel
from .base import BaseRepository

Default = TypeVar("Default")


class SettingsRepository(BaseRepository):
    """settings — общая таблица, разделение по колонке account (env HH_ACCOUNT).
    get/set/delete account-aware (минуя дженерик BaseRepository.get/save)."""

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
        async with self.conn.cursor() as cur:
            await cur.execute(
                "SELECT value FROM settings WHERE account=%s AND key=%s",
                (self._acc(), key),
            )
            row = await cur.fetchone()
        return row[0] if row else default

    async def set_value(
        self, key: str, value: str, /, commit: bool | None = None
    ) -> None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO settings(account, key, value) VALUES (%s, %s, %s) "
                "ON CONFLICT(account, key) DO UPDATE SET value=excluded.value",
                (self._acc(), key, value),
            )
        await self.maybe_commit(commit)

    async def delete_value(
        self, key: str, /, commit: bool | None = None
    ) -> None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM settings WHERE account=%s AND key=%s",
                (self._acc(), key),
            )
        await self.maybe_commit(commit)
