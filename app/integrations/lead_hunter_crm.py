"""Separate READ ONLY client for Lead Hunter; CRM Analyst's client is unchanged.

Legacy list methods are used deliberately for explicitly selected PHONE/WEB
multifields. No generic batch, write method, arbitrary URL or redirect is exposed.
"""
import asyncio
from urllib.parse import urlsplit
import re

import httpx


LIST_METHODS = frozenset({'crm.company.list', 'crm.contact.list', 'crm.lead.list',
                         'crm.deal.list', 'crm.activity.list', 'crm.status.list', 'user.get',
                         'crm.timeline.comment.list', 'crm.deal.productrows.get',
                         'crm.quote.list', 'crm.quote.productrows.get',
                         'crm.company.contact.items.get', 'crm.deal.contact.items.get'})
OBJECT_METHODS = frozenset({'crm.company.get', 'crm.contact.get', 'crm.lead.get',
                           'crm.company.fields', 'crm.contact.fields', 'crm.lead.fields',
                           'crm.deal.fields'})
ENVELOPE_METHODS = {'crm.category.list': 'categories'}
READ_METHODS = LIST_METHODS | OBJECT_METHODS | frozenset(ENVELOPE_METHODS)


class CRMReadError(Exception):
    """Safe error with no remote text, request URL or webhook value."""


class LeadHunterCRMClient:
    def __init__(self, webhook_url: str, timeout: float = 10, transport=None,
                 pause: float = .35, max_pages: int = 1000):
        parsed = urlsplit(webhook_url.strip())
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or not re.fullmatch(r'/rest/\d+/[A-Za-z0-9_-]+/?', parsed.path)):
            raise CRMReadError('Invalid HTTPS webhook configuration')
        self._base_url = webhook_url.strip().rstrip('/')
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False,
                                      headers={'User-Agent': 'CRM-Consulting-Lead-Hunter/1.2'})
        self.pause = pause
        self.max_pages = max_pages
        self.request_count = 0
        self.method_counts = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._http.aclose()

    async def read(self, method: str, parameters: dict) -> dict:
        if method not in READ_METHODS:
            raise CRMReadError('Method blocked by Lead Hunter READ ONLY allowlist')
        for attempt in range(3):
            if self.request_count and self.pause:
                await asyncio.sleep(self.pause)
            self.request_count += 1
            self.method_counts[method] = self.method_counts.get(method, 0) + 1
            try:
                response = await self._http.post(f'{self._base_url}/{method}.json', json=parameters)
            except httpx.RequestError:
                raise CRMReadError('CRM network/timeout failure') from None
            if response.status_code == 429 or response.status_code in {502, 503, 504}:
                if attempt < 2:
                    delay = response.headers.get('Retry-After', '')
                    await asyncio.sleep(min(60, max(1, float(delay))) if delay.isdigit() else 2 ** attempt)
                    continue
            if response.status_code != 200:
                raise CRMReadError(f'CRM HTTP {response.status_code}; redirects are not followed')
            try:
                data = response.json()
            except ValueError:
                raise CRMReadError('CRM returned invalid JSON') from None
            if not isinstance(data, dict):
                raise CRMReadError('CRM response must be an object')
            if 'error' in data:
                # Never expose error_description, which may echo credentials/input.
                code = str(data['error'])
                if code in {'QUERY_LIMIT_EXCEEDED', 'TOO_MANY_REQUESTS'} and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                safe = code if re.fullmatch(r'[A-Z_]{1,60}', code) else 'API_ERROR'
                raise CRMReadError(f'CRM rejected read request: {safe}')
            result = data.get('result')
            if method in LIST_METHODS:
                if not isinstance(result, list) or not all(isinstance(x, dict) for x in result):
                    raise CRMReadError('CRM list response has an unexpected shape')
            elif method in OBJECT_METHODS:
                if not isinstance(result, dict):
                    raise CRMReadError('CRM object response has an unexpected shape')
                if method.endswith('.fields') and not all(isinstance(v, dict) for v in result.values()):
                    raise CRMReadError('CRM field descriptions have an unexpected shape')
                if method.endswith('.get') and str(result.get('ID')) != str(parameters.get('id')):
                    raise CRMReadError('CRM object ID does not match the requested record')
            else:
                key = ENVELOPE_METHODS[method]
                if (not isinstance(result, dict) or not isinstance(result.get(key), list)
                        or not all(isinstance(x, dict) for x in result[key])):
                    raise CRMReadError('CRM envelope response has an unexpected shape')
            return data
        raise CRMReadError('CRM retry limit reached')

    async def pages(self, method: str, parameters: dict):
        if method not in LIST_METHODS or method.endswith('productrows.get') or method.endswith('items.get'):
            raise CRMReadError('Pagination requires an allowlisted paginated list method')
        start = 0
        seen = set()
        for _ in range(self.max_pages):
            if start in seen:
                raise CRMReadError('Repeated CRM pagination cursor')
            seen.add(start)
            page = await self.read(method, dict(parameters, start=start))
            yield page['result']
            cursor = page.get('next')
            if cursor in (None, ''):
                return
            if not page['result'] or isinstance(cursor, bool):
                raise CRMReadError('Invalid CRM pagination continuation')
            try:
                next_start = int(cursor)
            except (TypeError, ValueError):
                raise CRMReadError('Invalid CRM pagination cursor') from None
            if str(next_start) != str(cursor) or next_start <= start:
                raise CRMReadError('CRM pagination did not advance')
            start = next_start
        raise CRMReadError('CRM page limit reached; scan is incomplete')
