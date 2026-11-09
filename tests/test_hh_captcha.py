import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hh_applicant_tool.api import client as api
from hh_applicant_tool.api.errors import CaptchaRequired
from hh_applicant_tool.utils import hh_captcha as captcha


def test_only_hh_captcha_urls_are_allowed():
    assert captcha.valid_captcha_url('https://hh.ru/account/captcha?state=example')
    for url in ('http://hh.ru/account/captcha', 'https://example.com/account/captcha',
                'https://hh.ru.evil.example/account/captcha', 'https://user@hh.ru/account/captcha',
                'https://hh.ru:444/account/captcha', 'https://hh.ru:bad/account/captcha',
                'https://hh.ru/oauth/token'):
        assert not captcha.valid_captcha_url(url)


def test_image_solver_polls_and_uses_russian_pool(monkeypatch):
    monkeypatch.setattr(captcha.asyncio, 'sleep', AsyncMock())
    calls = []
    def respond(request):
        import json
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        data = ({'errorId': 0, 'taskId': 123} if len(calls) == 1 else
                {'errorId': 0, 'status': 'processing'} if len(calls) == 2 else
                {'errorId': 0, 'status': 'ready', 'solution': {'text': 'two words'}})
        return httpx.Response(200, json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await captcha.solve_image(client, 'test-key', b'png-image') == 'two words'
    asyncio.run(run())
    assert calls[0][1]['languagePool'] == 'rn'
    assert all(body['taskId'] == 123 for _, body in calls[1:])


@pytest.mark.parametrize('solved', [True, False])
def test_api_retries_blocked_post_only_after_success(monkeypatch, solved):
    solver = AsyncMock(return_value=solved)
    monkeypatch.setattr(captcha, 'solve_captcha', solver)
    notifications = []
    monkeypatch.setattr(api, '_notify_captcha', notifications.append)
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(403, json={'errors': [{'type': 'captcha_required',
                'value': 'captcha_required', 'captcha_url': 'https://hh.ru/account/captcha?state=test'}]})
        return httpx.Response(201, json={})
    async def run():
        client = api.ApiClient(access_token='USERtest', client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            if solved:
                assert await client.post('/negotiations', {'vacancy_id': '1', 'resume_id': 'r'}) == {}
            else:
                with pytest.raises(CaptchaRequired):
                    await client.post('/negotiations', {'vacancy_id': '1', 'resume_id': 'r'})
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == (2 if solved else 1)
    assert len(notifications) == (0 if solved else 1)
    if solved:
        assert calls[0].content == calls[1].content
    solver.assert_awaited_once()


def test_rejected_retry_does_not_loop_or_pay_again(monkeypatch):
    solver = AsyncMock(return_value=True)
    monkeypatch.setattr(captcha, 'solve_captcha', solver)
    monkeypatch.setattr(api, '_notify_captcha', lambda error: None)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(403, json={'errors': [{'value': 'captcha_required',
            'captcha_url': 'https://hh.ru/account/captcha?state=test'}]})
    async def run():
        client = api.ApiClient(access_token='USERtest', client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            with pytest.raises(CaptchaRequired):
                await client.get('/me')
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(calls) == 2
    solver.assert_awaited_once()


@pytest.mark.parametrize('locked,recent,expected', [(False, False, False),
    (True, True, False), (True, False, True)])
def test_account_lock_and_cooldown_prevent_duplicate_solver_charges(monkeypatch, locked, recent, expected):
    import time
    from hh_applicant_tool.storage import pgconn
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (locked,)
    monkeypatch.setattr(pgconn, 'connect', lambda: conn)
    monkeypatch.setattr(pgconn, 'get_account', lambda: 'test-account')
    def setting(key, default=None, account=None):
        if key == 'habr.2captcha_key':
            return 'test-key'
        return time.time() if recent else 0
    monkeypatch.setattr(pgconn, 'get_setting', setting)
    monkeypatch.setattr(pgconn, 'set_setting', lambda *args: None)
    browser = AsyncMock(return_value=True)
    monkeypatch.setattr(captcha, '_solve_in_browser', browser)
    assert asyncio.run(captcha.solve_captcha('https://hh.ru/account/captcha?state=test')) == expected
    assert browser.await_count == int(expected)
    conn.close.assert_called_once()


def test_captcha_after_token_refresh_is_resolved(monkeypatch):
    solver = AsyncMock(return_value=True)
    monkeypatch.setattr(captcha, 'solve_captcha', solver)
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(403, json={'errors': [{'type': 'forbidden'}]})
        if len(requests) == 2:
            return httpx.Response(403, json={'errors': [{'value': 'captcha_required',
                'captcha_url': 'https://hh.ru/account/captcha?state=test'}]})
        return httpx.Response(200, json={'id': 'test'})
    async def refresh(client):
        client.access_token = 'USERnew'
        client.access_expires_at = 9999999999
    async def run():
        client = api.ApiClient(access_token='USERold', refresh_token='refresh',
            refresh_hook=refresh, client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            assert await client.get('/me') == {'id': 'test'}
        finally:
            await client.aclose()
    asyncio.run(run())
    assert len(requests) == 3
    assert requests[2].headers['authorization'] == 'Bearer USERnew'
    solver.assert_awaited_once()
