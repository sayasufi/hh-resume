"""Активные цели для fan-out, обобщённо по платформам (spec §4.5).
Сейчас единственная платформа — hh (маппится на public.app_users)."""
from hh_applicant_tool.storage import pgconn

# Платформа -> переменная окружения контекста для сабпроцесса операции.
PLATFORM_ENV: dict[str, str] = {"hh": "HH_ACCOUNT"}


def active_targets(platform: str, feature: str | None = None) -> list[str]:
    """Идентификаторы целей для (платформа, фича). Для hh — аккаунты из app_users,
    отфильтрованные по feat.<feature> (default True). Новые платформы добавляют
    свою ветку, не трогая остальной оркестратор."""
    if platform == "hh":
        targets = [account for _name, account in pgconn.list_users()]
        if feature:
            targets = [t for t in targets if pgconn.feature_enabled(feature, account=t)]
        return targets
    raise ValueError(f"unknown platform: {platform}")
