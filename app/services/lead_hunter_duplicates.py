"""Identity matching plus bounded CRM enrichment. No creation or outbound contact."""
from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
from urllib.parse import urlsplit

from app.integrations.lead_hunter_crm import CRMReadError
from app.models.lead_hunter_crm import CRMCheck, CRMMatch, CRMRecord

SHARED_DOMAINS = frozenset({'hh.kz', 'hh.ru', 'finder.work', '2gis.kz', '2gis.ru', 'yandex.kz',
                            'yandex.ru', 'yandex.com', 'instagram.com', 'facebook.com', 'wa.me',
                            't.me', 'olx.kz', 'gmail.com', 'mail.ru', 'bk.ru', 'doq-med.kz',
                            'doq.kz', 'qyzmet.kz', 'joblum.com', 'cataloxy-kz.ru', 'taplink.cc',
                            'tilda.ws', 'business.site', 'wixsite.com', 'google.com'})


def normalize_phone(value: str) -> str | None:
    if re.search(r'(?:доб\.?|ext\.?|#)', value, re.I):
        return None  # Do not silently merge PBX extensions or guess their owner.
    digits = re.sub(r'\D', '', value)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits if 10 <= len(digits) <= 15 and not digits.startswith('0') else None


def normalize_domain(value: str) -> str | None:
    if not value.strip():
        return None
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    try:
        host = (parsed.hostname or '').lower().rstrip('.').removeprefix('www.').encode('idna').decode('ascii')
    except (UnicodeError, ValueError):
        return None
    if '.' not in host or any(host == d or host.endswith('.' + d) for d in SHARED_DOMAINS):
        return None
    return host  # No unsafe suffix/substring match and no guessed eTLD+1.


def normalize_name(value: str) -> str:
    name = value.casefold().replace('ё', 'е')
    name = re.sub(r'\b(?:тоо|ип|ооо|llp|llc)\b', ' ', name)
    return ' '.join(re.findall(r'[\w]+', name))


def candidate_keys(candidate):
    from app.services.lead_hunter_scoring import usable_contact
    now = datetime.now(timezone.utc)
    contacts = [c for c in candidate.contacts if usable_contact(candidate, c, now)]
    phones = {p for c in contacts if c.type == 'phone'
              if (p := normalize_phone(c.value))}
    # Only identity-verified official site contacts qualify as domain evidence.
    domains = {d for c in contacts if c.type == 'website'
               if (d := normalize_domain(c.value))}
    names = {normalize_name(n) for n in [candidate.name, *candidate.aliases] if normalize_name(n)}
    return phones, domains, names


def match_records(candidate, records: list[CRMRecord]) -> list[CRMMatch]:
    phones, domains, names = candidate_keys(candidate)
    def identity(r):
        return ('company', r.company_id) if r.company_id else (r.entity, r.id)
    phone_owners, domain_owners = {}, {}
    for r in records:
        for p in {normalize_phone(p) for p in r.phones} - {None}:
            phone_owners.setdefault(p, set()).add(identity(r))
        for d in {normalize_domain(d) for d in r.websites} - {None}:
            domain_owners.setdefault(d, set()).add(identity(r))
    matches = []
    for r in records:
        matched_phones = phones & {normalize_phone(p) for p in r.phones}
        matched_domains = domains & {normalize_domain(d) for d in r.websites}
        record_names = {normalize_name(r.company_title)} if r.entity == 'contact' else {normalize_name(r.name), normalize_name(r.company_title)}
        exact = bool(names & (record_names - {''}))
        similar = any(SequenceMatcher(None, n, rn).ratio() >= .86 for n in names for rn in record_names if rn)
        unique_phone = any(len(phone_owners[p]) == 1 for p in matched_phones)
        unique_domain = any(len(domain_owners[d]) == 1 for d in matched_domains)
        # One exact public business identifier suffices only if not shared by other CRM identities.
        confirmed = unique_phone or unique_domain
        if not (matched_phones or matched_domains or exact or similar):
            continue
        reasons = []
        if matched_phones:
            reasons.append('Совпадает нормализованный публичный деловой телефон')
        if matched_domains:
            reasons.append('Совпадает домен официального сайта')
        if exact:
            reasons.append('Совпадает нормализованное название; само по себе не доказывает дубль')
        elif similar:
            reasons.append('Похожее название — только повод для ручной проверки')
        if not confirmed and (matched_phones or matched_domains):
            reasons.append('Идентификатор встречается у нескольких CRM-клиентов: возможный общий контакт/домен')
        matches.append(CRMMatch(entity=r.entity, id=r.id, name=r.name,
                                confidence='confirmed' if confirmed else 'possible', reasons=reasons,
                                responsible_id=r.assigned_by_id, status=r.status_id))
    return matches


