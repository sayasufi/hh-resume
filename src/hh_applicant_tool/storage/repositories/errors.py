from functools import wraps

import psycopg


class RepositoryError(psycopg.Error):
    pass


def wrap_db_errors(func):
    """Async-обёртка ошибок БД для корутин-методов репозитория."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except psycopg.Error as e:
            raise RepositoryError(
                f"Database error in {func.__name__}: {e}"
            ) from e

    return wrapper
