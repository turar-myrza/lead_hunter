"""Public-source acceptance without sockets, credentials or CRM."""
from datetime import datetime, timedelta, timezone
import json
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from app.integrations import lead_hunter_public as public
from app.services.lead_hunter_discovery import (
    ALMATY, DiscoveryFinding, EmployerSiteAdapter, GdeltAdapter, PublicIndexAdapter, RssAdapter,
    collect_findings, discover, verify_findings,
)


NOW = datetime.now(timezone.utc)
DAY = NOW.astimezone(ALMATY).date()
NEWS = 'https://publication.kz/news/123'
EMPLOYER = 'https://employer.kz/career/job-12'
COMPANY = 'ТОО «Вымышленный бизнес»'


def response(url, body, media='text/html', status=200):
    return public.PublicResponse(url, status, media, body.encode() if isinstance(body, str) else body, NOW)


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url, *, media):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        assert value.media_type in media
        return value


def finding(lane='A', url=NEWS):
    return DiscoveryFinding(lane=lane, provider='fixture', url=url, title='Подсказка выдачи', found_at=NOW)


def article(sentence, publication=DAY):
    meta = f'<meta property="article:published_time" content="{publication}T08:00:00+05:00">' if publication else ''
    return f'<html><head>{meta}</head><body><h1>Новости производства</h1><p>{sentence}</p></body></html>'


def employer_job(description, *, publication=DAY, locality='Шымкент', valid_until=None, status=''):
    data = {'@type': 'JobPosting', 'datePosted': publication.isoformat() if publication else None,
            'hiringOrganization': {'name': COMPANY, 'sameAs': 'https://employer.kz/'},
            'jobLocation': {'address': {'addressLocality': locality}}}
    if valid_until:
        data['validThrough'] = valid_until.isoformat()
    return (f'<html><head><script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script></head>'
            f'<body><h1>Вакансия — {COMPANY}</h1><p>Город: Шымкент</p><p>{description}</p><p>{status}</p></body></html>')


@pytest.mark.asyncio
async def test_gdelt_lanes_use_real_documented_endpoint_and_distinct_queries():
    class GdeltFixture:
        calls = []

        async def fetch(self, url, *, media):
            self.calls.append(url)
            return response(url, json.dumps({'articles': [{'url': NEWS, 'title': 'Company mention', 'seendate': '20260101T000000Z'},
                    {'url': 'https://hh.kz/vacancy/123', 'title': 'Restricted job board'}]}), 'application/json')
    fetcher = GdeltFixture()
    result = await collect_findings('Шымкент', 2, adapters=[GdeltAdapter()], fetcher=fetcher)
    assert result.providers_succeeded == ['gdelt-doc:A', 'gdelt-doc:B']
    assert [(item.lane, item.url) for item in result.findings] == [('A', NEWS), ('B', NEWS)]
    parsed = [urlsplit(url) for url in fetcher.calls]
    assert all(url.scheme == 'https' and url.netloc == 'api.gdeltproject.org' and url.path == '/api/v2/doc/doc' for url in parsed)
    queries = [parse_qs(url.query) for url in parsed]
    assert 'sales automation' in queries[0]['query'][0] and 'Bitrix24' in queries[1]['query'][0]
    assert all(q['mode'] == ['artlist'] and q['format'] == ['json'] and 'apikey' not in q for q in queries)
    assert all(item.found_at == NOW for item in result.findings)  # GDELT seen date is not publication.


@pytest.mark.asyncio
async def test_public_rss_and_index_remain_discovery_only_and_same_publisher():
    feed = 'https://publication.kz/feed/'
    index = 'https://publication.kz/news/'
    xml = f'<rss><channel><item><title>Открытие нового склада</title><link>{NEWS}</link></item>' \
          '<item><title>Новый филиал</title><link>https://foreign.kz/news/1</link></item></channel></rss>'
    html = '<a href="/news/123">Открытие нового склада</a><a href="/news/456">Битрикс24 в Шымкенте</a>'
    fetcher = FakeFetcher({feed: response(feed, xml, 'application/rss+xml'), index: response(index, html)})
    result = await collect_findings('Шымкент', 2, adapters=[RssAdapter('rss', feed, ('publication.kz',)),
                       PublicIndexAdapter('index', index, '/news/')], fetcher=fetcher)
    assert {(item.lane, item.url) for item in result.findings} == {('A', NEWS), ('B', 'https://publication.kz/news/456')}
    assert not result.candidates
    assert not result.providers_failed


