from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from app.integrations.lead_hunter_public import PublicResponse, PublicSourceError
from app.services import sales_discovery as discovery


ARTICLE = 'https://press.kz/news/123'
SITE = 'https://granitsnab.kz/'
CONTACT = SITE + 'contacts/'
EMPLOYER_NEWS = SITE + 'news/25/'
CHECKED = datetime(2026, 11, 16, 6, tzinfo=timezone.utc)
HOME = '''<title>Гранит Снаб · Металлопрокат</title><h1>Оптовые поставки</h1>
<p>Каталог металлопроката</p><a href="/request/">Оставить заявку</a>
<nav><a href="/contacts/">Контакты</a></nav>'''
CONTACTS = '''<h1>Контакты отдела продаж</h1>
<a href="tel:+77172123456">+7 (7172) 123456</a>
<address>Электронная почта: sales@granitsnab.kz</address>
<a href="mailto:hr@granitsnab.kz">Отдел кадров</a>'''


class PublicPages:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    async def fetch(self, url, *, media):
        self.requests.append(url)
        if urlsplit(url).hostname == 'api.gdeltproject.org':
            return PublicResponse(url, 200, 'application/json', self.pages.get('GDELT', '{"articles":[]}').encode(), CHECKED)
        body = self.pages[url]
        if isinstance(body, Exception):
            raise body
        return PublicResponse(url, 200, 'text/html', body.encode(), CHECKED)


@pytest.mark.asyncio
async def test_article_link_preserves_evidence_from_official_company(monkeypatch):
    monkeypatch.setattr(discovery, 'DEFAULT_FEEDS', {})
    article = f'''<meta property="article:published_time" content="2026-11-13T09:00:00+05:00">
        <p>ТОО «<a href="{SITE}">Гранит Снаб</a>» открыл новый филиал в Астане.</p>
        <p>Номер журналиста: +77001234567.</p>'''
    fetcher = PublicPages({'GDELT':f'{{"articles":[{{"url":"{ARTICLE}"}}]}}', ARTICLE:article,
                           SITE:HOME, CONTACT:CONTACTS})

    prospects, events = await discovery.discover_national(fetcher=fetcher)

    assert len(prospects) == 1
    candidate = prospects[0]
    assert candidate.city == 'Астана'
    assert candidate.site == SITE and candidate.phones == ['+77172123456']
    assert candidate.emails == ['sales@granitsnab.kz']
    assert candidate.process == 'Запрос на металлопрокат' and len(candidate.visual_steps) == 3
    assert candidate.sources[0].published == '2026-11-13'
    assert [x.url for x in candidate.sources] == [ARTICLE, SITE, CONTACT]
    assert candidate.sources[-1].evidence_type == 'company_contact'
    assert 'sales@granitsnab.kz' in candidate.sources[-1].fact
    assert events[-1]['verified_contacts'] == 1


@pytest.mark.asyncio
async def test_employer_news_registered_source_with_visible_date(monkeypatch):
    monkeypatch.setattr(discovery, 'DEFAULT_FEEDS', {})
    article = '''<title>Гранит Снаб открыл филиал в Астане</title>
        <span class="news-date">13 ноября 2026</span><h1>Гранит Снаб открыл филиал в Астане</h1>'''
    fetcher = PublicPages({EMPLOYER_NEWS:article, SITE:HOME, CONTACT:CONTACTS})
    sources = [{'kind':'employer', 'employer':'Гранит Снаб', 'city':'Астана', 'url':EMPLOYER_NEWS}]

    prospects, _ = await discovery.discover_national(fetcher=fetcher, sources=sources)

    assert len(prospects) == 1 and prospects[0].contact_status == 'public_business_contact'
    assert prospects[0].sources[0].url == EMPLOYER_NEWS
    assert prospects[0].sources[0].published == '2026-11-13'


