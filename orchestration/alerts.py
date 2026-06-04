"""Алерт о падении флоу -> очередь уведомлений (тот же канал, что и дайджест)."""
from hh_applicant_tool.storage import pgconn


def notify_failure(flow, flow_run, state) -> None:
    """on_failure-хук флоу. Кладёт 🔴-уведомление; доставит send_digest.
    Best-effort: сбой алерта не должен ломать оркестрацию."""
    try:
        params = getattr(flow_run, "parameters", None) or {}
        job = params.get("job_name") or getattr(flow_run, "name", "") or "flow"
        pgconn.notify(
            pgconn.PRIORITY_HIGH,
            f"Оркестрация: задача «{job}» упала ({getattr(state, 'name', 'Failed')})",
            category="orchestration",
            dedup_key=f"orch-fail:{getattr(flow_run, 'id', job)}",
        )
    except Exception:
        pass
