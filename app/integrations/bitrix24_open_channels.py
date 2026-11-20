"""Open Channels REST transport, separate from both read-only CRM clients."""

import asyncio
import re
from urllib.parse import urlsplit

import httpx


CONNECTORS = {'telegrambot': 'telegram', 'fbinstagramdirect': 'instagram', 'facebook': 'facebook'}
READS = frozenset({'scope', 'imopenlines.config.list.get', 'imopenlines.crm.chat.get',
                   'imopenlines.session.history.get', 'imconnector.status'})
SEND = 'imopenlines.crm.message.add'


class OpenChannelsError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def positive_id(value):
    if not re.fullmatch(r'[1-9][0-9]{0,17}', str(value)):
        raise OpenChannelsError('unexpected_response')
    return int(value)


class OpenChannelsClient:
    def __init__(self, webhook, *, app_token=None, send_chat_ids=(), timeout=10, transport=None, pause=.5):
        webhook = webhook.strip().rstrip('/')
        match = re.fullmatch(r'https://[A-Za-z0-9.-]+/rest/([1-9][0-9]*)/[A-Za-z0-9_-]+', webhook)
        if not match:
            raise OpenChannelsError('invalid_configuration')
        self._webhook = webhook
        self._app_token = app_token
        self._app_endpoint = f'https://{urlsplit(webhook).hostname}/rest'
        self._user_id = int(match[1])
        self._send_chat_ids = frozenset(send_chat_ids)
        # Use the transport directly so httpx's INFO request log cannot include the webhook path.
        self._transport = transport if transport is not None else httpx.AsyncHTTPTransport(trust_env=False)
        self._timeout = timeout
        self._pause = pause
        self._called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._transport.aclose()

    async def _call(self, method, params, *, app=False):
        if self._called and self._pause:
            await asyncio.sleep(self._pause)
        self._called = True
        url = self._app_endpoint if app else self._webhook
        request = httpx.Request('POST', f'{url}/{method}.json',
            json={**params, 'auth': self._app_token} if app else params,
            extensions={'timeout': dict.fromkeys(('connect', 'read', 'write', 'pool'), self._timeout)})
        try:
            response = await self._transport.handle_async_request(request)
            try:
                await response.aread()
                data = response.json()
            finally:
                await response.aclose()
        except (httpx.HTTPError, OSError):
            raise OpenChannelsError('network_error') from None
        except ValueError:
            raise OpenChannelsError('unexpected_response') from None
        code = str(data.get('error', '')).casefold() if isinstance(data, dict) else ''
        if code == 'insufficient_scope':
            raise OpenChannelsError('insufficient_scope')
        if response.status_code in {401, 403} or code in {'access_denied', 'invalid_credentials', 'expired_token', 'wrong_auth_type'}:
            raise OpenChannelsError('authorization_denied')
        if response.status_code == 429 or code in {'query_limit_exceeded', 'too_many_requests'}:
            raise OpenChannelsError('rate_limited')
        if response.status_code != 200 or not isinstance(data, dict) or code or 'result' not in data:
            raise OpenChannelsError('upstream_rejected')
        return data['result']

    async def read(self, method, params=None):
        if method not in READS:
            raise OpenChannelsError('method_blocked')
        if method == 'imconnector.status' and not self._app_token:
            raise OpenChannelsError('application_context_required')
        return await self._call(method, params or {}, app=method == 'imconnector.status')

    async def reply(self, *, chat_id, target, message):
        self.require_send(chat_id)
        result = await self._call(SEND, {**target, 'CHAT_ID': chat_id, 'USER_ID': self._user_id, 'MESSAGE': message})
        return positive_id(result)

    def require_send(self, chat_id):
        if chat_id not in self._send_chat_ids:
            raise OpenChannelsError('test_send_disabled')
