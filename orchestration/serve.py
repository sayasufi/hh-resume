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
    print(f"serve: {len(deployments)} deployment(s): {[d.name for d in deployments]}")
    serve(*deployments)


if __name__ == "__main__":
    main()