@pytest.mark.asyncio
async def test_external_contact_and_mismatched_brand_do_not_become_proof(monkeypatch):
    monkeypatch.setattr(discovery, 'DEFAULT_FEEDS', {})
    article = f'''<meta property="article:published_time" content="2026-11-13T09:00:00+05:00">
        <p>ТОО «<a href="{SITE}">Гранит Снаб</a>» открыл новый филиал в Астане.</p>'''
    fetcher = PublicPages({'GDELT':f'{{"articles":[{{"url":"{ARTICLE}"}}]}}', ARTICLE:article,
                           SITE:'<h1>Другая компания</h1><a href="/contacts/">Контакты</a>'})
    sources = [{'kind':'employer', 'company':'Гранит Снаб', 'city':'Астана', 'url':EMPLOYER_NEWS,
                'contact_url':'https://independent.kz/contacts/'}]

    prospects, events = await discovery.discover_national(fetcher=fetcher, sources=sources)

    assert len(prospects) == 1 and not prospects[0].site and not prospects[0].phones
    assert CONTACT not in fetcher.requests and EMPLOYER_NEWS not in fetcher.requests
    assert events[0]['status'] == 'invalid_registry'


@pytest.mark.asyncio
async def test_inaccessible_contacts_keep_verified_site_without_contact_status(monkeypatch):
    monkeypatch.setattr(discovery, 'DEFAULT_FEEDS', {})
    article = '''<title>Гранит Снаб открыл филиал в Астане</title>
        <span>13 ноября 2026</span><h1>Гранит Снаб открыл филиал в Астане</h1>'''
    fetcher = PublicPages({EMPLOYER_NEWS:article, SITE:HOME,
                           CONTACT:PublicSourceError('Public provider blocked this run (HTTP 403)')})
    sources = [{'kind':'employer', 'company':'Гранит Снаб', 'city':'Астана', 'url':EMPLOYER_NEWS}]

    prospects, events = await discovery.discover_national(fetcher=fetcher, sources=sources)

    assert len(prospects) == 1 and prospects[0].site == SITE
    assert not prospects[0].phones and not prospects[0].emails
    assert prospects[0].contact_status == 'research_only'
    assert events[-1]['verified_contacts'] == 0


@pytest.mark.asyncio
async def test_verified_site_contacts_are_enough_without_following_an_extra_page(monkeypatch):
    monkeypatch.setattr(discovery, 'DEFAULT_FEEDS', {})
    article = '''<title>Гранит Снаб открыл филиал в Астане</title>
        <span>13 ноября 2026</span><h1>Гранит Снаб открыл филиал в Астане</h1>'''
    fetcher = PublicPages({EMPLOYER_NEWS:article, SITE:HOME + CONTACTS})
    sources = [{'kind':'employer', 'company':'Гранит Снаб', 'city':'Астана', 'url':EMPLOYER_NEWS,
                'contact_url':CONTACT}]

    prospects, _ = await discovery.discover_national(fetcher=fetcher, sources=sources)

    assert len(prospects) == 1 and prospects[0].emails == ['sales@granitsnab.kz']
    assert CONTACT not in fetcher.requests
    assert prospects[0].sources[-1].url == SITE


@pytest.mark.asyncio
async def test_registered_employer_index_only_reads_dated_same_site_signals(monkeypatch):
    monkeypatch.setattr(discovery, 'DEFAULT_FEEDS', {})
    index = SITE + 'news/'
    article = '''<title>Гранит Снаб открыл филиал в Астане</title>
        <span>13 ноября 2026</span><h1>Гранит Снаб открыл филиал в Астане</h1>'''
    fetcher = PublicPages({index:'<a href="/news/25/">Открыли филиал</a>'
                                 '<a href="https://unrelated.kz/news/5/">Открыли склад</a>',
                           EMPLOYER_NEWS:article, SITE:HOME, CONTACT:CONTACTS})
    sources = [{'kind':'employer', 'company':'Гранит Снаб', 'city':'Астана',
                'url':index, 'path_prefix':'/news/'}]

    prospects, events = await discovery.discover_national(fetcher=fetcher, sources=sources)

    assert len(prospects) == 1 and prospects[0].sources[0].url == EMPLOYER_NEWS
    assert 'https://unrelated.kz/news/5/' not in fetcher.requests
    assert events[0]['results'] == 1