def parse_record(entity: str, raw: dict) -> CRMRecord:
    def values(field):
        items = raw.get(field) or []
        if not isinstance(items, list):
            raise CRMReadError('CRM multifield has unexpected shape')
        return [str(x['VALUE']) for x in items if isinstance(x, dict) and x.get('VALUE')]
    def identifier(key):
        value = raw.get(key)
        return int(value) if value and str(value).isdigit() and int(value) > 0 else None
    record_id = identifier('ID')
    if not record_id:
        raise CRMReadError('CRM record lacks a valid ID')
    return CRMRecord(entity=entity, id=record_id,
                     name=str(raw.get('TITLE') or ' '.join(str(raw.get(k) or '') for k in ['LAST_NAME', 'NAME', 'SECOND_NAME'])).strip(),
                     company_id=record_id if entity == 'company' else identifier('COMPANY_ID'),
                     assigned_by_id=identifier('ASSIGNED_BY_ID'), status_id=raw.get('STATUS_ID') or raw.get('COMPANY_TYPE') or raw.get('TYPE_ID'),
                     phones=values('PHONE'), websites=values('WEB'), company_title=str(raw.get('COMPANY_TITLE') or ''))


class DuplicateService:
    def __init__(self, client):
        self.client = client
        self._users = {}
        self._statuses = None

    async def check(self, batch):
        records, coverage, scanned, limitations = [], {}, {}, []
        for entity, fields in {
            'company': ['ID', 'TITLE', 'ASSIGNED_BY_ID', 'COMPANY_TYPE', 'PHONE', 'WEB'],
            'contact': ['ID', 'NAME', 'LAST_NAME', 'SECOND_NAME', 'COMPANY_ID', 'ASSIGNED_BY_ID', 'TYPE_ID', 'PHONE', 'WEB'],
            'lead': ['ID', 'TITLE', 'COMPANY_TITLE', 'COMPANY_ID', 'ASSIGNED_BY_ID', 'STATUS_ID', 'PHONE', 'WEB'],
        }.items():
            coverage[entity] = False
            print(f'CRM READ ONLY: scanning {entity}', flush=True)
            seen = set()
            try:
                async for page in self.client.pages(f'crm.{entity}.list', {'select': fields, 'order': {'ID': 'ASC'}}):
                    for raw in page:
                        record = parse_record(entity, raw)
                        if record.id in seen:
                            raise CRMReadError('Repeated CRM record during scan; concurrent change or pagination error')
                        seen.add(record.id)
                        records.append(record)
                    if len(seen) and len(seen) % 2500 == 0:
                        print(f'CRM READ ONLY: {entity} records scanned: {len(seen)}', flush=True)
                coverage[entity] = True
            except CRMReadError as exc:
                limitations.append(f'{entity}: {exc}')
            scanned[entity] = len(seen)
            print(f'CRM READ ONLY: {entity} scanned={len(seen)}, complete={coverage[entity]}', flush=True)
        companies = {r.id: r.name for r in records if r.entity == 'company'}
        for r in records:
            if r.entity == 'contact' and r.company_id in companies:
                r.company_title = companies[r.company_id]
        # Resolve names/status/history for matches only; never export the full CRM index.
        for candidate in batch.candidates:
            matches = match_records(candidate, records)
            complete = all(coverage.values())
            state = ('confirmed_duplicate' if any(m.confidence == 'confirmed' for m in matches)
                     else 'incomplete' if not complete and records else 'unavailable' if not complete
                     else 'possible_match' if matches else 'no_match_accessible')
            check = CRMCheck(state=state, checked_at=datetime.now(timezone.utc), coverage=coverage,
                             scanned=scanned, matches=matches, limitations=list(limitations))
            check.limitations.append('Проверены только записи, видимые владельцу вебхука; скрытые правами записи неизвестны. Снимок может меняться во время чтения.')
            for match in matches:
                await self.enrich(match)
            candidate.crm_check = check
        return batch

    async def enrich(self, match: CRMMatch):
        if match.responsible_id:
            try:
                if match.responsible_id not in self._users:
                    result = await self.client.read('user.get', {'FILTER': {'ID': match.responsible_id}})
                    users = [r for r in result['result'] if str(r.get('ID')) == str(match.responsible_id)]
                    self._users[match.responsible_id] = (' '.join(str(users[0].get(k) or '') for k in ['NAME', 'LAST_NAME']).strip() if users else '')
                match.responsible_name = self._users[match.responsible_id] or None
            except CRMReadError as exc:
                match.limitations.append(f'Имя ответственного недоступно: {exc}')
        try:
            if self._statuses is None:
                self._statuses = {}
                async for page in self.client.pages('crm.status.list', {'order': {'SORT': 'ASC'}}):
                    for r in page:
                        self._statuses[(str(r.get('ENTITY_ID')), str(r.get('STATUS_ID')))] = str(r.get('NAME') or r.get('STATUS_ID'))
            status_group = {'lead': 'STATUS', 'company': 'COMPANY_TYPE', 'contact': 'CONTACT_TYPE'}.get(match.entity)
            if match.status:
                match.status = self._statuses.get((status_group, match.status), match.status)
        except CRMReadError as exc:
            self._statuses = None
            match.limitations.append(f'Справочник статусов недоступен: {exc}')
        owner_type = {'company': 4, 'contact': 3, 'lead': 1}[match.entity]
        try:
            # One binding per request includes secondary bindings. First page only, explicitly bounded.
            response = await self.client.read('crm.activity.list', {
                'filter': {'BINDINGS': [{'OWNER_TYPE_ID': owner_type, 'OWNER_ID': match.id}]},
                'order': {'ID': 'DESC'}, 'select': ['ID', 'TYPE_ID', 'SUBJECT', 'CREATED', 'END_TIME', 'COMPLETED', 'RESPONSIBLE_ID']})
            match.history = [{k: r.get(k) for k in ['ID', 'TYPE_ID', 'SUBJECT', 'CREATED', 'END_TIME', 'COMPLETED', 'RESPONSIBLE_ID']} for r in response['result'][:5]]
            match.limitations.append('История: последние 5 доступных дел по ID, без текстов переписок, записей звонков и комментариев таймлайна; это не полная история.')
            if not match.history:
                match.limitations.append('Доступных дел по прямым/вторичным привязкам не найдено; это не доказывает отсутствие общения.')
        except CRMReadError as exc:
            match.limitations.append(f'История взаимодействия недоступна: {exc}')
        try:
            key = {'company': 'COMPANY_ID', 'contact': 'CONTACT_ID', 'lead': 'LEAD_ID'}[match.entity]
            response = await self.client.read('crm.deal.list', {'filter': {key: match.id}, 'order': {'ID': 'DESC'},
                'select': ['ID', 'TITLE', 'STAGE_ID', 'CATEGORY_ID', 'ASSIGNED_BY_ID', 'DATE_MODIFY']})
            match.related_deals = [{k: r.get(k) for k in ['ID', 'TITLE', 'STAGE_ID', 'CATEGORY_ID', 'ASSIGNED_BY_ID', 'DATE_MODIFY']} for r in response['result'][:5]]
            match.limitations.append('Показаны до 5 последних связанных сделок; для контакта — по CONTACT_ID. Ответственный сделки указан ID, стадия — кодом.')
        except CRMReadError as exc:
            match.limitations.append(f'Связанные сделки недоступны: {exc}')
