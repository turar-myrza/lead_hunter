import json
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import open_channels
from app.api.universal_lead_hunter import get_application
from app.core.config import Settings, get_settings
from app.integrations.bitrix24_open_channels import CONNECTORS, SEND, OpenChannelsClient, OpenChannelsError
from app.main import app
from app.services.lead_hunter_open_channels import inventory
from app.services.sales_store import SalesStore


WEBHOOK = 'https://example.org/rest/9/mock'


@pytest.mark.parametrize('app_token', [None, 'synthetic-app-token'])
async def test_inventory_reports_confirmed_and_unknown_connector_status(app_token):
    def transport(request):
        method = request.url.path.rsplit('/', 1)[-1].removesuffix('.json')
        if method == 'imopenlines.config.list.get':
            return httpx.Response(200, json={'result': [{'ID': '7', 'LINE_NAME': 'Sales', 'ACTIVE': 'Y'}]})
        assert app_token and method == 'imconnector.status' and request.url.path == '/rest/imconnector.status.json'
        params = json.loads(request.content)
        assert params['auth'] == app_token
        return httpx.Response(200, json={'result': {**params, 'CONFIGURED': params['CONNECTOR'] != 'facebook',
                                                  'STATUS': params['CONNECTOR'] == 'telegrambot'}})

    async with OpenChannelsClient(WEBHOOK, app_token=app_token, transport=httpx.MockTransport(transport), pause=0) as client:
        result = await inventory(client)
    by_type = {item['connector_type']: item for item in result['connections']}
    assert set(by_type) == set(CONNECTORS.values())
    assert all(item['line_name'] == 'Sales' and item['line_id'] == 7 for item in by_type.values())
    if app_token:
        assert [by_type[key]['active'] for key in ('telegram', 'instagram', 'facebook')] == [True, False, False]
    else:
        assert all(item['active'] is None and item['reason'] == 'application_context_required' for item in by_type.values())
    assert app_token is None or app_token not in str(result)


@pytest.mark.parametrize(('remote_error', 'expected'), [('insufficient_scope', 'insufficient_scope'),
                                                        ('INVALID_CREDENTIALS', 'authorization_denied')])
async def test_authorization_errors_are_sanitized(remote_error, expected):
    transport = httpx.MockTransport(lambda _: httpx.Response(401, json={'error': remote_error, 'error_description': WEBHOOK}))
    async with OpenChannelsClient(WEBHOOK, transport=transport, pause=0) as client:
        with pytest.raises(OpenChannelsError, match=f'^{expected}$'):
            await client.read('imopenlines.config.list.get')
        with pytest.raises(OpenChannelsError, match='method_blocked'):
            await client.read(SEND)


@pytest.mark.parametrize('gate', ['disabled', 'changed_session', 'send', 'timeout'])
def test_link_poll_draft_and_test_reply(tmp_path, monkeypatch, gate):
    store = SalesStore(tmp_path / 'state.sqlite3')
    run_id = store.enqueue('universal_lead_hunter')
    candidate = NS(candidate=NS(id='test-prospect'), engagement=NS(permission='review_only'), qualification=NS(state='needs_review'))
    service = NS(store=store, result=lambda _: NS(mode='research', opportunities=[candidate]))
    writes, session = [], [31]

    def transport(request):
        method = request.url.path.rsplit('/', 1)[-1].removesuffix('.json')
        params = json.loads(request.content)
        if method == 'imopenlines.crm.chat.get':
            assert params['CRM_ENTITY'] == 14
            result = [{'CHAT_ID': '21', 'CONNECTOR_ID': 'telegrambot'}]
        elif method == 'imopenlines.session.history.get':
            assert params == {'CHAT_ID': 21}
            result = {'chatId': 21, 'sessionId': session[0], 'users': {'4': {'connector': True}, '9': {'connector': False}},
                      'message': {'100': {'id': '100', 'chatid': '21', 'senderid': '4', 'text': 'Test inquiry'},
                                  '101': {'id': '101', 'chatid': '21', 'senderid': '9', 'text': 'An agent message'}}}
        elif method == SEND:
            writes.append(params)
            if gate == 'timeout':
                raise httpx.ReadTimeout('Transport failure')
            result = 102
        else:
            pytest.fail('Unexpected Bitrix24 method')
        return httpx.Response(200, json={'result': result})

    monkeypatch.setattr(open_channels, 'client_from_settings', lambda _:
                        OpenChannelsClient(WEBHOOK, send_chat_ids=() if gate == 'disabled' else {21},
                                           transport=httpx.MockTransport(transport), pause=0))
    app.dependency_overrides.update({get_application: lambda: service,
                                     get_settings: lambda: Settings(_env_file=None, lead_hunter_api_token='fixture-access')})
    route = f'/api/v1/lead-hunter/runs/{run_id}/prospects/test-prospect/open-channel'
    try:
        with TestClient(app) as client:
            assert client.get(route + '/history').status_code == 401
            client.headers['Authorization'] = 'Bearer fixture-access'
            assert client.post(route, json={'crm_id': 14, 'crm_type': 'lead', 'chat_id': 22}).status_code == 409
            assert client.post(route, json={'crm_id': 14, 'crm_type': 'lead', 'chat_id': 21}).status_code == 200
            assert len(client.get(route + '/history').json()['messages']) == 2
            assert [item['id'] for item in client.post(route + '/poll').json()['incoming']] == [100]
            assert client.post(route + '/poll').json()['incoming'] == []
            assert client.get(route + '/receipts').json() == [{'message_id': 100, 'session_id': 31, 'at': None}]
            draft = client.post(route + '/drafts', json={'text': 'Reviewed test reply.'}).json()
            send_url = route + f"/drafts/{draft['id']}/send"
            assert client.post(send_url, json={'approved': False}).status_code == 422
            assert not writes
            if gate == 'changed_session':
                session[0] += 1
            response = client.post(send_url, json={'approved': True})
            assert response.status_code == {'disabled': 403, 'changed_session': 409, 'send': 200, 'timeout': 502}[gate]
            if gate in {'send', 'timeout'}:
                assert writes == [{'CRM_ENTITY': 14, 'CRM_ENTITY_TYPE': 'lead', 'USER_ID': 9,
                                   'CHAT_ID': 21, 'MESSAGE': 'Reviewed test reply.'}]
                assert client.post(send_url, json={'approved': True}).status_code == 409
                assert len(writes) == 1
            else:
                assert not writes
    finally:
        app.dependency_overrides.pop(get_application)
        app.dependency_overrides.pop(get_settings)
