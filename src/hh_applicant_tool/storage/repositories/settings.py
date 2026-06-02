from typing import TypeVar

from ..models.setting import SettingModel
from .base import BaseRepository

Default = TypeVar("Default")


class SettingsRepository(BaseRepository):
    __table__ = "settings"
    pkey: str = "key"
    model = SettingModel

    async def get_value(
        self,
        key: str,
        /,
        default: Default = None,
    ) -> str | Default:
        setting = await self.get(key)
        return setting.value if setting else default

    async def set_value(
        self,
        key: str,
        value: str,
        /,
        commit: bool | None = None,
    ) -> None:
        await self.save(self.model(key=key, value=value), commit=commit)

    async def delete_value(
        self, key: str, /, commit: bool | None = None
    ) -> None:
        setting = await self.get(key)
        if setting:
            await self.delete(setting, commit=commit)
