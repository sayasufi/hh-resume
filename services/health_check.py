"""Дневной чек здоровья источников: шлёт в личку, когда источник сломан (down/warn),
чтобы ничего не вставало молча. Считает то же, что секция «Источники» в кабинете
(_source_health), и нотифицирует с dedup_key на источник (не спамит).

Запуск: python /app/services/health_check.py   (обычно через Prefect JOBS, feature=None).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

from hh_applicant_tool.storage import pgconn
from services.web_app import _source_health


def run():
    account = pgconn.get_account()
    sources = _source_health(account)
    broken = []
    for s in sources:
        if s["state"] in ("down", "warn"):
            broken.append(s["src"])
            reason = s["label"] + (f" — {s['detail']}" if s.get("detail") else "")
            prio = pgconn.PRIORITY_HIGH if s["state"] == "down" else pgconn.PRIORITY_MED
            pgconn.notify(
                prio,
                f"⚠️ Источник «{s['src']}»: {reason}. Проверь — иначе отклики по нему не идут.",
                category="action", dedup_key=f"health:{s['src']}")
    print(f"health[{account}]: " +
          ("проблемы — " + ", ".join(broken) if broken else "все источники ок"))


if __name__ == "__main__":
    run()
