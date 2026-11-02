import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh_applicant_tool.operations import apply_similar as mod
from hh_applicant_tool.api.errors import CaptchaRequired


def search_op(monkeypatch, queries):
    op = mod.Operation()
    op._full_resume = {}
    op._profile_text = ''
    op.excluded_title_terms = []
    op.wanted_wf = set()
    op.allowed_areas = set()
    op.query_chat = None
    op.tool = SimpleNamespace(storage=SimpleNamespace(settings=None))
    monkeypatch.setattr(mod.sq, 'ensure_queries', AsyncMock(return_value=queries))
    monkeypatch.setattr(mod.asyncio, 'sleep', AsyncMock())
    return op


def test_search_uses_remaining_budget_after_all_first_pages(monkeypatch):
    op = search_op(monkeypatch, ['a', 'b', 'c', 'd', 'e'])
    calls = []

    async def get(path, params):
        calls.append((params['text'], params['page']))
        return {'items': [{'id': str(len(calls))}], 'pages': 3}

    op.api_client = SimpleNamespace(get=get)
    async def collect():
        return [v async for v in op._get_search_vacancies()]
    items = asyncio.run(collect())
    assert calls[:5] == [(q, 0) for q in ['a', 'b', 'c', 'd', 'e']]
    assert len(calls) == mod.SEARCH_MAX_REQUESTS
    assert len(items) == mod.SEARCH_MAX_REQUESTS


def test_search_captcha_is_not_silently_ignored(monkeypatch):
    op = search_op(monkeypatch, ['a', 'b'])
    error = CaptchaRequired(SimpleNamespace(status_code=403),
                            {'errors': [{'value': 'captcha_required'}]})
    op.api_client = SimpleNamespace(get=AsyncMock(side_effect=error))
    async def collect():
        return [v async for v in op._get_search_vacancies()]
    with pytest.raises(CaptchaRequired):
        asyncio.run(collect())
    assert op.api_client.get.await_count == 1


def test_search_skips_exhausted_queries_and_keeps_region_roots(monkeypatch):
    op = search_op(monkeypatch, ['short', 'long'])
    op.allowed_areas = {'2019', '123'}
    op.search_areas = {'2019'}
    calls = []
    async def get(path, params):
        assert params['area'] == ['2019']
        calls.append((params['text'], params['page']))
        return {'items': [{'id': 'same'}], 'pages': 1 if params['text'] == 'short' else 2}
    op.api_client = SimpleNamespace(get=get)
    async def collect():
        return [v async for v in op._get_search_vacancies()]
    assert len(asyncio.run(collect())) == 1
    assert calls == [('short', 0), ('long', 0), ('long', 1)]


def test_selected_region_includes_descendant_cities():
    from hh_applicant_tool.utils.areas import expand_areas
    api = SimpleNamespace(get=AsyncMock(return_value={
        'id': '2019', 'areas': [{'id': '123', 'areas': [{'id': '456', 'areas': []}]}]}))
    result = asyncio.run(expand_areas(api, {'2019'}))
    assert result == {'2019', '123', '456'}
    assert '2' not in result


def test_empty_area_selection_needs_no_api_calls():
    from hh_applicant_tool.utils.areas import expand_areas
    api = SimpleNamespace(get=AsyncMock())
    assert asyncio.run(expand_areas(api, set())) == set()
    api.get.assert_not_called()


def test_captcha_stops_vacancy_loop():
    op = mod.Operation()
    op._run_deadline = float('inf')
    op.applications_count = 0
    op.max_applications_per_day = 190
    op.api_client = SimpleNamespace(get=AsyncMock(return_value={}))
    error = CaptchaRequired(SimpleNamespace(status_code=403),
                            {'errors': [{'value': 'captcha_required'}]})
    save = AsyncMock(side_effect=error)
    op.tool = SimpleNamespace(config={}, storage=SimpleNamespace(
        vacancies=SimpleNamespace(save=save)))
    op._build_profile = lambda *args: ''
    async def candidates(resume):
        yield {'id': '1'}, 'search'
        yield {'id': '2'}, 'search'
    op._iter_candidates = candidates
    with pytest.raises(CaptchaRequired):
        asyncio.run(op._apply_resume({'id': 'r', 'title': 'analyst',
                                     'alternate_url': 'resume'}, {}, set()))
    assert save.await_count == 1
