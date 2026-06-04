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
