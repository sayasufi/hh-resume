"""Отклики на тесты фильтруются так же, как обычный авто-отклик."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock
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


def test_work_format_takes_precedence_over_schedule(at):
    v = _v(1, 'Analyst', ['HYBRID'])
    v['schedule'] = {'id': 'fullDay'}
    out = asyncio.run(at._filter_like_apply([v], {'preferences': {'work_format': ['onsite']}}, {}, '', {}))
    assert out == []


def test_test_vacancies_obey_selected_region_and_descendants(at, monkeypatch):
    original = at.pgconn.get_setting
    monkeypatch.setattr(at.pgconn, 'get_setting', lambda k, d=None, **kw:
                        '2019' if k == 'apply.area' else original(k, d, **kw))
    api = types.SimpleNamespace(get=AsyncMock(return_value={
        'id': '2019', 'areas': [{'id': '123', 'areas': []}]}))
    rows = [{**_v(1, 'Analyst'), 'area': {'id': '123'}},
            {**_v(2, 'Analyst'), 'area': {'id': '2'}}]
    out = asyncio.run(at._filter_like_apply(rows, {}, {}, '', {}, api=api))
    assert [v['id'] for v in out] == [1]


@pytest.mark.parametrize('daily,sent,per_run,expected', [
    (30, 27, 10, 3), (0, 0, 10, 0), (30, 30, 10, 0), (30, 0, 0, 0), (30, 0, 10, 10)])
def test_tests_budget_respects_daily_and_run_limits(at, monkeypatch, daily, sent, per_run, expected):
    monkeypatch.setattr(at.pgconn, 'get_setting', lambda *a, **kw: daily)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (sent,)
    monkeypatch.setattr(at.pgconn, 'connect', lambda: conn)
    monkeypatch.setattr(at.pgconn, 'get_account', lambda: 'test-account')
    assert at._daily_budget(per_run) == expected


def test_zero_daily_limit_stops_before_opening_api_or_browser(at, monkeypatch):
    monkeypatch.setattr(at, 'APPLY', True)
    monkeypatch.setattr(at.pgconn, 'feature_enabled', lambda *a: True)
    monkeypatch.setattr(at.pgconn, 'get_setting', lambda k, d=None: 0 if k == 'apply.tests_per_day' else d)
    config = MagicMock(side_effect=AssertionError('Should stop before loading credentials'))
    monkeypatch.setattr(at.pgconn, 'app_config', config)
    asyncio.run(at.main())
    config.assert_not_called()
