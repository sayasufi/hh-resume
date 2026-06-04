from functools import wraps

import psycopg


class RepositoryError(psycopg.Error):
    pass


def wrap_db_errors(func):
    """Async-обёртка ошибок БД для корутин-методов репозитория.
    При ошибке откатывает транзакцию, чтобы соединение не осталось
    в состоянии 'transaction aborted' (иначе следующий запрос на том же
    conn упадёт 'current transaction is aborted')."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except psycopg.Error as e:
            try:
                conn = getattr(args[0], "conn", None)
                if conn is not None:
                    await conn.rollback()
            except Exception:
                pass
            raise RepositoryError(
                f"Database error in {func.__name__}: {e}"
            ) from e

    return wrapper
