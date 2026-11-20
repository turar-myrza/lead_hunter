"""Open web discovery for the scheduling worker, reusing Lead Hunter fetch rules."""
from datetime import date
import json
import re
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

from app.integrations.lead_hunter_public import PublicFetcher, PublicSourceError, public_https_url
from app.models.sales_team import Prospect, PublicSignal
from app.services.lead_hunter_discovery import (
    ALMATY, PublicIndexAdapter, RssAdapter, _A_SIGNALS, _B_FUTURE, _B_USE,
    _CLOSED, _EMAIL, _GDELT, _HIRING_SKILL, _LEGAL, _NEGATIVE, _PHONE, _SECTOR,
    _Page, _date_value, _publication, _sentences, _source_allowed, _types,
)
from app.services.lead_hunter_duplicates import normalize_domain, normalize_name, normalize_phone

CITIES = {
    'алматы': 'Алматы', 'астан': 'Астана', 'шымкент': 'Шымкент', 'караганда': 'Караганда',
    'караганд': 'Караганда', 'қарағанды': 'Караганда', 'актобе': 'Актобе', 'ақтөбе': 'Актобе',
    'атырау': 'Атырау', 'актау': 'Актау', 'ақтау': 'Актау', 'павлодар': 'Павлодар',
    'костан': 'Костанай', 'қостанай': 'Костанай', 'семей': 'Семей', 'тараз': 'Тараз',
    'туркестан': 'Туркестан', 'түркістан': 'Туркестан', 'кызылорд': 'Кызылорда',
    'қызылорда': 'Кызылорда', 'петропавловск': 'Петропавловск', 'кокшетау': 'Кокшетау',
    'көкшетау': 'Кокшетау', 'усть-каменогорск': 'Усть-Каменогорск', 'өскемен': 'Усть-Каменогорск',
    'уральск': 'Уральск', 'орал': 'Уральск', 'талдыкорган': 'Талдыкорган',
}
DEFAULT_FEEDS = {
    'shymkent': 'https://www.gov.kz/api/v1/public/rss/shymkent/news/ru',
    'astana': 'https://www.gov.kz/api/v1/public/rss/astana/news/ru',
    'almaty': 'https://www.gov.kz/api/v1/public/rss/almaty/news/ru',
    'karaganda': 'https://www.gov.kz/api/v1/public/rss/karaganda/news/ru',
    'atyrau': 'https://www.gov.kz/api/v1/public/rss/atyrau/news/ru',
    'turkistan': 'https://www.gov.kz/api/v1/public/rss/turkestan/news/kk',
}
QUERIES = {
    'A': '(Kazakhstan OR Almaty OR Astana OR Shymkent OR Karaganda) ("new branch" OR "new factory" OR "sales automation" OR "new warehouse")',
    'B': '(Kazakhstan OR Almaty OR Astana OR Shymkent OR Karaganda) (Bitrix24 OR "Bitrix 24")',
}
HTML = ('text/html', 'application/xhtml+xml')
_CONTACT = re.compile(r'\b(?:контакт\w*|contact\w*|байланыс\w*)\b', re.I)
_HR = re.compile(r'\b(?:ваканси\w*|рекрут\w*|кадр\w*|личн\w*|hr|career\w*|jobs?)\b', re.I)
_LOCATION = re.compile(r'^(?:г\.\s*)|\s+(?:қаласы|қ\.)$', re.I)
_NEW_DELIVERY = re.compile(r'\bперв(?:ых|ые)\s+(?:вагон\w*|парти\w*)\s+с\s+(?:металл\w*|продукци\w*)', re.I)
_OPENING = re.compile(r'(?:открылся|открылась|состоялось\s+открытие)\s+(?:нов\w*\s+)?(?:кампус\w*|филиал\w*|склад\w*)', re.I)
_EXCLUDED_PROFILE = re.compile(
    r'\b(?:алкоголь\w*|табак\w*|вейп\w*|казино|ломбард\w*|букмекер\w*|азартн\w*|'
    r'микрофинанс\w*|микрозайм\w*|процентн\w*\s+кредит\w*)\b', re.I,
)
_MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
           'сентября', 'октября', 'ноября', 'декабря')
