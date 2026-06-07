"""Единый источник маппинга legacy KV-ключей (app_config/settings) в нормализованные
таблицы (users / user_features / health / web_state). Чистый модуль без БД — его
импортируют ВСЕ аксессоры (sync pgconn/config.py + async SettingsRepository), чтобы
маршрутизация была в одном месте (DRY). SQL каждый аксессор выполняет сам (sync/async)."""

# settings-ключ -> колонка в users
USER_COL = {
    "user.full_name": "full_name", "user.email": "email", "user.phone": "phone",
    "auth.username": "auth_username", "auth.password": "auth_password", "auth.last_login": "auth_last_login",
    "apply.resume_id": "apply_resume_id", "apply.max_per_day": "apply_max_per_day",
    "apply.tests_per_day": "apply_tests_per_day", "apply.use_ai": "apply_use_ai",
    "apply.force_message": "apply_force_message", "apply.civil_law_only": "apply_civil_law_only",
    "apply.excluded_terms": "apply_excluded_terms",
    "getmatch.session": "getmatch_session", "getmatch.username": "getmatch_username",
    "getmatch.max_per_day": "getmatch_max_per_day",
    "habr.login": "habr_login", "habr.password": "habr_password", "habr.session": "habr_session",
    "habr.2captcha_key": "habr_2captcha_key", "habr.query": "habr_query", "habr.max_per_day": "habr_max_per_day",
    "tg.cats": "tg_cats", "reply.ignore_names": "reply_ignore_names",
    "_applications_count": "applications_count", "_applications_date": "applications_date",
    "_applications_pause_until": "applications_pause_until",
}
# app_config-ключ -> колонка в users (web_state отдельной таблицей)
APP_COL = {
    "token": "hh_token", "openai": "openai", "telegram": "telegram", "preferences": "preferences",
    "resume_text": "resume_text", "hh_phone": "hh_phone", "tg_user_id": "tg_user_id",
    "tg_user_session": "tg_user_session",
}
APP_JSONB = {"hh_token", "openai", "telegram", "preferences"}   # jsonb-колонки в users
USER_INT = {"getmatch_max_per_day", "habr_max_per_day", "auth_last_login",
            "apply_max_per_day", "apply_tests_per_day", "tg_user_id"}
USER_BOOL = {"apply_use_ai", "apply_force_message", "apply_civil_law_only"}

# порядок колонок для сборки app_config-словаря (ключ app_config -> колонка users)
APP_ORDER = list(APP_COL.items())   # [(app_key, col), ...]


def resolve_setting(key: str):
    """Куда направить settings-ключ: ('feature', name) | ('health', name) |
    ('users', col) | ('kv', key) (глобальное/незамапленное остаётся в settings)."""
    if key.startswith("feat."):
        return ("feature", key[5:])
    if key.startswith("_health."):
        return ("health", key[8:])
    col = USER_COL.get(key)
    if col:
        return ("users", col)
    return ("kv", key)


def coerce_user(col: str, value):
    """Приведение значения к типу колонки users."""
    if value is None:
        return None
    if col in USER_BOOL:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on", "да")
    if col in USER_INT:
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    if col in APP_JSONB:
        return value   # сериализацию в jsonb делает аксессор (json.dumps + ::jsonb)
    return value if isinstance(value, str) else str(value)
