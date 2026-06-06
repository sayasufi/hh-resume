"""Один module-level диспетчер-флоу `dispatch(job_name)`, параметризованный именем
задачи из таблицы JOBS. Различия (платформа, команда, расписание, теги, таймаут)
берутся из JOBS по job_name. Так флоу импортируем по entrypoint (нужно serve()),
а per-job-идентичность несёт ИМЯ ДЕПЛОЙМЕНТА `<platform>-<name>`.
JOBS — единственный источник правды (см. план/спеку §4.5). Сейчас платформа одна — hh."""
from prefect import flow, task

from hh_applicant_tool.storage import pgconn

from .alerts import notify_failure
from .jitter import human_jitter
from .runner import run_op
from .targets import active_targets

JOBS: list[dict] = [
    dict(name="refresh-token",  command=["python", "-m", "hh_applicant_tool", "refresh-token"],
         feature=None,     cron="* * * * *",        jitter=0,    tags=[],          timeout=120),
    dict(name="update-resumes", command=["python", "-m", "hh_applicant_tool", "update-resumes"],
         feature=None,     cron="0 */2 * * *",      jitter=300,  tags=[],          timeout=600),
    dict(name="browse-activity", command=["python", "/app/services/browse_activity.py"],
         feature="browse", cron="23 5-19/2 * * *",  jitter=1500, tags=[],          timeout=1200),
    dict(name="apply-similar",  command=["python", "-m", "hh_applicant_tool", "apply-similar"],
         feature="apply",  cron="0 5-19 * * *",     jitter=300,  tags=["llm"],     timeout=1800),
    dict(name="notify-actions", command=["python", "/app/services/notify_actions.py"],
         feature="notify", cron="20 5-19 * * *",    jitter=120,  tags=["llm"],     timeout=900),
    dict(name="reply-employers", command=["python", "-m", "hh_applicant_tool", "reply-employers", "--use-ai"],
         feature="reply",  cron="30 5-19 * * *",    jitter=120,  tags=["llm"],     timeout=1800),
    dict(name="apply-tests",    command=["python", "/app/services/apply_tests.py", "--apply", "--limit", "10"],
         feature="tests",  cron="0 6-18/3 * * *",   jitter=90,   tags=["browser"], timeout=1800),
    dict(name="giga",           command=["python", "/app/services/giga_recruiter.py"],
         feature="giga",   cron="*/3 5-19 * * *",   jitter=60,   tags=["llm"],     timeout=1800),
    dict(name="getmatch",       command=["python", "/app/services/getmatch_apply.py"],
         feature="getmatch", cron="20 6-18/4 * * *", jitter=120,  tags=[],          timeout=1200),
    dict(name="monitor",        command=["python", "/app/services/monitor.py"],
         feature=None,     cron="0 5 * * *",        jitter=0,    tags=[],          timeout=600),
    dict(name="funnel",         command=["python", "/app/services/funnel.py"],
         feature=None,     cron="5 5 * * *",        jitter=0,    tags=[],          timeout=600),
    dict(name="send-digest",    command=["python", "/app/services/send_digest.py"],
         feature="notify", cron="15,45 5-19 * * *", jitter=0,    tags=[],          timeout=300),
    dict(name="health-check",   command=["python", "/app/services/health_check.py"],
         feature=None,     cron="0 9,17 * * *",     jitter=0,    tags=[],          timeout=300),
    dict(name="auto-screen",    command=["python", "/app/services/auto_screen.py", "--live"],
         feature="giga",   cron="50 9-18/3 * * *",  jitter=400,  tags=["llm"],     timeout=1900),
    dict(name="habr",           command=["python", "/app/services/habr_apply.py"],
         feature="habr",   cron="35 6-18/4 * * *",  jitter=200,  tags=["llm"],     timeout=1200),
    dict(name="habr-chat",      command=["python", "/app/services/habr_chat.py"],
         feature="habr_chat", cron="15 8-20/3 * * *", jitter=200, tags=["llm"],    timeout=600),
    dict(name="tg-channels",    command=["python", "/app/services/tg_channels.py", "--live"],
         feature="tg_channels", cron="40 9-19/4 * * *", jitter=300, tags=["llm"],  timeout=1200),
]
JOBS_BY_NAME: dict[str, dict] = {j["name"]: j for j in JOBS}


@task(retries=2, retry_delay_seconds=[30, 120])
async def run_target(job_name: str, target: str):
    job = JOBS_BY_NAME[job_name]
    feat = job.get("feature")
    try:
        await run_op(job["command"], job.get("platform", "hh"), target, timeout=job["timeout"])
    except Exception as e:
        if feat:  # хартбит источника: прогон упал
            try:
                pgconn.record_health(feat, False, repr(e)[:160], account=target)
            except Exception:
                pass
        raise
    if feat:  # хартбит источника: прогон прошёл
        try:
            pgconn.record_health(feat, True, account=target)
        except Exception:
            pass


@flow(on_failure=[notify_failure])
async def dispatch(job_name: str):
    """Один ран = одна задача: джиттер -> активные цели -> per-target сабтаски."""
    job = JOBS_BY_NAME[job_name]
    platform = job.get("platform", "hh")
    await human_jitter(job["jitter"])
    targets = active_targets(platform, job["feature"])
    tagged = run_target.with_options(tags=[platform, *job["tags"]])
    futures = [tagged.submit(job_name, t) for t in targets]
    # Изолируем падения: одна цель упала — остальные идут, флоу не валится из-за одной.
    for fut in futures:
        fut.result(raise_on_failure=False)


def _flow_name(job: dict) -> str:
    return f"{job.get('platform', 'hh')}-{job['name']}"


def build_deployments(names: set[str] | None):
    """Деплойменты для serve(). names — короткие имена задач (как в ORCH_ENABLED);
    None -> все. Имя деплоймента — '<platform>-<name>', параметр — job_name."""
    deployments = []
    for job in JOBS:
        if names is not None and job["name"] not in names:
            continue
        deployments.append(
            dispatch.to_deployment(
                name=_flow_name(job),
                parameters={"job_name": job["name"]},
                cron=job["cron"],
                tags=[job.get("platform", "hh"), *job["tags"]],
            )
        )
    return deployments