_VISIBLE_DATE = re.compile(r'(\d{1,2})\s+(' + '|'.join(_MONTHS) + r'),?\s+(\d{4})(?:\s*г\.?)?', re.I)
_ORG_TYPES = {'Organization', 'Corporation', 'LocalBusiness', 'Store', 'MedicalBusiness', 'EducationalOrganization'}
_PROCESS = (
    (r'процесс\s+поступления|при[её]м\w*\s+комисси\w*',
     'Заявка на поступление', ('Приёмная комиссия получила запрос', 'Семья согласовала посещение школы', 'Назначен следующий контакт по заявлению'),
     'Приёмная комиссия видит согласованный визит и ответственного за следующий контакт.'),
    (r'(?:запросить|получить)\s+(?:коммерческое предложение|кп)|рассчитать\s+стоимость',
     'Запрос стоимости', ('Поступил запрос с параметрами', 'Ответственный уточнил объём и состав', 'Назначена дата подготовки предложения'),
     'В карточке запроса видны согласованные параметры и срок ответа.'),
    (r'(?:каталог\s+металлопроката|металлопрокат|арматур\w*)',
     'Запрос на металлопрокат', ('Заказчик указал вид и количество металла', 'Менеджер проверил наличие по складу', 'Назначен ответственный за ответ с условиями поставки'),
     'В карточке заказа видны позиция, склад и согласованный следующий контакт.'),
    (r'(?:записаться|запись\s+на\s+(?:при[её]м|консультацию|занят\w*))',
     'Запись на услугу', ('Поступил запрос на запись', 'Администратор согласовал время', 'Запись подтверждена в карточке'),
     'Команда видит ответственного и время согласованного визита.'),
    (r'(?:оформить|сделать|разместить)\s+заказ|(?:доставк\w*|отгрузк\w*)\s+(?:по\s+казахстану|со\s+склада)',
     'Запрос на поставку', ('Получены параметры товара и города', 'Менеджер подтвердил наличие и способ выдачи', 'Назначен следующий контакт по заказу'),
     'В карточке заказа видны согласованные условия и исполнитель.'),
)
_INTAKE = re.compile(
    r'(?:оставить|отправить|подать)\s+(?:заявку|запрос)|(?:заказать|перезвонить)\s+звонок|'
    r'(?:связаться|напишите)\s+с\s+нами|заказать\s+(?:товар|услугу)|онлайн[- ]заказ|'
    r'(?:запросить|получить)\s+(?:коммерческое предложение|кп)|записаться', re.I,
)


class _CompanyPage(_Page):
    # Official navigation often contains the only link to the contact page.
    _HIDDEN = _Page._HIDDEN - {'nav'}
    _TEXT = _Page._TEXT | {'button', 'h4', 'label', 'span', 'strong'}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'meta' and self._skip in ([], ['head']):
            key = attributes.get('property') or attributes.get('name')
            if key in {'description', 'og:description', 'og:site_name'} and attributes.get('content'):
                self.metadata.setdefault(key, []).append(attributes['content'][:1200])
        super().handle_starttag(tag, attrs)


def _host(url):
    return (urlsplit(url).hostname or '').removeprefix('www.')


def _site_link(href, page_url):
    if not isinstance(href, str):
        return None
    try:
        url = public_https_url(urljoin(page_url, href))
        return url if _source_allowed(url) and normalize_domain(url) else None
    except (PublicSourceError, ValueError):
        return None


def _identifies_company(value, company):
    name = normalize_name(company)
    return len(name) >= 3 and f' {name} ' in f' {normalize_name(value)} '


def _title_identifies(page, company):
    titles = [text for tag, text in page.blocks if tag in {'title', 'h1'}]
    titles.extend(page.metadata.get('og:site_name', ()))
    titles.extend(page.metadata.get('og:title', ()))
    titles.extend(node['name'] for node in page.structured()
                  if _types(node) & _ORG_TYPES and isinstance(node.get('name'), str))
    return any(_identifies_company(title, company) for title in titles)


def _cities(text):
    return {city for key, city in CITIES.items() if re.search(r'(?<!\w)' + key, text.casefold())}


