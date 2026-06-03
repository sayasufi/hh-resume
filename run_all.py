"""Запускает команду для ВСЕХ активных юзеров (ОДИН контейнер, мультиюзер).

Юзеры берутся из public.app_users. Для каждого запускается subprocess с
HH_DB_SCHEMA=<schema_юзера> — полная изоляция данных/токена на процесс.

Примеры (в crontab):
  python /app/run_all.py -- python -m hh_applicant_tool apply-similar
  python /app/run_all.py -- python /app/apply_tests.py --apply --limit 10
"""
import os
import subprocess
import sys

from hh_applicant_tool.storage import pgconn


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: run_all.py -- <command...>")
        sys.exit(2)

    admin_dsn = os.environ.get("HH_DB_DSN", "")
    users = pgconn.list_users_full()
    print(f"run_all: {len(users)} active users -> {' '.join(argv)}", flush=True)
    rc = 0
    for name, schema, db_password in users:
        env = dict(os.environ, HH_DB_SCHEMA=schema)
        # Per-tenant роль (#18): процесс юзера ходит в БД под ограниченной ролью
        # (видит только свою схему). Нет пароля → фолбэк на admin-DSN (откат).
        if db_password and admin_dsn:
            env["HH_DB_DSN"] = pgconn.tenant_dsn(admin_dsn, schema, db_password)
            role_note = f"role={schema}"
        else:
            role_note = "role=admin(fallback)"
        print(f"=== [{name}] schema={schema} {role_note} ===", flush=True)
        try:
            r = subprocess.run(argv, env=env)
            if r.returncode:
                rc = r.returncode
        except Exception as e:
            print(f"  [{name}] ошибка запуска: {e!r}")
            rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
