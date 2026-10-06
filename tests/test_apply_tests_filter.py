"""Отклики на тесты фильтруются так же, как обычный авто-отклик."""
import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
_pa = types.ModuleType("playwright.async_api")
_pa.async_playwright = None
sys.modules.setdefault("playwright.async_api", _pa)


@pytest.fixture
def at(monkeypatch):
    mod = importlib.import_module("apply_tests")
    settings = {"apply.excluded_title_terms": "менеджер, курьер", "apply.fit_check": False}
    monkeypatch.setattr(mod.pgconn, "get_setting", lambda k, d=None, **kw: settings.get(k, d))
    monkeypatch.setattr(mod.pgconn, "seen_keys", lambda kind: set())
    return mod


def _v(i, name, wf=None):
    return {"id": i, "name": name, "work_format": [{"id": x} for x in (wf or [])]}


def test_filters_stop_words_format_and_stack(at):
    tvs = [_v(1, "Аналитик данных", ["REMOTE"]), _v(2, "Менеджер по продажам", ["REMOTE"]),
           _v(3, "Аналитик данных", ["ON_SITE"]), _v(4, "Программист 1С", ["REMOTE"])]
    cfg = {"preferences": {"work_format": ["remote"]}}
    out = asyncio.run(at._filter_like_apply(tvs, cfg, {"title": "Аналитик данных"}, "SQL, Python", {}))
    # 1С отсекается только при включённом fit_check — здесь он выключен
    assert [v["id"] for v in out] == [1, 4]


def test_login_redirect_detected(at):
    assert at.is_login_url("https://spb.hh.ru/account/login?postponed=&backurl=%2Fsearch%2Fvacancy")
    assert not at.is_login_url("https://hh.ru/applicant/vacancy_response?vacancyId=1")
    assert not at.is_login_url("https://hh.ru/applicant/resumes")