def _publication_date(page, nodes, checked):
    published = _publication(page, nodes, checked)
    if published:
        return published
    if any(page.metadata.get(key) for key in ('article:published_time', 'datepublished')) or any(
        _types(node) & {'Article', 'NewsArticle', 'BlogPosting'} and node.get('datePublished') for node in nodes
    ):
        return None  # Invalid or conflicting machine-readable publication dates.
    dates = {}
    for index, (tag, visible) in enumerate(page.blocks[:120]):
        if match := _VISIBLE_DATE.fullmatch(visible.strip()):
            try:
                dates[index] = date(int(match[3]), _MONTHS.index(match[2].casefold()) + 1, int(match[1]))
            except ValueError:
                continue
        elif tag == 'time' and (value := _date_value(visible)):
            dates[index] = value
    event_headings = [i for i, (tag, text) in enumerate(page.blocks[:120]) if tag in {'h1', 'h2'}
                      and (_OPENING.search(text) or _NEW_DELIVERY.search(text))]
    if event_headings:
        near_heading = {value for index, value in dates.items() if abs(event_headings[0] - index) <= 5}
        if len(near_heading) == 1:
            return next(iter(near_heading))
    unique_dates = set(dates.values())
    return next(iter(unique_dates)) if len(unique_dates) == 1 else None


def article_company(url, response, entry=None, *, page=None):
    page = page or _CompanyPage(response.body)
    checked = response.checked_at.astimezone(ALMATY).date()
    nodes = list(page.structured())
    jobs = [node for node in nodes if 'JobPosting' in _types(node)]
    if len(jobs) > 1 or (jobs and not entry):
        return None
    job = jobs[0] if jobs else None
    if job:
        end_date = _date_value(job.get('validThrough'))
        organisation = job.get('hiringOrganization')
        if ((end_date and end_date < checked) or any(_CLOSED.search(value) for _, value in page.blocks)
                or not isinstance(organisation, dict) or not isinstance(organisation.get('name'), str)
                or not _identifies_company(organisation['name'], entry['company'])):
            return None
        locations = job.get('jobLocation')
        locations = locations if isinstance(locations, list) else [locations]
        places = [x.get('address') for x in locations if isinstance(x, dict)]
        if not any(isinstance(x, dict) and isinstance(x.get('addressLocality'), str)
                   and _LOCATION.sub('', x['addressLocality']).casefold() == entry['city'].casefold() for x in places):
            return None
    publication = _publication(page, nodes, checked, job=job) if job else _publication_date(page, nodes, checked)
    if publication is None or not 0 <= (checked - publication).days <= 60:
        return None
    if entry and not (job or _title_identifies(page, entry['company'])):
        return None
    headings = [value for tag, value in page.blocks if tag in {'title', 'h1', 'h2'}]
    context = [sentence for sentence in headings if entry and _identifies_company(sentence, entry['company'])]
    headline_cities = set().union(*map(_cities, context))
    signals = (*_A_SIGNALS, ('growth', _NEW_DELIVERY, 'Источник сообщает о первых поставках.'),
               ('growth', _OPENING, 'Источник сообщает об открытии площадки.'))
    for sentence in (*context, *_sentences(page.blocks)):
        names = list(_LEGAL.finditer(sentence))
        if len(names) > 1 or len(sentence) > 520 or _NEGATIVE.search(sentence):
            continue
        cities = _cities(sentence)
        if entry:
            name = entry['company']
            city = entry['city']
            if names and not _identifies_company(names[0].group(1), name):
                continue
            if not job and city not in cities and headline_cities != {city}:
                continue
        else:
            if len(names) != 1 or len(cities) != 1:
                continue
            name, city = names[0].group(1), next(iter(cities))
        bitrix = _B_USE.search(sentence) if not (_HIRING_SKILL.search(sentence) or _B_FUTURE.search(sentence)) else None
        signal = next(((kind, match.group()) for kind, pattern, _ in signals
                       if (match := pattern.search(sentence))), None)
        if not bitrix and not signal:
            continue
        kind, process = ('existing_crm', bitrix.group()) if bitrix else signal
        # Short paraphrase/phrases retain attribution without copying the full source.
        fact = f'{name}, {city}. В датированном сообщении компания упомянута в связи с событием «{process}».'
        hypothesis = (f'Уточнить, как команда ведёт следующий шаг по процессу «{process}», '
                      'и проверить наличие отдельной задачи автоматизации.')
        return Prospect(name=name, city=city, industry='Требуется уточнение профиля',
                        sources=[{'url':url, 'fact':fact, 'published':publication.isoformat(),
                                  'observed_at':checked, 'evidence_type':'employer_vacancy' if job else kind}],
                        product='B' if bitrix else 'A', fact=fact,
                        crm_status='Битрикс24 указан в источнике.' if bitrix else 'Не установлена',
                        hypothesis=hypothesis, offer=f'Диагностика обработки заявки вокруг события «{process}».',
                        process=process, next_step='Найти официальный сайт, основной вид деятельности и публичный рабочий контакт.',
                        ethical='Требуется проверка по официальной странице', contact_status='research_only')


