import asyncio
from unittest.mock import AsyncMock, Mock

from services import views_snapshot as mod


def test_snapshot_commits_schema_before_insert(monkeypatch):
    events = []
    cursor = Mock()
    cursor.execute.side_effect = lambda sql, *args: events.append(
        'insert' if sql.startswith('INSERT') else 'ddl')
    conn = Mock()
    conn.cursor.return_value = cursor
    conn.commit.side_effect = lambda: events.append('commit')
    api = Mock(get=AsyncMock(return_value={'items': []}), aclose=AsyncMock())
    monkeypatch.setattr(mod, 'ApiClient', lambda **kwargs: api)
    monkeypatch.setattr(mod.pgconn, 'app_config', lambda: {'token': {'access_token': 'test'}})
    monkeypatch.setattr(mod.pgconn, 'get_account', lambda: 'test')
    monkeypatch.setattr(mod.pgconn, 'connect', lambda: conn)
    asyncio.run(mod.main())
    assert events == ['ddl', 'commit', 'insert', 'commit']
    conn.close.assert_called_once()
