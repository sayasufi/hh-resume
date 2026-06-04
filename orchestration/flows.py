"""Диспетчер-флоу на расписании: читает активные цели (платформа×фича) и раздаёт
работу per-target задаче, которая запускает существующую операцию как subprocess.
JOBS — единственный источник правды по задачам (см. таблицу в плане/спеке §4.5).
Сейчас единственная платформа — hh; новые источники/обработчики = новые JOBS-строки."""
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