def _employer_entry(raw):
    company = raw.get('employer') or raw.get('company')
    city = raw.get('city')
    if not isinstance(company, str) or not 3 <= len(company.strip()) <= 160 or city not in CITIES.values():
        return None
    url = _site_link(raw.get('url', ''), '')
    if url is None:
        return None
    site = _site_link(raw.get('site') or urlunsplit(('https', urlsplit(url).hostname, '/', '', '')), url)
    contact = _site_link(raw['contact_url'], url) if raw.get('contact_url') else None
    if not site or _host(site) != _host(url) or (raw.get('contact_url') and (not contact or _host(contact) != _host(url))):
        return None
    prefix = raw.get('path_prefix')
    if prefix is not None and (not isinstance(prefix, str) or not re.fullmatch(r'/(?:[\w-]+/)+', prefix)):
        return None
    return {'url':url, 'company':company.strip(), 'city':city, 'site':site, 'contact_url':contact, 'path_prefix':prefix}


def _official_site(article, article_url, company):
    links = {}
    for href, label in article.links:
        if (not _identifies_company(label, company) or len(label.split()) > 14
                or not (url := _site_link(href, article_url)) or _host(url) == _host(article_url)):
            continue
        parsed = urlsplit(url)
        if ((parsed.path.rstrip('/') not in {'', '/ru', '/kk', '/kz', '/en'} or parsed.query)
                and not re.search(r'официальн\w*\s+сайт|official\s+(?:web)?site|ресми\s+сайт', label, re.I)):
            continue
        links.setdefault(_host(url), url)
    return next(iter(links.values())) if len(links) == 1 else None


def _contact_link(page, site):
    for href, label in page.links:
        if not _CONTACT.search(label) or _HR.search(label):
            continue
        url = _site_link(href, site)
        if url and _host(url) == _host(site) and not _HR.search(urlsplit(url).path):
            return url
    return None


def _contacts(page):
    phones, emails = [], []
    fields = [value for tag, value in page.blocks if tag == 'address' or re.search(
        r'(?:телефон|контакты|электронная\s+почта|email|e-mail|байланыс)\s*:', value, re.I)]
    for href, label in page.links:
        scheme, _, value = href.partition(':')
        if scheme.casefold() in {'tel', 'mailto'} and not _HR.search(label):
            fields.append(unquote(value.split('?', 1)[0]))
    for field in fields:
        if _HR.search(field):
            continue
        for raw in _PHONE.findall(field):
            if number := normalize_phone(raw):
                if len(number) == 11 and number.startswith('7'):
                    phones.append('+' + number)
        for email in _EMAIL.findall(field):
            if not _HR.search(email.split('@', 1)[0]):
                emails.append(email.casefold())
    return list(dict.fromkeys(phones))[:4], list(dict.fromkeys(emails))[:4]


