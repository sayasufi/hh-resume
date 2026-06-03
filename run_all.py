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

    users = pgconn.list_users()
    print(f"run_all: {len(users)} active accounts -> {' '.join(argv)}", flush=True)
    rc = 0
    for name, account in users:
        # Единая схема: разделение по HH_ACCOUNT (см. pgconn.get_account).
        env = dict(os.environ, HH_ACCOUNT=account)
        env.pop("HH_DB_SCHEMA", None)
        print(f"=== [{name}] account={account} ===", flush=True)
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
