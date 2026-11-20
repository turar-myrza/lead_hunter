"""Public URL discovery and evidence verification, independent of CRM and LLMs."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import inspect
import json
import re
from typing import Literal, Protocol
from urllib.parse import urlencode, urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from app.integrations.lead_hunter_public import PublicFetcher, PublicResponse, PublicSourceError, public_https_url
from app.models.lead_hunter import Candidate, Record


LANES = ('A', 'B')
CITY = 'Шымкент'
CITY_RE = re.compile(r'(?<![\w-])шымкент(?:е|а|у|ом|тегі|те|тің)?(?![\w-])', re.I)
ALMATY = ZoneInfo('Asia/Almaty')
_MEDIA_XML = ('application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml')
_HTML = ('text/html', 'application/xhtml+xml')
_GDELT = 'https://api.gdeltproject.org/api/v2/doc/doc'
_GOV_FEED = 'https://www.gov.kz/api/v1/public/rss/shymkent/news/ru'
_GOV_RSS_LEGACY_ARTICLE = re.compile(r'/memleket/entities/shymkent/press/news/details/([1-9][0-9]{0,14})/?')
# The DOC index searches English translations. URLs/titles are discovery only.
_QUERIES = {
    'A': '(Shymkent OR Chimkent) ("sales automation" OR "business process automation" OR "new branch" OR "customer requests" OR "new factory" OR "new warehouse")',
    'B': '(Shymkent OR Chimkent) (Bitrix24 OR "Bitrix 24")',
}
_BITRIX = r'(?:bitrix\s*24|битрикс\s*24)'
_B_HINT = re.compile(_BITRIX, re.I)
_A_HINT = re.compile(r'автоматиз|цифровиз|внедр|нов(?:ый|ая|ое|ого)|откры|запуск|производств|филиал|склад|өтінім|автоматтан|жаңа|филиал|warehouse|automation|branch|factory', re.I)
_DENIED_SUFFIXES = ('hh.kz', 'hh.ru', 'headhunter.kz', 'finder.work', '2gis.kz', '2gis.ru', 'olx.kz',
                    'instagram.com', 'facebook.com', 'linkedin.com', 't.me', 'wa.me')
_LEGAL = re.compile(r'(?<!\w)(?:ТОО|АО|LLP|JSC)\s+[«“\"]([^»”\"\n]{2,90})[»”\"]', re.I)
_PHONE = re.compile(r'(?:\+7|8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)')
_EMAIL = re.compile(r'(?<![\w.@])([a-z0-9._%+\-]+@[a-z0-9.-]+\.[a-z]{2,})(?![\w.-])', re.I)
_SECTOR = (('Медицина', r'клиник|медицинск|диагностическ'), ('Образование', r'образовательн|учебн|школ'),
           ('Производство', r'производств|завод|фабрик'), ('Торговля', r'оптов|рознич|торгов'),
           ('Логистика', r'логистик|складск|грузоперевоз'), ('Сервис', r'сервисн|обслуживан'))


class DiscoveryFinding(Record):
    lane: Literal['A', 'B']
    provider: str = Field(min_length=1, max_length=80)
    url: str
    title: str = Field(default='', max_length=300)
    query: str = Field(default='', max_length=600)
    found_at: datetime
    discovered_url: str | None = None
    # Only a configured first-party employer adapter may provide this hint.
    employer: str | None = Field(default=None, min_length=2, max_length=120)
    _url = field_validator('url')(public_https_url)

    @field_validator('discovered_url')
    @classmethod
    def validate_discovered_url(cls, value):
        return public_https_url(value) if value is not None else None

    @field_validator('found_at')
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError('Discovery time must be timezone-aware')
        return value


class DiscoveryResult(Record):
    findings: list[DiscoveryFinding] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    search_log: list[str] = Field(default_factory=list)
    skipped_sources: list[str] = Field(default_factory=list)
    providers_succeeded: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)
    verification_incomplete: bool = False


class DiscoveryAdapter(Protocol):
    name: str

    async def search(self, city: str, lane: str, limit: int, fetcher: PublicFetcher) -> list[DiscoveryFinding]: ...


def _plain(value: str) -> str:
    return ' '.join(value.split())


def _source_allowed(url):
    host = urlsplit(url).hostname or ''
    return not any(host == suffix or host.endswith('.' + suffix) for suffix in _DENIED_SUFFIXES)


def _finding(url, title, lane, provider, query, checked_at, employer=None, discovered_url=None):
    try:
        url = public_https_url(url)
    except PublicSourceError:
        return None
    if not _source_allowed(url):
        return None
    return DiscoveryFinding(url=url, title=_plain(title)[:300], lane=lane, provider=provider,
                            query=query[:600], found_at=checked_at, employer=employer, discovered_url=discovered_url)


@dataclass(frozen=True)
class GdeltAdapter:
    name: str = 'gdelt-doc'

    async def search(self, city, lane, limit, fetcher):
        query = _QUERIES[lane]
        url = _GDELT + '?' + urlencode({'query': query, 'mode': 'artlist', 'format': 'json',
                                       'timespan': '30d', 'maxrecords': min(limit, 40), 'sort': 'datedesc'})
        response = await fetcher.fetch(url, media=('application/json',))
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise PublicSourceError('Invalid GDELT JSON') from None
        if not isinstance(payload, dict) or not isinstance(payload.get('articles'), list):
            raise PublicSourceError('Unrecognized GDELT response')
        found = []
        for item in payload['articles'][:limit]:
            if not isinstance(item, dict) or not isinstance(item.get('url'), str) or not isinstance(item.get('title'), str):
                continue
            finding = _finding(item['url'], item['title'], lane, self.name, query, response.checked_at)
            if finding:
                found.append(finding)
        return found


def _xml_root(data):
    try:
        text = data.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
            raise PublicSourceError('XML DTD/entities are prohibited')
        return ElementTree.fromstring(text)
    except (UnicodeError, ElementTree.ParseError):
        raise PublicSourceError('Invalid public XML feed') from None


def _feed_items(data):
    root = _xml_root(data)
    if root.tag.rsplit('}', 1)[-1] not in {'rss', 'feed', 'RDF'}:
        raise PublicSourceError('Public XML is not RSS/Atom')
    for item in root.iter():
        if item.tag.rsplit('}', 1)[-1] not in {'item', 'entry'}:
            continue
        title, url = '', ''
        for field in item:
            tag = field.tag.rsplit('}', 1)[-1]
            if tag == 'title':
                title = _plain(' '.join(field.itertext()))[:300]
            elif tag == 'link' and field.attrib.get('rel', 'alternate') == 'alternate':
                url = (field.attrib.get('href') or field.text or '').strip()
        if title and url:
            yield title, url


@dataclass(frozen=True)
class RssAdapter:
    name: str
    url: str
    # The publisher advertises these article hosts. External feed links are ignored.
    article_hosts: tuple[str, ...]

    async def search(self, city, lane, limit, fetcher):
        response = await fetcher.fetch(self.url, media=_MEDIA_XML)
        hints = _A_HINT if lane == 'A' else _B_HINT
        findings = []
        for title, link in _feed_items(response.body):
            try:
                url = public_https_url(urljoin(self.url, link))
            except ValueError:
                continue
            parsed = urlsplit(url)
            if parsed.hostname not in self.article_hosts or not hints.search(title):
                continue
            original = None
            if (self.url == _GOV_FEED and parsed.hostname == 'www.gov.kz'
                    and (match := _GOV_RSS_LEGACY_ARTICLE.fullmatch(parsed.path))):
                # First-party canonical URL for this one publisher and numeric article ID.
                # Do not process redirect headers or allow arbitrary URL rewrites.
                original = url
                url = f'https://www.gov.kz/org/shymkent/press/news/details/{match.group(1)}?lang=ru'
            item = _finding(url, title, lane, self.name, 'RSS/Atom: заголовок', response.checked_at,
                            discovered_url=original)
            if item:
                findings.append(item)
            if len(findings) >= limit:
                break
        return findings


class _Page(HTMLParser):
    """Small bounded parser: visible paragraphs and public, structured metadata."""
    _TEXT = {'title', 'h1', 'h2', 'h3', 'p', 'li', 'td', 'address', 'blockquote', 'time'}
    _HIDDEN = {'script', 'style', 'iframe', 'form', 'template', 'noscript', 'svg', 'head', 'nav'}

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.blocks, self.links, self.metadata, self.jsonld = [], [], {}, []
        self._block = None
        self._link = None
        self._skip = []
        self._json = None
        self.feed(body.decode('utf-8', errors='replace'))
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = ('hidden' in attrs or (attrs.get('aria-hidden') or '').casefold() == 'true'
                  or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style') or '', re.I))
        if tag in self._HIDDEN or hidden:
            self._skip.append(tag)
            if tag == 'script' and attrs.get('type') == 'application/ld+json':
                self._json = []
            return
        if tag == 'meta' and self._skip in ([], ['head']):
            key = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').casefold()
            if key in {'article:published_time', 'datepublished', 'dateposted', 'og:title'} and attrs.get('content'):
                self.metadata.setdefault(key, []).append(attrs['content'])
        if self._skip and not (tag == 'title' and self._skip == ['head']):
            return
        if tag in self._TEXT and self._block is None:
            self._block = (tag, [])
            if tag == 'time' and 'datepublished' in (attrs.get('itemprop') or '').casefold() and attrs.get('datetime'):
                self.metadata.setdefault('datepublished', []).append(attrs['datetime'])
        if tag == 'a' and attrs.get('href') and len(self.links) < 1000:
            self._link = (attrs['href'], [])

    def handle_data(self, data):
        if self._json is not None:
            if len(data) <= 131072:
                self._json.append(data)
            return
        if self._skip and not (self._block and self._block[0] == 'title' and self._skip == ['head']):
            return
        if self._block:
            self._block[1].append(data)
        if self._link:
            self._link[1].append(data)

    def handle_endtag(self, tag):
        if self._skip and tag == self._skip[-1]:
            self._skip.pop()
            if tag == 'script' and self._json is not None:
                value = ''.join(self._json)
                self._json = None
                if len(value) <= 131072 and len(self.jsonld) < 10:
                    try:
                        self.jsonld.append(json.loads(value))
                    except (ValueError, RecursionError):
                        pass
            return
        if self._block and tag == self._block[0]:
            value = _plain(' '.join(self._block[1]))
            self._block = None
            if value and len(self.blocks) < 300:
                self.blocks.append((tag, value[:5000]))
        if tag == 'a' and self._link:
            href, fragments = self._link
            self._link = None
            if label := _plain(' '.join(fragments)):
                self.links.append((href, label[:300]))

    def structured(self):
        pending = [(node, 0) for node in self.jsonld]
        processed = 0
        while pending and processed < 100:
            node, depth = pending.pop()
            processed += 1
            if depth > 4:
                continue
            if isinstance(node, dict):
                yield node
                if '@graph' in node:
                    pending.append((node['@graph'], depth + 1))
            elif isinstance(node, list):
                pending.extend((child, depth + 1) for child in node[:40])


@dataclass(frozen=True)
class PublicIndexAdapter:
    name: str
    url: str
    article_path: str

    async def search(self, city, lane, limit, fetcher):
        response = await fetcher.fetch(self.url, media=_HTML)
        page = _Page(response.body)
        host = urlsplit(self.url).hostname
        hints = _A_HINT if lane == 'A' else _B_HINT
        found = []
        for href, title in page.links:
            try:
                url = public_https_url(urljoin(self.url, href))
            except (PublicSourceError, ValueError):
                continue
            parsed = urlsplit(url)
            if parsed.hostname != host or not parsed.path.startswith(self.article_path) or not hints.search(title):
                continue
            item = _finding(url, title, lane, self.name, 'Публичный индекс: заголовок', response.checked_at)
            if item:
                found.append(item)
            if len(found) >= limit:
                break
        return found


@dataclass(frozen=True)
class EmployerSiteAdapter:
    """Configured official employer page(s), never a job-board results scraper."""
    name: str
    company: str
    pages: tuple[str, ...]

    async def search(self, city, lane, limit, fetcher):
        findings = []
        for url in self.pages[:limit]:
            # Only the verification stage reads and classifies these public pages.
            item = _finding(url, self.company, lane, self.name, 'Официальная страница работодателя',
                            datetime.now(timezone.utc), employer=self.company)
            if item:
                findings.append(item)
        return findings


DEFAULT_ADAPTERS = (
    GdeltAdapter(),
    RssAdapter('shymkent-gov-rss', _GOV_FEED, ('www.gov.kz',)),
    RssAdapter('otyrar-rss', 'https://otyrar.kz/feed/', ('otyrar.kz', 'www.otyrar.kz')),
)


def _bounds(city, limit, lanes=LANES):
    if city.strip().casefold() not in {'шымкент', 'shymkent'}:
        raise ValueError('Only Shymkent is configured for discovery')
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('Discovery candidate limit must be between 1 and 30')
    if not lanes or len(set(lanes)) != len(lanes) or any(lane not in LANES for lane in lanes):
        raise ValueError('Discovery lanes must be A, B or both')


async def collect_findings(city, limit, *, lanes=LANES, adapters=None, fetcher=None, as_of=None):
    _bounds(city, limit, lanes)
    fetcher = fetcher or PublicFetcher()
    adapters = DEFAULT_ADAPTERS if adapters is None else tuple(adapters)
    result = DiscoveryResult()
    groups = []
    for lane in lanes:
        for adapter in adapters:
            stage = f'{adapter.name}:{lane}'
            query = _QUERIES[lane] if isinstance(adapter, GdeltAdapter) else getattr(adapter, 'url', 'официальные страницы')
            try:
                found = await adapter.search(CITY, lane, min(max(6, limit * 2), 40), fetcher)
            except (PublicSourceError, ValueError, UnicodeError) as exc:
                result.providers_failed.append(stage)
                reason = str(exc) if isinstance(exc, PublicSourceError) else 'Неразбираемый публичный ответ'
                result.skipped_sources.append(f'{stage}: {reason}')
                result.search_log.append(f'{stage}: {query}; источник недоступен/не принят.')
                continue
            result.providers_succeeded.append(stage)
            result.search_log.append(f'{stage}: {query}; обнаружено ссылок: {len(found)}.')
            groups.append(list(found))
    # Round-robin: one large provider/track cannot consume the entire URL budget.
    seen = set()
    for index in range(max(map(len, groups), default=0)):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            key = (item.url, item.lane)
            if key not in seen:
                seen.add(key)
                result.findings.append(item)
            if len(result.findings) >= min(60, max(10, limit * 6)):
                return result
    return result


def _types(node):
    value = node.get('@type')
    return {value} if isinstance(value, str) else {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def _date_value(value):
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[T ])', value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _publication(page, nodes, checked_day, *, job=None):
    dates = set()
    if job is not None:
        parsed = _date_value(job.get('datePosted'))
        return parsed if parsed and parsed <= checked_day else None
    for name in ('article:published_time', 'datepublished'):
        dates.update(value for raw in page.metadata.get(name, []) if (value := _date_value(raw)))
    for node in nodes:
        if _types(node) & {'Article', 'NewsArticle', 'BlogPosting'}:
            if value := _date_value(node.get('datePublished')):
                dates.add(value)
    if len(dates) != 1:
        return None
    value = dates.pop()
    return value if value <= checked_day else None


_NEGATIVE = re.compile(r'\bне\s+(?:откры|запус|внедр|использ|работ|вед|фикс|теря|пропуска)|\b(?:отмен[её]н|отложен|неактуаль)', re.I)
_HIRING_SKILL = re.compile(r'\b(?:опыт|знание|навык|желательно|приветствуется|требовани)', re.I)
_B_USE = re.compile(rf'(?:(?:работ(?:аем|ает|ают|ать|а|у)|вед(?:[её]м|[её]т|ут|ение)|вести|фиксиру\w*|уч[её]т\w*)[^.!?;]{{0,70}}?\bв\s+(?:crm\s+)?{_BITRIX}|использу(?:ем|ет|ют)\s+(?:crm\s+)?{_BITRIX})', re.I)
_B_FUTURE = re.compile(r'\b(?:планиру\w*|предстоит|хотим|внедрить|будет\s+использ)', re.I)
_A_SIGNALS = (
    ('processing_problem', re.compile(r'(?:теря(?:ем|ет|ют|ются)|пропуска(?:ем|ет|ют))\s+(?:часть\s+)?(?:заявк\w*|обращени\w*|звонк\w*)', re.I),
     'В источнике прямо описаны пропущенные обращения.'),
    ('automation_project', re.compile(r'(?:внедря(?:ет|ют|ем)|начал[аи]?\s+внедрение|планирует\s+внедрить|запускает\s+проект\s+автоматизации|автоматизиру(?:ет|ют|ем))\s+(?:[^.!?;]{0,35}?\b)?(?:crm\b|обработк\w*\s+(?:заявок|обращений|заказов)|процесс\w*\s+продаж|складск\w*\s+уч[её]т\w*)', re.I),
     'Источник описывает проект CRM или автоматизации процесса.'),
    ('growth', re.compile(r'(?:откры(?:л[аи]?|вает|вают)|запустил[аи]?|расширя(?:ет|ют))\s+(?:нов\w*\s+)?(?:филиал\w*|отдел\s+продаж|производственн\w*\s+площадк\w*|завод\w*|складск\w*\s+комплекс\w*)', re.I),
     'Источник сообщает о запуске или расширении местной площадки.'),
)
_B_PROJECT = re.compile(r'(?:интегриру(?:ет|ют|ем)|настраива(?:ет|ют|ем)|внедря(?:ет|ют|ем)|автоматизиру(?:ет|ют|ем))\s+'
                        r'(?:[^.!?;]{0,40}?\b)?(?:whatsapp[- ]звонк\w*|ip[- ]телефони\w*|телефони\w*|'
                        r'чат[- ]бот\w*|обработк\w*\s+(?:заявок|обращений|заказов)|процесс\w*\s+продаж)', re.I)
_CLOSED = re.compile(r'вакансия\s+(?:в архиве|закрыта|архивная|недоступна)', re.I)
_ACTIVE = re.compile(r'вакансия\s+(?:актуальна|открыта|активна)(?!\s+до\s+\d)', re.I)
_CRM_GENERIC = re.compile(r'(?:вед(?:[её]м|[её]т|ут|ение)|вести|работ(?:аем|ает|ают|ать|а|у)|фиксиру\w*)[^.!?;]{0,70}?\bв\s+CRM\b', re.I)


def _sentences(blocks):
    for tag, text in blocks:
        if tag in {'title', 'time', 'address'}:
            continue
        yield from (part.strip() for part in re.split(r'(?<=[.!?])\s+|[\n\r]+', text) if part.strip())


def _job_subject(nodes, host):
    jobs = [node for node in nodes if 'JobPosting' in _types(node)]
    if len(jobs) > 1:
        raise PublicSourceError('На странице несколько вакансий; контекст одной компании не выделен')
    if len(jobs) != 1:
        return None, None
    job = jobs[0]
    employer = job.get('hiringOrganization')
    if not isinstance(employer, dict) or not isinstance(employer.get('name'), str):
        return job, None
    url = employer.get('sameAs') or employer.get('url')
    if not isinstance(url, str):
        return job, None
    try:
        if urlsplit(public_https_url(url)).hostname != host:
            return job, None
    except PublicSourceError:
        return job, None
    # Never borrow a corporate HQ address: require this vacancy's local workplace.
    places = job.get('jobLocation')
    places = [places] if isinstance(places, dict) else places if isinstance(places, list) else []
    for location in places:
        address = location.get('address') if isinstance(location, dict) else None
        if (isinstance(address, dict) and isinstance(address.get('addressLocality'), str)
                and CITY_RE.fullmatch(re.sub(r'^(?:г\.\s*)|\s+(?:қаласы|қ\.)$', '', address['addressLocality'].strip(), flags=re.I))):
            return job, employer['name'].strip()
    return job, None


def _short_quote(phrase, limit=15):
    return phrase if 1 <= len(phrase.split()) <= limit and len(phrase) <= 500 else None


def _candidate(finding: DiscoveryFinding, response: PublicResponse):
    page = _Page(response.body)
    nodes = list(page.structured())
    host = urlsplit(finding.url).hostname
    job, structured_name = _job_subject(nodes, host)
    if job and finding.employer and structured_name and finding.employer.casefold() != structured_name.casefold():
        raise PublicSourceError('Наименование работодателя расходится с публичной вакансией')
    employer = structured_name if job else finding.employer
    checked_at = response.checked_at.astimezone(ALMATY)
    checked_day = checked_at.date()
    published_on = _publication(page, nodes, checked_day, job=job)
    valid_until = _date_value(job.get('validThrough')) if job else None
    if valid_until is not None and valid_until < checked_day:
        raise PublicSourceError('Публичная вакансия истекла')
    blocks = [text for _, text in page.blocks]
    job_page = bool(job or re.search(r'/(?:vacanc(?:y|ies)|jobs|careers?|vakansii)(?:/|$)', urlsplit(finding.url).path, re.I)
                    or any(tag == 'h1' and re.search(r'\bвакансия\b', text, re.I) for tag, text in page.blocks))
    city_matches = [(text, CITY_RE.search(text)) for text in blocks if CITY_RE.search(text)]
    if not city_matches:
        raise PublicSourceError('На первичной странице не подтверждён Шымкент')
    if job_page and any(_CLOSED.search(text) for text in blocks):
        raise PublicSourceError('Публичная вакансия закрыта/архивная')
    if job_page and (not published_on or (checked_day - published_on).days > 90):
        raise PublicSourceError('Дата вакансии не установлена либо источник старше 90 дней')
    # A typed job posting is usable only from the employer's own site.
    if job_page and not employer:
        raise PublicSourceError('Не подтверждена официальная локальная страница работодателя')

    selected = None
    for sentence in _sentences(page.blocks):
        if len(sentence) > 500 or _NEGATIVE.search(sentence):
            continue
        legal = list(_LEGAL.finditer(sentence))
        if employer:
            if len(legal) > 1 or (legal and employer.casefold() not in legal[0].group().casefold()
                                 and legal[0].group(1).casefold() not in employer.casefold()):
                continue
            identity = next((match.group() for text in blocks if (match := re.search(re.escape(employer), text, re.I))), None)
            if not identity:
                continue
            name = employer
            city_quote = city_matches[0][1].group()
        else:
            # A press article must explicitly attribute the same scoped fact to one legal entity.
            city = CITY_RE.search(sentence)
            if len(legal) != 1 or not city:
                continue
            identity = legal[0].group()
            name = identity
            city_quote = city.group()

        bitrix_match = _B_USE.search(sentence) if not (_HIRING_SKILL.search(sentence) or _B_FUTURE.search(sentence)) else None
        if finding.lane == 'B':
            if not bitrix_match or not (phrase := _short_quote(bitrix_match.group())):
                continue
            selected = (name, identity, city_quote, phrase, 'existing_crm',
                        'Операционная работа с Битрикс24 прямо указана в источнике.', phrase)
            break
        if published_on is None or (checked_day - published_on).days > 30:
            continue
        # If existing CRM use is explicit in this fact, do not label it first implementation.
        if bitrix_match or _CRM_GENERIC.search(sentence):
            continue
        for signal, pattern, fact in _A_SIGNALS:
            if (match := pattern.search(sentence)) and (phrase := _short_quote(match.group())):
                selected = (name, identity, city_quote, phrase, signal, fact, None)
                break
        if selected:
            break
    if not selected:
        reason = ('Не подтверждено операционное использование Битрикс24' if finding.lane == 'B'
                  else 'Нет датированного локального сигнала проблемы, проекта или роста с однозначной компанией')
        raise PublicSourceError(reason)
    name, identity, city_quote, signal_quote, signal, signal_fact, bitrix = selected
    if not _short_quote(identity, 12):
        raise PublicSourceError('Идентификационная цитата превышает локальный лимит')

    quotes = list(dict.fromkeys([identity, city_quote, signal_quote]))
    if sum(len(q.split()) for q in quotes) > 25:
        raise PublicSourceError('Подтверждение превышает лимит цитирования источника')
    evidence = lambda quote, fact: {'source_id': 'primary', 'quote': quote, 'fact': fact}
    signals = [{**evidence(signal_quote, signal_fact), 'signal': signal}]
    if bitrix and published_on and (checked_day - published_on).days <= 30:
        rules = (*_A_SIGNALS, ('automation_project', _B_PROJECT, 'Источник описывает текущую интеграцию каналов или автоматизацию.'))
        for sentence in _sentences(page.blocks):
            if len(sentence) > 500 or _NEGATIVE.search(sentence) or _HIRING_SKILL.search(sentence):
                continue
            names = [match.group() for match in _LEGAL.finditer(sentence)]
            if (not employer and (not CITY_RE.search(sentence) or names != [identity])) or (employer and any(
                    employer.casefold() not in name.casefold() and name.casefold() not in employer.casefold() for name in names)):
                continue
            for kind, pattern, fact in rules:
                if any(entry['signal'] == kind for entry in signals):
                    continue
                if (match := pattern.search(sentence)) and (quote := _short_quote(match.group(), 10)):
                    if sum(len(q.split()) for q in quotes) + len(quote.split()) <= 25:
                        quotes.append(quote)
                        signals.append({**evidence(quote, fact), 'signal': kind})
    contacts = []
    # Contact values from news, recruiters and social pages are not imported.
    if employer and not job_page:
        contacts.append({'type': 'website', 'value': finding.url,
                         'evidence': evidence(identity, 'Официальная страница работодателя')})
        for tag, text in page.blocks:
            if tag != 'address' and not re.search(r'(?:телефон|контакты|электронная почта|e-mail)\s*:', text, re.I):
                continue
            if re.search(r'рекрутер|ваканси|отклик|личн', text, re.I):
                continue
            for kind, pattern in [('phone', _PHONE), ('email', _EMAIL)]:
                match = pattern.search(text)
                if not match or any(c['type'] == kind for c in contacts):
                    continue
                value = match.group()
                if sum(len(q.split()) for q in quotes) + len(value.split()) <= 25:
                    quotes.append(value)
                    contacts.append({'type': kind, 'value': value,
                                     'evidence': evidence(value, 'Публичный деловой контакт на официальной странице')})

    active = next((match.group() for text in blocks if (match := _ACTIVE.search(text))), None) if job_page else None
    if active and sum(len(q.split()) for q in quotes) + len(active.split()) <= 25:
        quotes.append(active)
    else:
        active = None
    title = next((text for tag, text in page.blocks if tag == 'h1'), None) or next((text for tag, text in page.blocks if tag == 'title'), None) or host
    sector = next((label for label, pattern in _SECTOR if re.search(pattern, f'{title} {signal_quote}', re.I)), 'Не установлена')
    source = {'id': 'primary', 'url': finding.url, 'title': title[:300], 'checked_at': checked_at,
              'published_on': published_on, 'valid_until': valid_until, 'status_evidence': active,
              'vacancy_status': 'active' if active else 'unknown' if job_page else 'not_applicable',
              'kind': 'vacancy' if job_page else 'company_site' if employer else 'public_page', 'access': 'public_page',
              'excerpt': '\n'.join(quotes), 'limitation': 'Доказательство извлечено из открытой страницы; прочтение не заменяет смысловую проверку.'}
    usage = ({'status': 'bitrix24', 'basis': 'explicit_usage', 'evidence': [evidence(bitrix, signal_fact)]} if bitrix else {})
    return Candidate.model_validate({'name': name, 'city': CITY, 'sector': sector, 'website': finding.url,
            'identity': evidence(identity, 'Наименование компании в первичном источнике'),
            'geography': evidence(city_quote, 'Локальная привязка к Шымкенту'),
            'sources': [source], 'evidence': signals,
            'contacts': contacts, 'crm_usage_evidence': usage, 'commercial_proposal': None,
            'products': ['Аудит или донастройка Битрикс24' if bitrix else 'Внедрение CRM или автоматизация продаж'],
            'hypothesis': ('Уточнить, есть ли задача развития действующей системы и какой процесс она затрагивает.' if bitrix else
                           'Уточнить текущий процесс, действующую CRM и связь подтверждённого события с систематизацией заявок.'),
            'limitations': [f'Направление поиска {finding.lane}; источник обнаружен через {finding.provider}.',
                            'Интерес к покупке, решение об обращении и предложение не сформированы.',
                            *(['Подтверждено использование Битрикс24; отдельного текущего сигнала потребности на странице не найдено.']
                              if bitrix and len(signals) == 1 else [])]})


async def verify_findings(city, findings, limit, *, fetcher=None, as_of=None, on_candidate=None):
    _bounds(city, limit)
    fetcher = fetcher or PublicFetcher()
    result = DiscoveryResult()
    seen = set()
    for raw in findings:
        finding = raw if isinstance(raw, DiscoveryFinding) else DiscoveryFinding.model_validate(raw)
        result.findings.append(finding)
        # Prevent accidental use of a previously known, prohibited board/source.
        if not _source_allowed(finding.url):
            result.skipped_sources.append(f'{finding.provider}: источник не разрешён для автоматического сбора.')
            continue
        try:
            response = await fetcher.fetch(finding.url, media=_HTML)
            candidate = _candidate(finding, response)
        except (PublicSourceError, ValueError, UnicodeError) as exc:
            reason = str(exc) if isinstance(exc, PublicSourceError) else 'Доказательство не прошло локальную валидацию'
            safe_url = finding.url.split('?', 1)[0]
            result.skipped_sources.append(f'{finding.provider}:{finding.lane} {safe_url}: {reason}')
            if isinstance(exc, PublicSourceError) and exc.retryable:
                result.verification_incomplete = True
                break
            continue
        key = re.sub(r'[^\w]', '', candidate.name.casefold())
        if key in seen:
            result.search_log.append(f'{candidate.name}: повторная находка, нужна сверка идентичности; в текущей выдаче сохранён первый источник.')
            continue
        seen.add(key)
        result.candidates.append(candidate)
        result.search_log.append(f'{finding.provider}:{finding.lane}: подтверждена открытая страница и локальная цитата для {candidate.name}.')
        if on_candidate is not None:
            pending = on_candidate(candidate)
            if inspect.isawaitable(pending):
                await pending
        if len(result.candidates) >= limit:
            break
    return result


async def discover(city, limit, *, lanes=LANES, adapters=None, fetcher=None, as_of=None, on_candidate=None):
    fetcher = fetcher or PublicFetcher()
    found = await collect_findings(city, limit, lanes=lanes, adapters=adapters, fetcher=fetcher, as_of=as_of)
    verified = await verify_findings(city, found.findings, limit, fetcher=fetcher, as_of=as_of, on_candidate=on_candidate)
    found.candidates = verified.candidates
    found.search_log.extend(verified.search_log)
    found.skipped_sources.extend(verified.skipped_sources)
    found.verification_incomplete = verified.verification_incomplete
    return found
