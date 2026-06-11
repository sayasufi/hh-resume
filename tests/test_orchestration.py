import asyncio
import logging

import pytest

import orchestration.targets as targets
from orchestration.jitter import human_jitter
from orchestration.runner import run_op


def test_package_imports():
    import orchestration  # noqa: F401


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
    import orchestration.runner as runner
    monkeypatch.setattr(runner, "get_run_logger", lambda: logging.getLogger("test"))
    with pytest.raises(RuntimeError):
        asyncio.run(run_op(["python", "-c", "import sys; sys.exit(3)"], "hh", "acct", timeout=30))


def test_run_op_success(monkeypatch):
    import orchestration.runner as runner
    monkeypatch.setattr(runner, "get_run_logger", lambda: logging.getLogger("test"))
    rc = asyncio.run(run_op(["python", "-c", "print('hi')"], "hh", "acct", timeout=30))
    assert rc == 0


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
