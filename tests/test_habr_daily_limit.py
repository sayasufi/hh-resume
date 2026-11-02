import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services'))


@pytest.mark.parametrize('sent,expected', [(0, 15), (12, 3), (15, 0), (20, 0)])
def test_habr_limit_is_daily_across_runs(monkeypatch, sent, expected):
    mod = importlib.import_module('habr_apply')
    monkeypatch.setattr(mod, 'DRY', False)
    monkeypatch.setattr(mod.pgconn, 'get_account', lambda: 'test')
    monkeypatch.setattr(mod.pgconn, 'feature_enabled', lambda *a: True)
    settings = {'habr.session': 'test', 'habr.max_per_day': 15}
    monkeypatch.setattr(mod.pgconn, 'get_setting', lambda k, d=None, **kw: settings.get(k, d))
    monkeypatch.setattr(mod.pgconn, 'app_config', lambda: {})
    monkeypatch.setattr(mod.pgconn, 'seen_keys', lambda *a: set())
    monkeypatch.setattr(mod.pgconn, 'add_seen', lambda *a: None)
    monkeypatch.setattr(mod.pgconn, 'bump_activity', lambda *a, **kw: None)
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    def execute(sql, params):
        cur.fetchone.return_value = (sent,) if 'activity_daily' in sql else (True,)
    cur.execute.side_effect = execute
    monkeypatch.setattr(mod.pgconn, 'connect', lambda: conn)
    api = SimpleNamespace(ensure_auth=AsyncMock(), aclose=AsyncMock(),
                          offers=AsyncMock(return_value=[{'id': str(i), 'title': 'Analyst'} for i in range(30)]),
                          apply=AsyncMock(return_value=SimpleNamespace(status_code=200)))
    monkeypatch.setattr(mod.habr_api, 'HabrAPI', lambda *a: api)
    monkeypatch.setattr(mod, '_record', lambda *a: None)
    monkeypatch.setattr(mod, '_gen_letter', AsyncMock(return_value=''))
    monkeypatch.setattr(mod.asyncio, 'sleep', AsyncMock())
    asyncio.run(mod.run())
    assert api.apply.await_count == expected
