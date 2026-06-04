# Prefect Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cron-based periodic-task orchestration of hh-applicant-tool with Prefect 3 (scheduler + UI + retries + observability), migrating job-by-job with full rollback at every step.

**Architecture:** A `prefect-server` container (official image, Postgres-backed) hosts the API/UI. An `hh-orchestrator` container (our image) runs `orchestration/serve.py`, which registers one Prefect deployment per current cron job; each deployment is a *dispatcher flow* that reads active accounts and fans out to a per-account task that **subprocess-invokes the existing operation code** (wrap, don't rewrite). `hh-web` (uvicorn) and `hh-listener` (Telegram /connect) become plain compose services with `restart: always`, replacing the two pgrep watchdogs. Migration is gated by an `ORCH_ENABLED` env list so jobs go live one at a time alongside the still-running cron container; cron lines are deleted only after per-job parity.

**Tech Stack:** Prefect 3 (self-hosted), Postgres 16 (existing `db` service, new logical DB `prefect`), Docker Compose, Python 3.13, existing `hh_applicant_tool` package + standalone scripts, pytest.

---

## Reference: the 11 jobs (current crontab → Prefect)

`account` = value from `public.app_users`. `feature` = gate via `pgconn.feature_enabled(feature, account)`; `None` = run for all. `jitter` = max random pre-delay seconds (anti-bot). Cron is UTC (work window 05–19 UTC = 08–22 MSK).

| name | command (argv) | feature | cron | jitter | tags | task_timeout |
|---|---|---|---|---|---|---|
| `refresh-token` | `python -m hh_applicant_tool refresh-token` | None | `* * * * *` | 0 | — | 120 |
| `update-resumes` | `python -m hh_applicant_tool update-resumes` | None | `0 */2 * * *` | 300 | — | 600 |
| `browse-activity` | `python /app/browse_activity.py` | `browse` | `23 5-19/2 * * *` | 1500 | — | 1200 |
| `apply-similar` | `python -m hh_applicant_tool apply-similar` | `apply` | `0 5-19 * * *` | 300 | `llm` | 1800 |
| `notify-actions` | `python /app/notify_actions.py` | `notify` | `20 5-19 * * *` | 120 | `llm` | 900 |
| `reply-employers` | `python -m hh_applicant_tool reply-employers --use-ai` | `reply` | `30 5-19 * * *` | 120 | `llm` | 1800 |
| `apply-tests` | `python /app/apply_tests.py --apply --limit 10` | `tests` | `0 6-18/3 * * *` | 90 | `browser` | 1800 |
| `giga` | `python /app/giga_recruiter.py` | `giga` | `*/3 5-19 * * *` | 60 | `llm` | 1800 |
| `monitor` | `python /app/monitor.py` | None | `0 5 * * *` | 0 | — | 600 |
| `funnel` | `python /app/funnel.py` | None | `5 5 * * *` | 0 | — | 600 |
| `send-digest` | `python /app/send_digest.py` | `notify` | `15,45 5-19 * * *` | 0 | — | 300 |

This table is the single source of truth; it is encoded literally in `orchestration/flows.py` (Task 6).

**Platform dimension (spec §4.5):** every JOBS row also carries `platform="hh"` (the only platform today). Deployment/flow names are `"<platform>-<name>"` — i.e. `hh-refresh-token`, `hh-apply-similar`, … When a second source/handler is added later it appears as new rows with its own `platform` (e.g. `habr`, `tg`) and its own command; the orchestrator code does not change. In the verification commands below, deployment names therefore take the form `hh-<job>` (e.g. `prefect deployment run "hh-apply-similar/hh-apply-similar"`).

## File structure (what gets created/modified)

- Create `orchestration/__init__.py` — package marker.
- Create `orchestration/targets.py` — `active_targets(platform, feature)` + `PLATFORM_ENV` map (platform → context env var; `hh → HH_ACCOUNT`). Platform-generic per spec §4.5.
- Create `orchestration/runner.py` — `run_op(command, platform, target, timeout)` subprocess wrapper that sets the per-platform context env, streaming to Prefect logs.
- Create `orchestration/jitter.py` — `human_jitter(max_seconds)`.
- Create `orchestration/alerts.py` — `notify_failure(flow, flow_run, state)` → `pgconn.notify`.
- Create `orchestration/flows.py` — the JOBS table + `make_dispatcher(...)` factory + `build_deployments(names)`.
- Create `orchestration/serve.py` — reads `ORCH_ENABLED`, calls `serve(*deployments)`.
- Create `tests/__init__.py`, `tests/test_orchestration.py` — unit tests for accounts/runner/jitter/flows.
- Modify `pyproject.toml` — add `prefect ^3` dependency.
- Modify `docker-compose.yml` — add `prefect-server`, `hh-orchestrator`, `hh-web`, `hh-listener`; create `prefect` DB; keep `hh_applicant_tool` (cron) until Phase 7.
- Modify `crontab` — delete lines per ported job (Phases 3–5) and watchdogs (Phase 5); reduce to empty at Phase 7.
- Create `docs/ops/prefect.md` — runbook (Phase 8).

Server-side (not in repo, documented in runbook): one-time `CREATE DATABASE prefect`; nginx vhost for the UI subdomain.

---

## Phase 0 — Dependency + package skeleton (no behavior change)

### Task 1: Add Prefect dependency and rebuild the image

**Files:**
- Modify: `pyproject.toml` (`[tool.poetry.dependencies]`)

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, under `[tool.poetry.dependencies]`, after the `uvicorn` line, add:

```toml
# Оркестратор периодических задач (расписания, ретраи, UI)
prefect = "^3.1"
```

- [ ] **Step 2: Regenerate the lock and rebuild the worker image**

Run on the server (where the repo is bind-mounted):
```bash
cd /path/to/hh-applicant-tool
poetry lock --no-update            # if poetry available; else skip (pip resolves at build)
docker compose build hh_applicant_tool
```
Expected: build succeeds; image `hh_applicant_tool:latest` now contains `prefect`.

- [ ] **Step 3: Verify Prefect is importable in the image**

Run:
```bash
docker run --rm hh_applicant_tool:latest python -c "import prefect; print(prefect.__version__)"
```
Expected: prints a `3.x` version.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml poetry.lock
git commit -m "build: add prefect 3 dependency for orchestration"
```

### Task 2: Create the orchestration package skeleton + test harness

**Files:**
- Create: `orchestration/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_orchestration.py`

- [ ] **Step 1: Create the package marker**

`orchestration/__init__.py`:
```python
"""Prefect-оркестрация периодических задач hh-applicant-tool."""
```

- [ ] **Step 2: Create the empty test package + a smoke test**

`tests/__init__.py`:
```python
```

`tests/test_orchestration.py`:
```python
def test_package_imports():
    import orchestration  # noqa: F401
```

- [ ] **Step 3: Run the smoke test**

Run:
```bash
docker run --rm -v "$PWD:/app" -w /app hh_applicant_tool:latest python -m pytest tests/test_orchestration.py -v
```
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add orchestration/__init__.py tests/__init__.py tests/test_orchestration.py
git commit -m "chore: orchestration package skeleton + test harness"
```

---

## Phase 1 — Stand up Prefect server alongside cron (cron stays live)

### Task 3: Create the `prefect` database

**Files:** none (server-side action; documented in runbook later)

- [ ] **Step 1: Create the database on the existing Postgres**

Run:
```bash
docker compose exec db psql -U hh -d hh -c "CREATE DATABASE prefect OWNER hh;"
```
Expected: `CREATE DATABASE` (or `already exists` — safe to ignore).

- [ ] **Step 2: Verify it exists**

Run:
```bash
docker compose exec db psql -U hh -d hh -c "\l" | grep prefect
```
Expected: a `prefect` row owned by `hh`.

### Task 4: Add the `prefect-server` compose service

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add the service**

In `docker-compose.yml`, under `services:` (after `db:`), add:

```yaml
  prefect-server:
    container_name: prefect_server
    image: prefecthq/prefect:3-latest
    restart: unless-stopped
    command: prefect server start --host 0.0.0.0 --port 4200
    depends_on:
      db:
        condition: service_healthy
    environment:
      PREFECT_API_DATABASE_CONNECTION_URL: postgresql+asyncpg://hh:hh_local_pw@db:5432/prefect
      PREFECT_SERVER_API_HOST: 0.0.0.0
      PREFECT_UI_API_URL: /api
    ports:
      - "127.0.0.1:4200:4200"   # наружу только через nginx+TLS
    networks:
      - default
```

- [ ] **Step 2: Start it**

Run:
```bash
docker compose up -d prefect-server
docker compose logs --tail=30 prefect-server
```
Expected: logs show "Application startup complete" / server listening on 4200; Prefect runs its DB migrations against `prefect`.

- [ ] **Step 3: Verify API health**

Run:
```bash
docker compose exec prefect-server python -c "import urllib.request,json; print(urllib.request.urlopen('http://127.0.0.1:4200/api/health').read())"
```
Expected: `true` (or HTTP 200). The cron container is untouched and still running.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(orchestration): add prefect-server (postgres-backed) alongside cron"
```

**Rollback (this phase):** `docker compose rm -sf prefect-server` + revert the compose hunk. Cron untouched throughout.

---

## Phase 2 — Core orchestration helpers (pure code + unit tests)

### Task 5: `accounts.py`, `runner.py`, `jitter.py`, `alerts.py`

**Files:**
- Create: `orchestration/targets.py`
- Create: `orchestration/runner.py`
- Create: `orchestration/jitter.py`
- Create: `orchestration/alerts.py`
- Modify: `tests/test_orchestration.py`

- [ ] **Step 1: Write `targets.py`**

`orchestration/targets.py`:
```python
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
```

- [ ] **Step 2: Write `runner.py`**

`orchestration/runner.py`:
```python
"""Запуск существующей операции как subprocess для одной цели (платформа+target),
со стримингом вывода в логи Prefect и пробросом ненулевого кода как ошибки.
Контекст цели выставляется через PLATFORM_ENV (hh -> HH_ACCOUNT)."""
import asyncio
import os

from prefect import get_run_logger

from .targets import PLATFORM_ENV


async def run_op(command: list[str], platform: str, target: str, timeout: int = 1800) -> int:
    logger = get_run_logger()
    ctx_env = PLATFORM_ENV.get(platform)
    if not ctx_env:
        raise ValueError(f"no context env mapping for platform: {platform}")
    env = {**os.environ, ctx_env: target}
    env.pop("HH_DB_SCHEMA", None)  # единая схема: изоляция по контексту цели
    proc = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None

    async def _pump() -> None:
        async for raw in proc.stdout:
            logger.info("[%s/%s] %s", platform, target, raw.decode("utf-8", "replace").rstrip())

    try:
        await asyncio.wait_for(asyncio.gather(_pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"{' '.join(command)} (platform={platform} target={target}) timed out after {timeout}s"
        )
    rc = proc.returncode or 0
    if rc != 0:
        raise RuntimeError(
            f"{' '.join(command)} (platform={platform} target={target}) exited rc={rc}"
        )
    return rc
```

- [ ] **Step 3: Write `jitter.py`**

`orchestration/jitter.py`:
```python
"""Анти-бот джиттер: случайная задержка перед стартом флоу."""
import asyncio
import secrets


async def human_jitter(max_seconds: int) -> None:
    if max_seconds and max_seconds > 0:
        await asyncio.sleep(secrets.randbelow(max_seconds + 1))
```

- [ ] **Step 4: Write `alerts.py`**

`orchestration/alerts.py`:
```python
"""Алерт о падении флоу -> очередь уведомлений (тот же канал, что и дайджест)."""
from hh_applicant_tool.storage import pgconn


def notify_failure(flow, flow_run, state) -> None:
    """on_failure-хук флоу. Кладёт 🔴-уведомление; доставит send_digest.
    Best-effort: сбой алерта не должен ломать оркестрацию."""
    try:
        name = getattr(flow_run, "name", "") or getattr(flow, "name", "flow")
        pgconn.notify(
            pgconn.PRIORITY_HIGH,
            f"Оркестрация: задача «{name}» упала ({getattr(state, 'name', 'Failed')})",
            category="orchestration",
            dedup_key=f"orch-fail:{getattr(flow_run, 'id', name)}",
        )
    except Exception:
        pass
```

- [ ] **Step 5: Write unit tests**

Replace `tests/test_orchestration.py` with:
```python
import asyncio
import logging

import pytest

import orchestration.targets as targets
from orchestration.jitter import human_jitter
from orchestration.runner import run_op


def test_active_targets_hh_no_feature(monkeypatch):
    monkeypatch.setattr(targets.pgconn, "list_users", lambda: [("A", "a"), ("B", "b")])
    assert targets.active_targets("hh") == ["a", "b"]


def test_active_targets_hh_feature_filter(monkeypatch):
    monkeypatch.setattr(targets.pgconn, "list_users", lambda: [("A", "a"), ("B", "b")])
    monkeypatch.setattr(
        targets.pgconn, "feature_enabled",
        lambda feat, account=None: account == "a",
    )
    assert targets.active_targets("hh", "apply") == ["a"]


def test_active_targets_unknown_platform_raises():
    with pytest.raises(ValueError):
        targets.active_targets("habr")


def test_human_jitter_zero_is_instant():
    asyncio.run(human_jitter(0))  # returns immediately, no error


def test_run_op_nonzero_raises(monkeypatch):
    # get_run_logger requires a run context; patch it to a stub logger.
    import orchestration.runner as runner
    monkeypatch.setattr(runner, "get_run_logger", lambda: logging.getLogger("test"))
    with pytest.raises(RuntimeError):
        asyncio.run(run_op(["python", "-c", "import sys; sys.exit(3)"], "hh", "acct", timeout=30))


def test_run_op_success(monkeypatch):
    import orchestration.runner as runner
    monkeypatch.setattr(runner, "get_run_logger", lambda: logging.getLogger("test"))
    rc = asyncio.run(run_op(["python", "-c", "print('hi')"], "hh", "acct", timeout=30))
    assert rc == 0
```

- [ ] **Step 6: Run the tests**

Run:
```bash
docker run --rm -v "$PWD:/app" -w /app hh_applicant_tool:latest python -m pytest tests/test_orchestration.py -v
```
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add orchestration/ tests/test_orchestration.py
git commit -m "feat(orchestration): account fan-out, subprocess runner, jitter, failure alert"
```

---

## Phase 3 — Flow factory + first job live (exemplar: refresh-token)

### Task 6: `flows.py` (JOBS table + dispatcher factory) and `serve.py`

**Files:**
- Create: `orchestration/flows.py`
- Create: `orchestration/serve.py`
- Modify: `tests/test_orchestration.py`

- [ ] **Step 1: Write `flows.py`**

`orchestration/flows.py`:
```python
"""Диспетчер-флоу на расписании: читает активные аккаунты и раздаёт работу
per-account задаче, которая запускает существующую операцию как subprocess.
JOBS — единственный источник правды по задачам (см. таблицу в плане/спеке)."""
from prefect import flow, task

from .alerts import notify_failure
from .jitter import human_jitter
from .runner import run_op
from .targets import active_targets

JOBS: list[dict] = [
    dict(name="refresh-token",  command=["python", "-m", "hh_applicant_tool", "refresh-token"],
         feature=None,     cron="* * * * *",        jitter=0,    tags=[],          retries=1, timeout=120),
    dict(name="update-resumes", command=["python", "-m", "hh_applicant_tool", "update-resumes"],
         feature=None,     cron="0 */2 * * *",      jitter=300,  tags=[],          retries=2, timeout=600),
    dict(name="browse-activity", command=["python", "/app/browse_activity.py"],
         feature="browse", cron="23 5-19/2 * * *",  jitter=1500, tags=[],          retries=1, timeout=1200),
    dict(name="apply-similar",  command=["python", "-m", "hh_applicant_tool", "apply-similar"],
         feature="apply",  cron="0 5-19 * * *",     jitter=300,  tags=["llm"],     retries=2, timeout=1800),
    dict(name="notify-actions", command=["python", "/app/notify_actions.py"],
         feature="notify", cron="20 5-19 * * *",    jitter=120,  tags=["llm"],     retries=2, timeout=900),
    dict(name="reply-employers", command=["python", "-m", "hh_applicant_tool", "reply-employers", "--use-ai"],
         feature="reply",  cron="30 5-19 * * *",    jitter=120,  tags=["llm"],     retries=2, timeout=1800),
    dict(name="apply-tests",    command=["python", "/app/apply_tests.py", "--apply", "--limit", "10"],
         feature="tests",  cron="0 6-18/3 * * *",   jitter=90,   tags=["browser"], retries=1, timeout=1800),
    dict(name="giga",           command=["python", "/app/giga_recruiter.py"],
         feature="giga",   cron="*/3 5-19 * * *",   jitter=60,   tags=["llm"],     retries=1, timeout=1800),
    dict(name="monitor",        command=["python", "/app/monitor.py"],
         feature=None,     cron="0 5 * * *",        jitter=0,    tags=[],          retries=1, timeout=600),
    dict(name="funnel",         command=["python", "/app/funnel.py"],
         feature=None,     cron="5 5 * * *",        jitter=0,    tags=[],          retries=1, timeout=600),
    dict(name="send-digest",    command=["python", "/app/send_digest.py"],
         feature="notify", cron="15,45 5-19 * * *", jitter=0,    tags=[],          retries=1, timeout=300),
]


def _make_dispatcher(job: dict):
    name = job["name"]
    platform = job.get("platform", "hh")          # spec §4.5; hh — единственная сейчас
    flow_name = f"{platform}-{name}"               # e.g. hh-apply-similar
    command = job["command"]
    feature = job["feature"]
    jitter = job["jitter"]
    timeout = job["timeout"]
    task_tags = [platform, *job["tags"]]

    @task(name=f"{flow_name}:target", retries=job["retries"],
          retry_delay_seconds=[30, 120], tags=task_tags)
    async def _target_task(target: str):
        await run_op(command, platform, target, timeout=timeout)

    @flow(name=flow_name, on_failure=[notify_failure])
    async def _dispatch():
        await human_jitter(jitter)
        targets = active_targets(platform, feature)
        futures = [_target_task.submit(t) for t in targets]
        # Изолируем падения: одна цель упала — остальные идут, флоу не валится из-за одной.
        for fut in futures:
            fut.result(raise_on_failure=False)

    _dispatch.__name__ = flow_name.replace("-", "_")
    return _dispatch


def _flow_name(job: dict) -> str:
    return f"{job.get('platform', 'hh')}-{job['name']}"


FLOWS = {job["name"]: _make_dispatcher(job) for job in JOBS}
JOBS_BY_NAME = {job["name"]: job for job in JOBS}


def build_deployments(names: set[str] | None):
    """Список deployments для serve(). names — короткие имена задач (как в ORCH_ENABLED);
    None -> все. Имя деплоймента — '<platform>-<name>'."""
    deployments = []
    for job in JOBS:
        if names is not None and job["name"] not in names:
            continue
        f = FLOWS[job["name"]]
        deployments.append(
            f.to_deployment(name=_flow_name(job), cron=job["cron"],
                            tags=[job.get("platform", "hh"), *job["tags"]])
        )
    return deployments
```

> Note: `ORCH_ENABLED` and `build_deployments(names)` use the **short** job name (`refresh-token`); the resulting Prefect **deployment name** is `hh-refresh-token`. Verification commands use the deployment name, e.g. `prefect deployment run "hh-refresh-token/hh-refresh-token"`.
```

- [ ] **Step 2: Write `serve.py`**

`orchestration/serve.py`:
```python
"""Долгоживущий процесс: регистрирует и обслуживает выбранные deployments.
ORCH_ENABLED — список имён через запятую (или 'all'/'*'). Пусто -> ничего не
обслуживаем (безопасный дефолт во время поэтапной миграции)."""
import os

from prefect import serve

from orchestration.flows import build_deployments


def main() -> None:
    raw = (os.environ.get("ORCH_ENABLED") or "").strip()
    if raw in ("all", "*"):
        names = None
    else:
        names = {n.strip() for n in raw.split(",") if n.strip()}
    if names is not None and not names:
        print("ORCH_ENABLED пуст — нечего обслуживать, выходим.")
        return
    deployments = build_deployments(names)
    print(f"serve: {len(deployments)} deployment(s): "
          f"{[d.name for d in deployments]}")
    serve(*deployments)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Add a flows unit test**

Append to `tests/test_orchestration.py`:
```python
def test_jobs_table_complete():
    from orchestration.flows import JOBS
    names = [j["name"] for j in JOBS]
    assert len(names) == len(set(names)) == 11
    for j in JOBS:
        assert j["command"] and isinstance(j["command"], list)
        assert j["cron"] and isinstance(j["cron"], str)


def test_build_deployments_filters_by_name():
    from orchestration.flows import build_deployments
    deps = build_deployments({"refresh-token"})
    assert len(deps) == 1 and deps[0].name == "hh-refresh-token"
    assert len(build_deployments(None)) == 11
```

- [ ] **Step 4: Run tests**

Run:
```bash
docker run --rm -v "$PWD:/app" -w /app hh_applicant_tool:latest python -m pytest tests/test_orchestration.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestration/flows.py orchestration/serve.py tests/test_orchestration.py
git commit -m "feat(orchestration): dispatcher-flow factory + serve entrypoint (ORCH_ENABLED-gated)"
```

### Task 7: Add the `hh-orchestrator` service and bring `refresh-token` live

**Files:**
- Modify: `docker-compose.yml`
- Modify: `crontab`

- [ ] **Step 1: Add the orchestrator service (only refresh-token enabled)**

In `docker-compose.yml`, under `services:`, add:

```yaml
  hh-orchestrator:
    container_name: hh_orchestrator
    image: hh_applicant_tool:latest
    restart: unless-stopped
    command: python -m orchestration.serve
    depends_on:
      db:
        condition: service_healthy
      prefect-server:
        condition: service_started
    volumes:
      - .:/app
      - /etc/localtime:/etc/localtime:ro
    environment:
      - CONFIG_DIR=/app/config
      - HH_DB_DSN=postgresql://hh:hh_local_pw@db:5432/hh
      - HH_DB_SCHEMA=public
      - PREFECT_API_URL=http://prefect-server:4200/api
      # Поэтапная миграция: включаем задачи по одной. Пусто/список имён/all.
      - ORCH_ENABLED=refresh-token
    networks:
      - default
      - test-llm
```

- [ ] **Step 2: Start the orchestrator**

Run:
```bash
docker compose up -d hh-orchestrator
docker compose logs --tail=40 hh-orchestrator
```
Expected: `serve: 1 deployment(s): ['refresh-token']`, then Prefect "Your deployments are being served...".

- [ ] **Step 3: Confirm the deployment + a run in the Prefect API**

Run (wait ~70s for the first 1-min schedule to fire):
```bash
docker compose exec prefect-server prefect deployment ls
docker compose exec prefect-server prefect flow-run ls --limit 5
```
Expected: a `hh-refresh-token/hh-refresh-token` deployment; at least one `Completed` flow run.

- [ ] **Step 4: Disable the cron `refresh-token` line (parity reached → remove duplicate)**

In `crontab`, comment out the refresh-token line (line beginning `*/1 * * * * ... refresh-token`):
```diff
-*/1 * * * * /usr/local/bin/python /app/run_all.py -- /usr/local/bin/python -m hh_applicant_tool refresh-token >> /dev/null 2>&1
+# MIGRATED TO PREFECT (refresh-token): */1 * * * * .../refresh-token
```

- [ ] **Step 5: Reload cron in the running cron container**

Run:
```bash
docker compose exec hh_applicant_tool crontab -u docker /app/crontab
docker compose exec hh_applicant_tool crontab -l -u docker | grep refresh-token
```
Expected: only the commented `# MIGRATED ...` line; no active refresh-token cron entry.

- [ ] **Step 6: Verify token still refreshes (Prefect is now the only refresher)**

Run after a couple minutes:
```bash
docker compose exec prefect-server prefect flow-run ls --limit 3
```
Expected: refresh-token runs continue, `Completed`.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml crontab
git commit -m "feat(orchestration): hh-orchestrator service; migrate refresh-token from cron to prefect"
```

**Rollback (per job):** un-comment the cron line + `crontab -u docker /app/crontab`, and remove the job from `ORCH_ENABLED` + `docker compose up -d hh-orchestrator`. The job is back on cron within a minute. No code is deleted.

---

## Phase 4 — Port the remaining scheduled jobs one at a time

Each job below is an identical mini-task. Do them **one per commit**, verifying each in the Prefect UI before deleting its cron line. Order chosen safest-first (read-only / idempotent before send-heavy).

### Task 8: Port `update-resumes`, then `funnel`, then `monitor` (no-feature, low-risk)

For **each** of `update-resumes`, `funnel`, `monitor`:

- [ ] **Step 1: Enable in the orchestrator**

Edit `docker-compose.yml` → `hh-orchestrator.environment.ORCH_ENABLED`, append the job name (comma-separated). Example after adding `update-resumes`:
```yaml
      - ORCH_ENABLED=refresh-token,update-resumes
```

- [ ] **Step 2: Apply**

Run:
```bash
docker compose up -d hh-orchestrator
docker compose logs --tail=20 hh-orchestrator
```
Expected: `serve: N deployment(s)` including the new name.

- [ ] **Step 3: Trigger an immediate run to verify (don't wait for schedule)**

Run:
```bash
docker compose exec prefect-server prefect deployment run "<job>/<job>"   # e.g. update-resumes/update-resumes
docker compose exec prefect-server prefect flow-run ls --limit 5
```
Expected: a `Completed` run; inspect logs in the UI (per-account lines `[account] ...`).

- [ ] **Step 4: Delete the cron line for this job + reload cron**

Comment the matching line in `crontab`, then:
```bash
docker compose exec hh_applicant_tool crontab -u docker /app/crontab
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml crontab
git commit -m "feat(orchestration): migrate <job> from cron to prefect"
```

Repeat Steps 1–5 for `funnel` and `monitor`.

### Task 9: Port the feature-gated jobs `browse-activity`, `apply-similar`, `notify-actions`, `reply-employers`, `apply-tests`, `send-digest`

Same 5-step procedure as Task 8, **one job per commit, in this order**: `browse-activity` → `apply-similar` → `notify-actions` → `reply-employers` → `send-digest` → `apply-tests`.

Extra per-job verification before deleting the cron line:

- [ ] **`apply-similar`:** after the manual `prefect deployment run`, confirm an application actually sent for an apply-enabled account:
```bash
docker compose exec hh_applicant_tool python -c "from hh_applicant_tool.storage import pgconn;\
import datetime;\
c=pgconn.connect();cur=c.cursor();\
cur.execute(\"select account,kind,count from activity_daily where day=current_date and kind='apply'\");\
print(cur.fetchall())"
```
Expected: counts present for apply-enabled accounts (parity with cron behavior).

- [ ] **`send-digest`:** confirm a digest delivered (sent_at advanced) after the run:
```bash
docker compose exec hh_applicant_tool python -c "from hh_applicant_tool.storage import pgconn;\
c=pgconn.connect();cur=c.cursor();\
cur.execute(\"select account,max(sent_at) from notifications group by 1\");print(cur.fetchall())"
```
Expected: recent `sent_at` for notify-enabled accounts.

- [ ] **`apply-tests`:** create the `browser` concurrency limit (single browser at a time) BEFORE enabling:
```bash
docker compose exec prefect-server prefect concurrency-limit create browser 1
docker compose exec prefect-server prefect concurrency-limit ls
```
Expected: a `browser` tag limit = 1. (The task is tagged `browser` via `task_tags`.)

Commit message per job: `feat(orchestration): migrate <job> from cron to prefect`.

### Task 10: Port `giga` (keeps its advisory lock for now)

`giga` runs every 3 min; the existing `giga_recruiter.py` already holds a Postgres advisory lock per account, so overlapping schedules are safe **without** a Prefect concurrency limit yet (that swap is Task 14).

- [ ] **Step 1: Enable `giga` in `ORCH_ENABLED`, `docker compose up -d hh-orchestrator`.**
- [ ] **Step 2: Verify a giga flow run completes; check `giga_queue` still drains:**
```bash
docker compose exec prefect-server prefect flow-run ls --limit 5
docker compose exec hh_applicant_tool python -c "from hh_applicant_tool.storage import pgconn;\
c=pgconn.connect();cur=c.cursor();\
cur.execute(\"select account,status,count(*) from giga_queue group by 1,2 order by 1,2\");print(cur.fetchall())"
```
Expected: giga flow `Completed`; queue still progressing; cron.log shows no `giga: уже выполняется` collisions from a stray cron giga (because we delete the cron line next).
- [ ] **Step 3: Delete the giga cron line + reload cron + commit.**

```bash
docker compose exec hh_applicant_tool crontab -u docker /app/crontab
git add docker-compose.yml crontab
git commit -m "feat(orchestration): migrate giga from cron to prefect (advisory lock retained)"
```

---

## Phase 5 — Convert long-running services to compose (kill the watchdogs)

### Task 11: `hh-web` service (replaces the uvicorn pgrep watchdog)

**Files:**
- Modify: `docker-compose.yml`
- Modify: `crontab`

- [ ] **Step 1: Stop the in-cron uvicorn (frees port 60080) and add the service**

In `docker-compose.yml` add:
```yaml
  hh-web:
    container_name: hh_web
    image: hh_applicant_tool:latest
    restart: always
    command: python -u -m uvicorn web_app:app --host 0.0.0.0 --port 60080
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - .:/app
      - /etc/localtime:/etc/localtime:ro
    environment:
      - CONFIG_DIR=/app/config
      - HH_DB_DSN=postgresql://hh:hh_local_pw@db:5432/hh
      - HH_DB_SCHEMA=public
    ports:
      - "127.0.0.1:60080:60080"
    networks:
      - default
      - test-llm
```

Remove the `ports:` mapping `127.0.0.1:60080:60080` from the original `hh_applicant_tool` service (avoid the port clash) and comment the uvicorn watchdog line in `crontab`:
```diff
-* * * * * /bin/bash -c 'pgrep -f "[u]vicorn web_app" >/dev/null || /bin/bash /app/run_webapp.sh'
+# MIGRATED TO COMPOSE (hh-web): uvicorn watchdog
```

- [ ] **Step 2: Kill the cron-spawned uvicorn, reload cron, start hh-web**

Run:
```bash
docker compose exec hh_applicant_tool sh -lc 'pkill -f "[u]vicorn web_app"; crontab -u docker /app/crontab'
docker compose up -d hh_applicant_tool hh-web   # recreate cron container w/o the port; start web
```

- [ ] **Step 3: Verify the cabinet still serves**

Run:
```bash
docker compose exec hh-web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:60080/').status)"
```
Expected: `200`. Kill `hh-web` once (`docker compose kill hh-web`) and confirm it auto-restarts (`docker compose ps hh-web` → `running` within seconds).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml crontab
git commit -m "feat(orchestration): hh-web compose service; remove uvicorn pgrep watchdog"
```

### Task 12: `hh-listener` service (replaces the tg_connect_bot pgrep watchdog)

**Files:**
- Modify: `docker-compose.yml`
- Modify: `crontab`

- [ ] **Step 1: Inspect how the listener is launched**

Run:
```bash
cat run_listener.sh
```
Use the exact command it runs as the service `command` below (typically `python tg_connect_bot.py`). If `run_listener.sh` does extra setup, call the script instead: `command: bash /app/run_listener.sh` is NOT suitable (it backgrounds+exits); use the foreground python invocation it contains.

- [ ] **Step 2: Add the service**

```yaml
  hh-listener:
    container_name: hh_listener
    image: hh_applicant_tool:latest
    restart: always
    command: python -u /app/tg_connect_bot.py
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - .:/app
      - /etc/localtime:/etc/localtime:ro
    environment:
      - CONFIG_DIR=/app/config
      - HH_DB_DSN=postgresql://hh:hh_local_pw@db:5432/hh
      - HH_DB_SCHEMA=public
    networks:
      - default
      - test-llm
```

Comment the listener watchdog in `crontab`:
```diff
-* * * * * /bin/bash -c 'pgrep -f "[t]g_connect_bot.py" >/dev/null || /bin/bash /app/run_listener.sh'
+# MIGRATED TO COMPOSE (hh-listener): tg_connect_bot watchdog
```

- [ ] **Step 3: Stop the cron-spawned listener, reload cron, start the service**

```bash
docker compose exec hh_applicant_tool sh -lc 'pkill -f "[t]g_connect_bot.py"; crontab -u docker /app/crontab'
docker compose up -d hh-listener
docker compose logs --tail=20 hh-listener
```
Expected: listener starts and connects (no crash loop). Verify `/connect` in the bot still works (manual check or logs).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml crontab
git commit -m "feat(orchestration): hh-listener compose service; remove listener pgrep watchdog"
```

---

## Phase 6 — Failure alerting end-to-end

### Task 13: Verify the `on_failure` → Telegram path

The `notify_failure` hook is already wired into every flow (Task 6). This task proves it.

- [ ] **Step 1: Force a failure on a throwaway deployment run**

Temporarily set a bad command to trigger a failure, OR run an existing deployment against a known-broken account. Simplest: add a one-off check by running the dispatcher with a deliberately failing command via the Prefect UI "Custom Run" is not available for params here, so instead assert the hook function directly:
```bash
docker run --rm -v "$PWD:/app" -w /app hh_applicant_tool:latest python -c "
from orchestration.alerts import notify_failure
class F:  name='unit-test-job'
class R:  name='unit-test-run'; id='x'
class S:  name='Failed'
notify_failure(F(), R(), S())
from hh_applicant_tool.storage import pgconn
c=pgconn.connect();cur=c.cursor()
cur.execute(\"select text,category from notifications where category='orchestration' order by created_at desc limit 1\")
print(cur.fetchone())
"
```
Expected: prints a row like `('Оркестрация: задача «unit-test-run» упала (Failed)', 'orchestration')`.

- [ ] **Step 2: Confirm it would be delivered by the digest**

`send-digest` (already migrated) will pick up the 🔴 notification on its next run. Optionally trigger:
```bash
docker compose exec prefect-server prefect deployment run "hh-send-digest/hh-send-digest"
```
Expected: the orchestration-failure line appears in the Telegram digest (for notify-enabled accounts).

- [ ] **Step 3: Clean up the test notification + commit (no code change; this is verification)**

```bash
docker compose exec hh_applicant_tool python -c "from hh_applicant_tool.storage import pgconn;\
c=pgconn.connect();cur=c.cursor();cur.execute(\"delete from notifications where category='orchestration' and text like '%unit-test%'\");c.commit()"
```
No commit needed (verification only). If you added any helper, commit it.

---

## Phase 7 — Decommission cron + advisory locks

### Task 14: giga single-flight — keep the advisory lock (per-account guard)

**Decision:** giga needs **per-account** single-flight (account A and B may run concurrently; the same account must not overlap). Postgres advisory locks (`pg_try_advisory_lock(hashtext('giga:'+acc))`, already in `giga_recruiter.py`) do exactly this with zero extra infra and survive worker restarts. Prefect's tag concurrency limits are global-per-tag (not per-account), and the `concurrency(name)` global-limit context requires each `giga:<account>` limit to be pre-created — clunky for a dynamic, growing account set. So we **keep the advisory lock** for giga's per-account guard and use Prefect concurrency only for the *global* `llm`/`browser` throttles (Task 17).

**Files:**
- Modify: `giga_recruiter.py` (comment only)

- [ ] **Step 1: Document why the lock stays**

In `giga_recruiter.py`, above the `_lock(account)` call in `main()`, add a comment:
```python
    # Per-account single-flight: advisory-лок оставлен сознательно (Prefect tag-лимиты
    # глобальны, а не per-account). Глобальные throttle на LLM/браузер — в Prefect (теги llm/browser).
```

- [ ] **Step 2: Verify no overlap under Prefect's 3-min schedule**

Run two manual giga runs back-to-back; the second must no-op on the held lock for any account already running:
```bash
docker compose exec prefect-server prefect deployment run "hh-giga/hh-giga"
docker compose exec prefect-server prefect deployment run "hh-giga/hh-giga"
docker compose exec hh-orchestrator sh -lc 'grep -a "уже выполняется" /proc/1/fd/1 | tail -2 || true'
docker compose exec prefect-server prefect flow-run ls --limit 6
```
Expected: the second overlapping run logs `giga: уже выполняется для этого аккаунта — пропуск`; queue stays consistent (no double-answers).

- [ ] **Step 3: Commit**

```bash
git add giga_recruiter.py
git commit -m "docs(giga): note advisory lock retained as per-account single-flight under Prefect"
```

### Task 15: Strip the cron container down to nothing and switch its role off

**Files:**
- Modify: `crontab`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Confirm every cron line is commented (all jobs + both watchdogs migrated)**

Run:
```bash
grep -vnE '^\s*#|^\s*$|^PATH=|^SHELL=|@reboot' crontab || echo "NO ACTIVE CRON LINES"
```
Expected: `NO ACTIVE CRON LINES` (only the `@reboot startup.sh` may remain — see Step 2).

- [ ] **Step 2: Decide the cron container's fate**

The `@reboot /app/startup.sh` did: reload crontab, ensure tables, refresh-token, update-resumes, start listener. Tables are ensured by `pgconn.connect(ensure=True)` (now also run by any flow). With all jobs + services migrated, the cron container has no remaining duty. Stop and remove it from compose:

In `docker-compose.yml`, delete the entire `hh_applicant_tool` service block (the cron+tail one). The image is still built/used by `hh-orchestrator`/`hh-web`/`hh-listener`, so keep the `build:` on ONE of those services instead. Move the `build:` stanza to `hh-orchestrator`:
```yaml
  hh-orchestrator:
    build:
      context: .
      dockerfile: Dockerfile
    image: hh_applicant_tool:latest
    # ...rest unchanged...
```

- [ ] **Step 3: Apply and verify the stack**

```bash
docker compose up -d --remove-orphans
docker compose ps
```
Expected services running: `db`, `prefect-server`, `hh-orchestrator`, `hh-web`, `hh-listener` (+ external `vllm` untouched). No `hh_applicant_tool` cron container.

- [ ] **Step 4: Set ORCH_ENABLED=all (all jobs now owned by Prefect)**

In `docker-compose.yml`, set `hh-orchestrator.environment` `ORCH_ENABLED=all`, then:
```bash
docker compose up -d hh-orchestrator
docker compose exec prefect-server prefect deployment ls
```
Expected: 11 deployments listed.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml crontab
git commit -m "feat(orchestration): decommission cron container; Prefect owns all schedules"
```

### Task 16: Remove now-dead orchestration glue

**Files:**
- Modify/Delete: `run_all.py`, `run_webapp.sh`, `run_listener.sh`, `startup.sh`, `crontab`, `Dockerfile`

- [ ] **Step 1: Confirm nothing references `run_all.py` anymore**

Run:
```bash
grep -rnE "run_all\.py|run_webapp\.sh|run_listener\.sh" --include='*.sh' --include='*.yml' --include='crontab' . | grep -v '#'
```
Expected: no active references (only commented).

- [ ] **Step 2: Remove cron from the Dockerfile**

In `Dockerfile`: drop `cron` from the apt install, drop the `COPY crontab` / `crontab -u docker` lines, and change the final `CMD` to a harmless default (the real commands come from compose `command:`), e.g.:
```dockerfile
CMD ["python", "-c", "print('hh-applicant-tool image; command provided by compose')"]
```
Keep `procps`, `dos2unix`, `tzdata`, playwright deps.

- [ ] **Step 3: Delete the dead files**

```bash
git rm run_all.py run_webapp.sh run_listener.sh startup.sh crontab
```
(If `startup.sh`'s table-ensure is still desired as a one-shot, fold `pgconn.connect(ensure=True)` into `orchestration/serve.py` startup before `serve()` — add one line: `pgconn.connect(ensure=True).close()` at the top of `main()`. Do this BEFORE deleting startup.sh.)

- [ ] **Step 4: Rebuild + full stack smoke**

```bash
docker compose build hh-orchestrator
docker compose up -d --remove-orphans
docker compose ps
docker compose exec prefect-server prefect flow-run ls --limit 10
```
Expected: all services healthy; flow runs completing on schedule.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(orchestration): remove cron/run_all/watchdog glue; ensure tables in serve startup"
```

---

## Phase 8 — Hardening, UI exposure, docs

### Task 17: Protect vLLM + expose the UI securely

**Files:**
- Modify: `docker-compose.yml` (concurrency note)
- Create: `docs/ops/prefect.md`

- [ ] **Step 1: Create LLM + browser concurrency limits (protect the shared box)**

```bash
docker compose exec prefect-server prefect concurrency-limit create llm 3
docker compose exec prefect-server prefect concurrency-limit create browser 1
docker compose exec prefect-server prefect concurrency-limit ls
```
Expected: `llm`=3, `browser`=1. (Tasks are already tagged `llm`/`browser` via the JOBS table, so these throttle LLM-heavy and browser flows globally → vLLM and Chromium never get hammered.)

- [ ] **Step 2: nginx vhost for the UI (server-side; document, don't commit secrets)**

On the host, add an nginx server block for e.g. `prefect.<domain>` proxying to `127.0.0.1:4200`, with Let's Encrypt TLS and HTTP basic-auth (htpasswd), mirroring the Mini App vhost. Verify:
```bash
curl -su user:pass https://prefect.<domain>/api/health
```
Expected: `true`. Without creds → 401.

- [ ] **Step 3: Write the runbook**

`docs/ops/prefect.md` — document: services & ports, `ORCH_ENABLED`, how to add a job (append a JOBS row + redeploy `hh-orchestrator`), how to pause/trigger a deployment (`prefect deployment pause/run`), where logs live (Prefect UI), concurrency limits, rollback (re-enable a cron line is gone post-Phase-7, so rollback = `prefect deployment pause` + manual run), and the `prefect` DB backup note.

- [ ] **Step 4: Commit**

```bash
git add docs/ops/prefect.md docker-compose.yml
git commit -m "docs(ops): prefect runbook; concurrency limits for vLLM/browser protection"
```

---

## Self-review notes (coverage vs spec)

- Spec §4.1 containers → Tasks 4, 7, 11, 12, 15 (server, orchestrator, web, listener; cron removed).
- Spec §4.2 dispatcher→per-account → Task 6 (factory) + Tasks 8–10 (jobs).
- Spec §4.3 schedules / work window / jitter → JOBS table (Task 6), preserved cron expressions + `human_jitter`.
- Spec §4.4 giga → Task 10 (port). **Deviation (documented):** per-account single-flight stays on the Postgres advisory lock (Task 14) rather than a Prefect concurrency limit — advisory locks are per-account and infra-free; Prefect tag limits are global. Spec intent (no overlap, vLLM protection) is fully met: no-overlap via the lock, vLLM/browser protection via Prefect `llm`/`browser` tag limits (Task 17).
- Spec §4.5 platform-genericity → `targets.active_targets(platform, feature)` + `PLATFORM_ENV` (Task 5), `platform` in JOBS + `<platform>-<name>` deployments (Task 6). hh is the only platform today; new sources/handlers (parent umbrella spec) plug in as JOBS rows with no orchestrator change.
- Spec §5 reliability → task retries (Task 6), global `llm`/`browser` concurrency limits (Task 17), per-account giga single-flight via advisory lock (Task 14), `on_failure` alerts (Tasks 6, 13), `restart: always` services (Tasks 11, 12).
- Spec §6 observability → Prefect UI throughout; runbook (Task 17). (Cabinet widget intentionally out of scope.)
- Spec §7 management → `ORCH_ENABLED` + JOBS table + per-account settings read each run (`active_accounts`).
- Spec §8 security → Task 17 (nginx + basic-auth + 127.0.0.1 binds; `PREFECT_API_URL` internal).
- Spec §9 phased migration → Phases 1→7 are exactly the spec's 5 steps, with per-job rollback (Task 7 rollback note) until Phase 7.
- Spec §10 testing → pytest for pure logic (Tasks 2, 5, 6); per-job manual `deployment run` + DB parity checks (Tasks 8, 9, 10, 13).
- Spec §11 out of scope → business logic untouched (subprocess wrapping); cabinet widget deferred.
