"""Миграция данных одного юзера из SQLite/JSON в PG-схему.

ENV: HH_DB_DSN, HH_DB_SCHEMA (целевая схема), MIGRATE_CONFIG_DIR (каталог с
config.json + data(sqlite) + *_seen.json; по умолчанию /app/config).

Переносит: config.json -> app_config(jsonb); sqlite settings -> settings(verbatim);
actions_seen.json/tests_seen.json -> seen_keys. Кэш вакансий/негоциаций НЕ
переносится (перестраивается из API).
"""
import json
import os
import sqlite3

import psycopg

from hh_applicant_tool.storage.pgconn import TABLES_DDL

CONFIG_DIR = os.environ.get("MIGRATE_CONFIG_DIR", "/app/config")
DSN = os.environ["HH_DB_DSN"]
SCHEMA = os.environ["HH_DB_SCHEMA"]


def main():
    conn = psycopg.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        cur.execute(f'SET search_path TO "{SCHEMA}"')
        cur.execute(TABLES_DDL)
    conn.commit()

    # config.json -> app_config
    n_cfg = 0
    cfg_path = os.path.join(CONFIG_DIR, "config.json")
    if os.path.exists(cfg_path):
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            for k, v in cfg.items():
                cur.execute(
                    "INSERT INTO app_config(key, value) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = now()",
                    (k, json.dumps(v, ensure_ascii=False)),
                )
                n_cfg += 1
        conn.commit()

    # sqlite settings -> settings (verbatim)
    n_set = 0
    db_path = os.path.join(CONFIG_DIR, "data")
    if os.path.exists(db_path):
        sq = sqlite3.connect(db_path)
        try:
            rows = sq.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error:
            rows = []
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            for k, v in rows:
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES (%s, %s) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v)),
                )
                n_set += 1
        conn.commit()

    # seen json -> seen_keys
    n_seen = 0
    for kind, fname in (
        ("actions", "actions_seen.json"),
        ("tests", "tests_seen.json"),
    ):
        p = os.path.join(CONFIG_DIR, fname)
        if os.path.exists(p):
            try:
                keys = json.load(open(p, encoding="utf-8"))
            except Exception:
                keys = []
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{SCHEMA}"')
                for key in keys:
                    cur.execute(
                        "INSERT INTO seen_keys(kind, key) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (kind, str(key)),
                    )
                    n_seen += 1
            conn.commit()

    conn.close()
    print(
        f"migrated -> schema {SCHEMA}: app_config={n_cfg} settings={n_set} "
        f"seen={n_seen}"
    )


if __name__ == "__main__":
    main()