async def _enrich(prospect, article, article_url, entry, fetcher, stopped):
    site = entry['site'] if entry else _official_site(article, article_url, prospect.name)
    if not site or stopped():
        return prospect
    website = await fetcher.fetch(site, media=HTML)
    page = _CompanyPage(website.body)
    if not _title_identifies(page, prospect.name):
        return prospect
    website_date = website.checked_at.astimezone(ALMATY).date()
    profile = [v for k, v in page.blocks if k in {'title', 'h1', 'h2', 'p'} and len(v) <= 650]
    profile.extend(page.metadata.get('description', ()))
    profile.extend(page.metadata.get('og:description', ()))
    primary = ' '.join(profile[:35])
    industry = ('Оптовые поставки металлопроката' if re.search(r'металлопрокат|поставк\w*\s+металла', primary, re.I)
                else next((name for name, pattern in _SECTOR if re.search(pattern, primary, re.I)), None))
    updates = {'site':site}
    if industry:
        updates['industry'] = industry
        updates['ethical'] = ('ETHICS_REVIEW: на официальном сайте найден неоднозначный профиль.' if _EXCLUDED_PROFILE.search(primary)
                              else 'По публичному профилю признаки исключённых видов деятельности не обнаружены.')
    visible = ' '.join(value for _, value in page.blocks) + ' ' + ' '.join(label for _, label in page.links)
    eligible = next(((match.group(), title, steps, outcome) for pattern, title, steps, outcome in _PROCESS
                     if (match := re.search(pattern, visible, re.I))), None)
    displayed_intake = _INTAKE.search(visible)
    evidence = f'{prospect.name}. Официальный сайт подтверждает профиль «{industry or "требуется уточнение"}».'
    if eligible and displayed_intake:
        phrase, title, steps, outcome = eligible
        evidence += f' Опубликованы «{phrase}» и кнопка или приглашение «{displayed_intake.group()}».'
        service = 'проверку маршрута в действующем Битрикс24' if prospect.product == 'B' else 'разбор маршрута заявки в CRM'
        updates.update(process=title, visual_steps=list(steps), outcome=outcome,
                       hypothesis=f'Проверить, помогает ли единая карточка команде {prospect.name} согласовывать следующий шаг по процессу «{title}».',
                       offer=f'Для {prospect.name} покажем {service} для процесса «{title}» на одном учебном примере.',
                       next_step=f'Гайни уточнить владельца процесса «{title}» через опубликованный деловой контакт и предложить показ на 12 минут.')
    sources = prospect.sources.copy()
    if site != article_url:
        sources.append(PublicSignal(url=site, fact=evidence, observed_at=website_date, evidence_type='company_site'))
    phone, email = _contacts(page)
    contact_url, contact_date = site, website_date
    discovered_contact = (entry and entry['contact_url']) or _contact_link(page, site)
    if not (phone or email) and discovered_contact and discovered_contact != site and not stopped():
        try:
            contact_response = await fetcher.fetch(discovered_contact, media=HTML)
        except PublicSourceError:
            return prospect.model_copy(update={**updates, 'sources':sources,
                'next_step':'Подтвердить публичный рабочий контакт на официальном сайте перед квалификацией.'})
        contact_page = _CompanyPage(contact_response.body)
        headings = [value for key, value in contact_page.blocks if key in {'title', 'h1'}]
        if any(_CONTACT.search(value) for value in headings):
            phone, email = _contacts(contact_page)
            contact_url, contact_date = discovered_contact, contact_response.checked_at.astimezone(ALMATY).date()
    if phone or email:
        contacts = ', '.join(phone + email)
        sources.append(PublicSignal(url=contact_url, fact=f'В разделе деловых контактов {prospect.name} опубликованы {contacts}.',
                                    observed_at=contact_date, evidence_type='company_contact'))
        updates.update(phones=phone, emails=email, contact_status='public_business_contact')
    # Stages downstream still require evidence, ethical fit, live CRM verification and consent.
    if 'phones' not in updates:
        updates['next_step'] = 'Подтвердить публичный рабочий контакт на официальном сайте перед квалификацией.'
    return prospect.model_copy(update={**updates, 'sources':sources})


