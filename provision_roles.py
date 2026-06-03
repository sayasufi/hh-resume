"""Провижин per-tenant PG-ролей (#18). Запускать ПОД admin-ролью (hh).

Для каждого активного юзера из public.app_users:
  • создаёт схему + таблицы (TABLES_DDL),
  • создаёт/обновляет login-роль с именем = имя схемы (u_egor/u_lexa),
  • выдаёт роли доступ ТОЛЬКО к её схеме (USAGE + DML, без CREATE),
  • генерирует и сохраняет пароль в public.app_users.db_password (читает только
    admin: tenant-ролям SELECT на app_users НЕ выдаётся).

Идемпотентно и реентерабельно. Пароль генерится один раз и переиспользуется.
Откат: `UPDATE public.app_users SET db_password=NULL;` — run_all вернётся на admin-DSN.

Запуск:  docker exec hh_applicant_tool python /app/provision_roles.py
"""
import secrets

import psycopg
from psycopg import sql

from hh_applicant_tool.storage.pgconn import (
    TABLES_DDL,
    _ensure_app_users,
    get_dsn,
)


def quote_ident(name: str) -> str:
    # схемы/роли — из нашего кода (u_egor/u_lexa), но всё равно экранируем кавычки
    return '"' + name.replace('"', '""') + '"'


def main() -> None:
    conn = psycopg.connect(get_dsn())
    conn.autocommit = True  # CREATE ROLE / ALTER DEFAULT PRIVILEGES — вне явных txn
    try:
        with conn.cursor() as cur:
            _ensure_app_users(cur)
            cur.execute(
                "SELECT name, schema, db_password FROM public.app_users "
                "WHERE active ORDER BY id"
            )
            rows = cur.fetchall()

            for name, schema, pw in rows:
                role = schema
                qs = quote_ident(schema)
                qr = quote_ident(role)

                # 1) схема + таблицы (как admin/owner = hh)
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {qs}")
                cur.execute(f"SET search_path TO {qs}")
                cur.execute(TABLES_DDL)
                cur.execute("SET search_path TO public")

                # 2) пароль — генерим один раз, дальше переиспользуем
                if not pw:
                    pw = secrets.token_urlsafe(24)
                    cur.execute(
                        "UPDATE public.app_users SET db_password=%s "
                        "WHERE schema=%s",
                        (pw, schema),
                    )

                # 3) login-роль (создать или обновить пароль). CREATE/ALTER ROLE —
                #    utility-стейтменты, НЕ принимают bind-параметры → инлайним
                #    пароль безопасно через sql.Literal (экранирование psycopg).
                verb = "ALTER"
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
                if not cur.fetchone():
                    verb = "CREATE"
                cur.execute(
                    sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(
                        sql.SQL(verb), sql.Identifier(role), sql.Literal(pw)
                    )
                )

                # 4) гранты ТОЛЬКО на свою схему (без CREATE → не плодит таблицы;
                #    к чужим u_*-схемам и к public.app_users доступа нет по умолчанию)
                cur.execute(f"GRANT USAGE ON SCHEMA {qs} TO {qr}")
                cur.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA {qs} TO {qr}"
                )
                cur.execute(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {qs} TO {qr}"
                )
                # будущие таблицы/секвенции (если admin создаст) — авто-гранты
                cur.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {qs} "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {qr}"
                )
                cur.execute(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {qs} "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {qr}"
                )
                print(f"provisioned: {name} schema={schema} role={role}", flush=True)

        print("done", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
