"""Одноразовая миграция на единую схему: данные из схем u_egor/u_lexa переносятся
в общие таблицы public.* с колонкой account ('egor'/'lexa'). Старые схемы НЕ
трогаются (откат = старый код в git + они на месте). Идемпотентно по ON CONFLICT.

Запуск под admin: docker exec hh_applicant_tool python /app/migrate_single.py
"""
import psycopg

from hh_applicant_tool.storage import pgconn

ACCT = {"u_egor": "egor", "u_lexa": "lexa"}

# per-account таблицы: (колонки для копирования)
PER_ACCT = {
    "app_config": "key, value, updated_at",
    "settings": "key, value",
    "seen_keys": "kind, key, created_at",
    "notifications": "priority, category, text, link, dedup_key, created_at, sent_at",
    "action_items": "nid, chat_id, vacancy, action, chat_url, vacancy_url, created_at",
}
# общие кэш-таблицы (PK = hh-id, между аккаунтами не пересекаются)
CACHE = ["employers", "vacancies", "negotiations", "resumes", "vacancy_contacts"]


def main():
    conn = psycopg.connect(pgconn.get_dsn())
    cur = conn.cursor()
    cur.execute("SET search_path TO public")
    cur.execute(pgconn.TABLES_DDL)  # создать новые общие таблицы
    # app_users: добавить колонку account и заполнить из schema
    cur.execute("ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS account text")
    cur.execute(
        "UPDATE public.app_users SET account = "
        "CASE WHEN schema LIKE 'u\\_%%' THEN substring(schema from 3) ELSE schema END "
        "WHERE account IS NULL AND schema IS NOT NULL"
    )
    conn.commit()
    print("tables ensured, app_users.account filled")

    for schema, acct in ACCT.items():
        # есть ли такая схема
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
            (schema,),
        )
        if not cur.fetchone():
            print(f"  {schema}: нет схемы, пропуск")
            continue
        for tbl, cols in PER_ACCT.items():
            try:
                cur.execute(
                    f'INSERT INTO public.{tbl}(account, {cols}) '
                    f'SELECT %s, {cols} FROM "{schema}".{tbl} '
                    "ON CONFLICT DO NOTHING",
                    (acct,),
                )
                print(f"  {acct}.{tbl}: +{cur.rowcount}")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"  {acct}.{tbl}: ERR {repr(e)[:90]}")
        for tbl in CACHE:
            try:
                cur.execute(
                    f'INSERT INTO public.{tbl} SELECT * FROM "{schema}".{tbl} '
                    "ON CONFLICT DO NOTHING"
                )
                print(f"  cache {tbl} ({acct}): +{cur.rowcount}")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"  cache {tbl} ({acct}): ERR {repr(e)[:90]}")
    conn.close()
    print("migration done")


if __name__ == "__main__":
    main()
