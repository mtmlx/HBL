import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mtm_hbl.clickup_connector.client import ClickUpClient
from mtm_hbl.config import Settings


@pytest.mark.parametrize('failure', ['upload', 'assign', 'readback', None])
def test_replace_preserves_old_until_exact_new_id_confirmed(monkeypatch, tmp_path, failure):
    pdf = tmp_path / 'same-name.pdf'
    pdf.write_bytes(b'pdf')
    events = []
    ids = ['old']
    client = ClickUpClient(Settings(clickup_workspace_id='workspace'), 'test')
    async def get_task(_):
        events.append(('read', list(ids)))
        return SimpleNamespace(field_by_id=lambda _: SimpleNamespace(
            value=[{'id': i, 'title': pdf.name} for i in ids]))
    client.get_task = get_task

    def request(req):
        import json
        if req.url.path.endswith('/attachments'):
            events.append(('upload', None))
            return httpx.Response(503 if failure == 'upload' else 200, json={'id': 'new'})
        value = json.loads(req.content)['value']
        events.append(('write', value))
        if 'add' in value:
            if failure == 'assign':
                return httpx.Response(503)
            if failure != 'readback':
                ids.extend(value['add'])
                ids.append('concurrent-other')
        if 'rem' in value:
            assert 'new' in ids
            ids[:] = [i for i in ids if i not in value['rem']]
        return httpx.Response(200, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(request), **kw))
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    if failure:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            asyncio.run(client.upload_attachment_to_custom_field('task', 'field', str(pdf)))
        assert 'old' in ids
        assert not any(kind == 'write' and 'rem' in value for kind, value in events)
    else:
        asyncio.run(client.upload_attachment_to_custom_field('task', 'field', str(pdf)))
        assert ids == ['new', 'concurrent-other']
        assert events.index(('read', ['old', 'new', 'concurrent-other'])) < events.index(('write', {'rem': ['old']}))