@pytest.mark.asyncio
async def test_feed_rejects_dtd_and_does_not_claim_success():
    url = 'https://publication.kz/feed/'
    xml = '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item>&x;</item></channel></rss>'
    fetcher = FakeFetcher({url: response(url, xml, 'application/rss+xml')})
    result = await collect_findings('Шымкент', 1, lanes=('A',), adapters=[RssAdapter('rss', url, ('publication.kz',))], fetcher=fetcher)
    assert result.providers_failed == ['rss:A']
    assert not result.providers_succeeded and not result.findings
    assert 'entities' in result.skipped_sources[0]


@pytest.mark.asyncio
async def test_only_known_government_rss_article_path_is_mapped_before_fetch():
    feed = 'https://www.gov.kz/api/v1/public/rss/shymkent/news/ru'
    legacy = 'https://www.gov.kz/memleket/entities/shymkent/press/news/details/123?lang=ru'
    canonical = 'https://www.gov.kz/org/shymkent/press/news/details/123?lang=ru'
    other = 'https://www.gov.kz/memleket/entities/almaty/press/news/details/124?lang=ru'
    foreign = legacy.replace('www.gov.kz', 'government-kz.com')
    items = ''.join(f'<item><title>Открытие филиала</title><link>{url}</link></item>' for url in [legacy, other, foreign])
    fetcher = FakeFetcher({feed: response(feed, f'<rss><channel>{items}</channel></rss>', 'application/rss+xml'),
                           canonical: response(canonical, article(f'{COMPANY} открыл филиал в Шымкенте.'))})
    discovered = await collect_findings('Шымкент', 2, lanes=('A',), adapters=[RssAdapter('official', feed, ('www.gov.kz',))], fetcher=fetcher)
    assert [(item.url, item.discovered_url) for item in discovered.findings] == [(canonical, legacy), (other, None)]
    verified = await verify_findings('Шымкент', discovered.findings[:1], 1, fetcher=fetcher)
    assert verified.candidates[0].sources[0].url == canonical
    assert fetcher.calls == [feed, canonical]


@pytest.mark.asyncio
async def test_dated_local_entity_fact_becomes_candidate_and_callback_not_proposal():
    raw = article(f'{COMPANY} открыл новый складской комплекс в Шымкенте.')
    fetcher = FakeFetcher({NEWS: response(NEWS, raw)})
    callbacks = []
    async def checkpoint(candidate):
        callbacks.append(candidate.name)
    result = await verify_findings('Шымкент', [finding()], 2, fetcher=fetcher, on_candidate=checkpoint)
    candidate, = result.candidates
    assert callbacks == [COMPANY]
    assert candidate.name == COMPANY and candidate.city == 'Шымкент'
    assert candidate.sources[0].published_on == DAY and candidate.sources[0].checked_at.tzinfo == ALMATY
    assert candidate.evidence[0].signal == 'growth'
    assert candidate.commercial_proposal is None and not candidate.purchase_evidence
    assert candidate.crm_check.state == 'not_checked'
    assert candidate.crm_usage_evidence.status == 'unknown'
    assert not candidate.contacts  # A news mention isn't a company contact source.


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    article(f'{COMPANY} открыл филиал в Таразе.'),
    article(f'{COMPANY} открыл филиал в Шымкенте.', None),
    article(f'{COMPANY} открыл филиал в Шымкенте.', DAY-timedelta(days=31)),
    article(f'{COMPANY} открыл филиал в Шымкенте.', DAY+timedelta(days=1)),
    article(f'{COMPANY} не открыл филиал в Шымкенте.'),
    article(f'{COMPANY} и ТОО «Другой бизнес» открыл филиал в Шымкенте.'),
    article(f'<span hidden>{COMPANY} открыл филиал в Шымкенте.</span>'),
    article(f'<script>{COMPANY} открыл филиал в Шымкенте.</script>'),
])
async def test_ambiguous_hidden_undated_old_or_wrong_city_fact_is_not_promoted(raw):
    fetcher = FakeFetcher({NEWS: response(NEWS, raw)})
    result = await verify_findings('Шымкент', [finding()], 2, fetcher=fetcher)
    assert not result.candidates and len(result.skipped_sources) == 1
    assert not result.verification_incomplete