async def discover_national(*, stopped=lambda: False, fetcher=None, sources=(), limit=45):
    fetcher = fetcher or PublicFetcher(max_requests=75)
    links, events = {}, []
    sources = tuple(sources)
    for raw in sources:
        if raw.get('kind') != 'employer':
            continue
        if stopped():
            return [], events
        if not (entry := _employer_entry(raw)):
            events.append({'source':raw.get('name', 'employer'), 'status':'invalid_registry'})
            continue
        label = raw.get('name') or entry['company']
        if not entry['path_prefix']:
            links.setdefault(entry['url'], entry)
            events.append({'source':label, 'status':'available', 'results':1})
            continue
        results = set()
        adapter = PublicIndexAdapter(label, entry['url'], entry['path_prefix'])
        try:
            for lane in QUERIES:
                if stopped():
                    return [], events
                for finding in await adapter.search(entry['city'], lane, 7, fetcher):
                    links.setdefault(finding.url, entry)
                    results.add(finding.url)
            events.append({'source':label, 'status':'available', 'results':len(results)})
        except (PublicSourceError, ValueError):
            events.append({'source':label, 'status':'unavailable'})
    for lane, query in QUERIES.items():
        if stopped():
            return [], events
        url = _GDELT + '?' + urlencode({'query':query, 'mode':'artlist', 'format':'json',
                                        'timespan':'30d', 'maxrecords':18, 'sort':'datedesc'})
        try:
            response = await fetcher.fetch(url, media=('application/json',))
            items = json.loads(response.body)['articles']
            if not isinstance(items, list):
                raise ValueError('Bad article index')
            for item in items[:18]:
                if isinstance(item, dict) and isinstance(item.get('url'), str):
                    if _source_allowed(item['url']):
                        links.setdefault(item['url'], None)
            events.append({'source':f'GDELT {lane}', 'status':'available', 'results':len(items)})
        except (PublicSourceError, KeyError, TypeError, ValueError):
            events.append({'source':f'GDELT {lane}', 'status':'unavailable'})
    adapters = [RssAdapter(f'gov-{region}', url, ('www.gov.kz',)) for region, url in DEFAULT_FEEDS.items()]
    for entry in sources:
        if entry.get('kind') == 'employer':
            continue
        url = _site_link(entry.get('url', ''), '')
        if not url:
            continue
        if entry.get('path_prefix') and entry['path_prefix'] != '/':
            adapters.append(PublicIndexAdapter(entry['name'], url, entry['path_prefix']))
        else:
            adapters.append(RssAdapter(entry['name'], url, (urlsplit(url).hostname,)))
    for adapter in adapters:
        if stopped():
            return [], events
        try:
            findings = await adapter.search('Казахстан', 'A', 7, fetcher)
            for finding in findings:
                url = finding.url
                # Legacy and canonical paths belong to the same publisher.
                if url.startswith('https://www.gov.kz/memleket/entities/') and '/press/news/details/' in url:
                    url = url.replace('/memleket/entities/', '/org/', 1)
                links.setdefault(url, None)
            events.append({'source':adapter.name, 'status':'available', 'results':len(findings)})
        except (PublicSourceError, ValueError):
            events.append({'source':adapter.name, 'status':'unavailable'})
    prospects = {}
    for url, entry in list(links.items())[:limit]:
        if stopped():
            return [], events
        try:
            url = public_https_url(url)
            response = await fetcher.fetch(url, media=HTML)
            page = _CompanyPage(response.body)
            prospect = article_company(url, response, entry, page=page)
        except (PublicSourceError, ValueError, UnicodeError):
            continue
        if prospect is None:
            continue
        try:
            prospect = await _enrich(prospect, page, url, entry, fetcher, stopped)
        except (PublicSourceError, ValueError, UnicodeError):
            pass  # Retain the original dated evidence when first-party verification fails.
        identity = normalize_name(prospect.name), prospect.city
        old = prospects.get(identity)
        if old is None or (bool(prospect.phones or prospect.emails), bool(prospect.site)) > (
                bool(old.phones or old.emails), bool(old.site)):
            prospects[identity] = prospect
    result = list(prospects.values())
    events.append({'source':'verification', 'status':'finished', 'urls':len(links), 'companies':len(result),
                   'verified_sites':sum(bool(company.site) for company in result),
                   'verified_contacts':sum(bool(company.phones or company.emails) for company in result)})
    return result, events
