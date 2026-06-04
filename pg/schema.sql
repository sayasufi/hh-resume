-- Postgres-схема hh-applicant-tool (schema-per-user). Запуск:
--   psql -v sch=u_egor -f schema.sql
-- Перенос с SQLite: INTEGER -> bigint для hh id (negotiation/chat_id > 2^31),
-- DATETIME -> timestamptz, randomblob -> gen_random_uuid, триггеры -> функция.

CREATE SCHEMA IF NOT EXISTS :"sch";
SET search_path TO :"sch";

/* ===================== employers ===================== */
CREATE TABLE IF NOT EXISTS employers (
    id bigint PRIMARY KEY,
    name text NOT NULL,
    type text,
    description text,
    site_url text,
    area_id bigint,
    area_name text,
    alternate_url text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

/* ===================== vacancy_contacts ===================== */
CREATE TABLE IF NOT EXISTS vacancy_contacts (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    vacancy_id bigint NOT NULL,
    vacancy_alternate_url text,
    vacancy_name text,
    vacancy_area_id bigint,
    vacancy_area_name text,
    vacancy_salary_from bigint,
    vacancy_salary_to bigint,
    vacancy_currency varchar(3),
    vacancy_gross boolean,
    employer_id bigint,
    employer_name text,
    name text,
    email text,
    phone_numbers text NOT NULL,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE (vacancy_id, email)
);

/* ===================== vacancies ===================== */
CREATE TABLE IF NOT EXISTS vacancies (
    id bigint PRIMARY KEY,
    name text NOT NULL,
    area_id bigint,
    area_name text,
    salary_from bigint,
    salary_to bigint,
    currency varchar(3),
    gross boolean,
    published_at timestamptz,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    remote boolean,
    experience text,
    professional_roles text,
    alternate_url text
);

/* ===================== negotiations ===================== */
CREATE TABLE IF NOT EXISTS negotiations (
    id bigint PRIMARY KEY,
    state text NOT NULL,
    vacancy_id bigint NOT NULL,
    employer_id bigint,
    chat_id bigint NOT NULL,
    resume_id text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

/* ===================== settings ===================== */
CREATE TABLE IF NOT EXISTS settings (
    key text PRIMARY KEY,
    value text NOT NULL
);

/* ===================== resumes ===================== */
CREATE TABLE IF NOT EXISTS resumes (
    id text PRIMARY KEY,
    title text NOT NULL,
    url text,
    alternate_url text,
    status_id text,
    status_name text,
    can_publish_or_update boolean,
    total_views integer DEFAULT 0,
    new_views integer DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

/* ===================== app_config (бывший config.json) ===================== */
-- token, openai, telegram, preferences, openrouter — каждый ключ = JSON
CREATE TABLE IF NOT EXISTS app_config (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz DEFAULT now()
);

/* ===================== seen-наборы (бывшие JSON) ===================== */
-- kind: 'actions' (key=nid:msgid) | 'tests' (key=vacancy_id)
CREATE TABLE IF NOT EXISTS seen_keys (
    kind text NOT NULL,
    key text NOT NULL,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (kind, key)
);

/* ===================== action_items (бывший action_items.md) ===================== */
CREATE TABLE IF NOT EXISTS action_items (
    id bigserial PRIMARY KEY,
    nid bigint,
    chat_id bigint,
    vacancy text,
    action text NOT NULL,
    chat_url text,
    vacancy_url text,
    created_at timestamptz DEFAULT now()
);

/* ===================== индексы ===================== */
CREATE INDEX IF NOT EXISTS idx_vac_upd ON vacancies(updated_at);
CREATE INDEX IF NOT EXISTS idx_emp_upd ON employers(updated_at);
CREATE INDEX IF NOT EXISTS idx_neg_upd ON negotiations(updated_at);

/* ===================== updated_at триггеры ===================== */
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['employers','vacancy_contacts','vacancies','negotiations','resumes']
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%1$s_updated ON %1$s;
             CREATE TRIGGER trg_%1$s_updated BEFORE UPDATE ON %1$s
             FOR EACH ROW EXECUTE FUNCTION set_updated_at();', t);
    END LOOP;
END $$;