@pytest.mark.asyncio
async def test_current_bitrix_use_and_separate_project_are_not_same_signal():
    body = employer_job('Ведение клиентской базы в Битрикс24. Компания интегрирует WhatsApp-звонки.', status='Вакансия актуальна')
    fetcher = FakeFetcher({EMPLOYER: response(EMPLOYER, body)})
    result = await discover('Шымкент', 1, lanes=('B',), adapters=[EmployerSiteAdapter('own-site', COMPANY, (EMPLOYER,))], fetcher=fetcher)
    candidate, = result.candidates
    assert candidate.crm_usage_evidence.status == 'bitrix24'
    assert {e.signal.value for e in candidate.evidence} == {'existing_crm', 'automation_project'}
    assert candidate.sources[0].vacancy_status == 'active'
    assert candidate.sources[0].status_evidence == 'Вакансия актуальна'
    assert candidate.commercial_proposal is None


@pytest.mark.asyncio
async def test_bitrix_presence_only_does_not_manufacture_current_need():
    body = employer_job('Ведение клиентской базы в Битрикс24.')
    result = await verify_findings('Шымкент', [finding('B', EMPLOYER)], 1, fetcher=FakeFetcher({EMPLOYER: response(EMPLOYER, body)}))
    candidate, = result.candidates
    assert [e.signal.value for e in candidate.evidence] == ['existing_crm']
    assert any('отдельного текущего сигнала потребности' in text for text in candidate.limitations)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    employer_job('Требуется опыт работы в Битрикс24.'),
    employer_job('Компания планирует работать в Битрикс24.'),
    employer_job('Компания не работает в Битрикс24.'),
    employer_job('Ведение клиентской базы в Битрикс24.', publication=DAY-timedelta(days=91)),
    employer_job('Ведение клиентской базы в Битрикс24.', status='Вакансия закрыта'),
    employer_job('Ведение клиентской базы в Битрикс24.', valid_until=DAY-timedelta(days=1)),
    employer_job('Ведение клиентской базы в Битрикс24.', locality='Тараз'),
])
async def test_operational_bitrix_evidence_must_be_current_and_local(body):
    result = await discover('Шымкент', 1, lanes=('B',), adapters=[EmployerSiteAdapter('own-site', COMPANY, (EMPLOYER,))],
                            fetcher=FakeFetcher({EMPLOYER: response(EMPLOYER, body)}))
    assert not result.candidates and result.skipped_sources


@pytest.mark.asyncio
async def test_only_configured_employer_page_supplies_published_business_contact():
    url = 'https://employer.kz/news/branch'
    body = article(f'{COMPANY} открыл новый филиал в Шымкенте.<address>Телефон: +7 700 123 45 67; e-mail: info@employer.kz</address>')
    result = await discover('Шымкент', 1, lanes=('A',), adapters=[EmployerSiteAdapter('own-site', COMPANY, (url,))],
                            fetcher=FakeFetcher({url: response(url, body)}))
    candidate, = result.candidates
    assert {contact.type for contact in candidate.contacts} == {'website', 'phone', 'email'}
    assert sum(len(line.split()) for line in candidate.sources[0].excerpt.splitlines()) <= 25


@pytest.mark.asyncio
async def test_retryable_verification_is_not_complete_and_stops_before_later_findings():
    fetcher = FakeFetcher({NEWS: public.PublicSourceError('Public request budget exhausted', retryable=True)})
    result = await verify_findings('Шымкент', [finding(), finding('A', 'https://publication.kz/news/2')], 2, fetcher=fetcher)
    assert result.verification_incomplete and not result.candidates
    assert fetcher.calls == [NEWS]


@pytest.mark.asyncio
async def test_permanent_source_stop_is_logged_without_retry_checkpoint():
    fetcher = FakeFetcher({NEWS: public.PublicSourceError('Public provider blocked this run (HTTP 403 / challenge)')})
    result = await verify_findings('Шымкент', [finding()], 2, fetcher=fetcher)
    assert not result.verification_incomplete and not result.candidates
    assert 'HTTP 403' in result.skipped_sources[0]


@pytest.mark.parametrize('url', ['http://public.kz/', 'https://host.local/', 'https://user:secret@public.kz/',
    'https://public.kz:444/', 'https://public.kz/?access_token=secret', 'https://public.kz/%72est/1/secret',
    'https://public.kz/%252e%252e/hidden', 'https://public.kz\\@evil.kz/'])
def test_reject_unsafe_urls_before_fetch(url):
    with pytest.raises(public.PublicSourceError):
        public.public_https_url(url)


@pytest.mark.parametrize('bad', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '100.64.0.1', '224.0.0.2',
                               '::1', 'fd00::1', '::ffff:127.0.0.1', '64:ff9b::a9fe:a9fe', '2002:a9fe:a9fe::1'])
def test_one_private_or_tunneled_dns_answer_blocks_entire_host(monkeypatch, bad):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        (socket.AF_INET6 if ':' in bad else socket.AF_INET, socket.SOCK_STREAM, 6, '', (bad, 443)),
    ])
    with pytest.raises(public.PublicSourceError, match='not publicly routable') as error:
        public.resolve_public_addresses('public.kz')
    assert error.value.retryable is False


def test_tls_socket_uses_exact_validated_ip_and_original_hostname(monkeypatch):
    trace = []
    class Peer:
        def getpeername(self):
            return ('8.8.8.8', 443)
    peer = Peer()
    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            trace.append(server_hostname)
            return raw
    monkeypatch.setattr(public.ssl, 'create_default_context', Context)
    def connect(addr, timeout):
        trace.append(addr)
        return peer
    monkeypatch.setattr(socket, 'create_connection', connect)
    conn = public._PinnedHTTPSConnection('public.kz', '8.8.8.8', 5)
    conn.connect()
    assert conn.sock is peer
    assert trace == [('8.8.8.8', 443), 'public.kz']


@pytest.mark.asyncio
async def test_robots_rules_check_encoded_paths_before_article_and_cache(monkeypatch):
    seen = []
    def request(url, **kwargs):
        seen.append(url)
        if url.endswith('/robots.txt'):
            return response(url, 'User-agent: *\nDisallow: /hidden/*\nAllow: /hidden/approved$\n', 'text/plain')
        return response(url, '<p>Allowed</p>')
    monkeypatch.setattr(public, '_request', request)
    fetcher = public.PublicFetcher(min_delay=0)
    with pytest.raises(public.PublicSourceError, match='robots.txt disallows'):
        await fetcher.fetch('https://public.kz/%68idden/%73ecret')
    await fetcher.fetch('https://public.kz/hidden/approved')
    await fetcher.fetch('https://public.kz/hidden/approved')
    assert seen == ['https://public.kz/robots.txt', 'https://public.kz/hidden/approved']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(401, ''), (403, ''), (429, ''), (200, '<title>Just a moment...</title>')])
async def test_access_restriction_stops_host_without_retry(monkeypatch, status, body):
    seen = []
    def request(url, **kwargs):
        seen.append(url)
        return response(url, '', 'text/plain', status=404) if url.endswith('/robots.txt') else response(url, body, status=status)
    monkeypatch.setattr(public, '_request', request)
    fetcher = public.PublicFetcher(min_delay=0)
    for suffix in ('/one', '/two'):
        with pytest.raises(public.PublicSourceError, match='blocked') as error:
            await fetcher.fetch('https://public.kz' + suffix)
        assert error.value.retryable is False
    assert seen == ['https://public.kz/robots.txt', 'https://public.kz/one']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,retryable', [(301, False), (404, False), (503, True)])
async def test_redirects_are_not_followed_and_server_error_is_retryable(monkeypatch, status, retryable):
    seen = []
    def request(url, **kwargs):
        seen.append(url)
        return response(url, '', 'text/plain', status=404 if url.endswith('/robots.txt') else status)
    monkeypatch.setattr(public, '_request', request)
    fetcher = public.PublicFetcher(min_delay=0)
    with pytest.raises(public.PublicSourceError) as error:
        await fetcher.fetch('https://public.kz/article')
    assert error.value.retryable is retryable
    assert seen == ['https://public.kz/robots.txt', 'https://public.kz/article']
