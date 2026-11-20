"""National prospect identity and history check through the existing read-only client."""
from collections import defaultdict
from datetime import datetime, timezone
import re
from types import SimpleNamespace

from app.core.config import get_settings
from app.core.exceptions import Bitrix24ConfigurationError
from app.integrations.lead_hunter_crm import CRMReadError, LeadHunterCRMClient
from app.models.lead_hunter_crm import CRMCheck, CRMMatch
from app.services.lead_hunter_duplicates import normalize_domain, normalize_name, normalize_phone
from app.services.lead_hunter_relationships import RelationshipService

FIELDS = {
    'company': 'ID TITLE COMPANY_TYPE PHONE EMAIL WEB ASSIGNED_BY_ID',
    'contact': 'ID NAME LAST_NAME COMPANY_ID TYPE_ID PHONE EMAIL WEB ASSIGNED_BY_ID',
    'lead': 'ID TITLE COMPANY_TITLE COMPANY_ID STATUS_ID PHONE EMAIL WEB ASSIGNED_BY_ID',
    'deal': 'ID TITLE COMPANY_ID CONTACT_ID LEAD_ID STAGE_ID ASSIGNED_BY_ID',
}
_SHARED = {'mail.kz', 'icloud.com', 'outlook.com', 'hotmail.com', 'list.ru', 'inbox.ru', 'bitrix24.kz', 'bitrix24.ru', 'gov.kz', 'goszakup.gov.kz', 'tilda.kz', 'taplink.ws', 'satu.kz'}
_TITLE_PHONE = re.compile(r'(?<!\d)(?:\+?7|8)[\s()-]*\d(?:[\s()-]*\d){9}(?!\d)')


def identity_name(name):
    normalized = normalize_name(name)
    return ''.join(part for part in normalized.split() if part not in {'жшс', 'ао', 'ақ', 'ltd', 'llp'})


def company_domain(value):
    value = normalize_domain(value)
    return value if value and not any(value == d or value.endswith('.' + d) for d in _SHARED) else None


def identity_keys(company):
    pairs = [('name', identity_name(n)) for n in (company.name, *company.aliases)]
    pairs += [('domain', company_domain(company.site or ''))]
    pairs += [('phone', normalize_phone(p)) for p in company.phones]
    pairs += [('email', e.casefold()) for e in company.emails]
    pairs += [('domain', company_domain(e.split('@')[-1])) for e in company.emails]
    return tuple(sorted({f'{k}:{v}' for k, v in pairs if v and (k != 'name' or len(v) >= 4)}))


async def read_index(client, *, stopped=lambda: False):
    records, coverage = {}, {}
    for entity, fields in FIELDS.items():
        seen = set()
        coverage[entity] = False
        try:
            async for page in client.pages(f'crm.{entity}.list', {'select': fields.split(), 'order': {'ID': 'ASC'}}):
                if stopped():
                    return records, coverage
                for raw in page:
                    ident = str(raw.get('ID', ''))
                    if not ident.isdigit() or int(ident) < 1 or ident in seen:
                        raise CRMReadError('Неоднозначная страница CRM')
                    seen.add(ident)
                    records[(entity, ident)] = raw
            coverage[entity] = True
        except CRMReadError:
            continue
    return records, coverage


def _index(records):
    keys = defaultdict(set)
    for pair, raw in records.items():
        entity, _ = pair
        names = [raw.get('TITLE'), raw.get('COMPANY_TITLE')]
        if entity == 'contact':
            names.append(' '.join(str(raw.get(field) or '') for field in ('NAME', 'LAST_NAME')))
        for name in filter(None, names):
            normal = identity_name(name)
            if len(normal) >= 4:
                keys[f'name:{normal}'].add(pair)
            for part in _TITLE_PHONE.findall(name):
                if normalized := normalize_phone(part):
                    keys[f'phone:{normalized}'].add(pair)
        for field in ('PHONE', 'EMAIL', 'WEB'):
            for item in raw.get(field) or []:
                value = str(item.get('VALUE') or '')
                if field == 'PHONE' and (phone := normalize_phone(value)):
                    keys[f'phone:{phone}'].add(pair)
                elif field == 'EMAIL' and '@' in value:
                    keys[f'email:{value.casefold()}'].add(pair)
                    if domain := company_domain(value.rsplit('@', 1)[-1]):
                        keys[f'domain:{domain}'].add(pair)
                elif field == 'WEB' and (domain := company_domain(value)):
                    keys[f'domain:{domain}'].add(pair)
    return keys


def match_company(company, records, keys, coverage):
    hits = defaultdict(set)
    for key in identity_keys(company):
        for pair in keys.get(key, ()):
            hits[pair].add(key.split(':')[0])
    # Deal/lead titles often include the company name along with the request.
    labels = {normalize_name(n) for n in (company.name, *company.aliases) if len(identity_name(n)) >= 6}
    patterns = [re.compile(r'(?<!\w)' + re.escape(n) + r'(?!\w)') for n in labels]
    if patterns:
        for pair, raw in records.items():
            if pair[0] not in {'deal', 'lead'}:
                continue
            title = normalize_name(raw.get('TITLE') or '')
            if any(p.search(title) for p in patterns):
                hits[pair].add('name')
    labels = {'name': 'Название, требуется сверка', 'domain': 'Деловой домен',
              'phone': 'Нормализованный телефон', 'email': 'Деловой email'}
    matches = []
    for (entity, ident), proof in sorted(hits.items()):
        raw = records[entity, ident]
        owner = str(raw.get('ASSIGNED_BY_ID') or '')
        strong = 'email' in proof or 'phone' in proof or {'domain', 'name'} <= proof
        matches.append(CRMMatch(entity=entity, id=int(ident), name=str(raw.get('TITLE') or company.name),
                                confidence='confirmed' if strong else 'possible',
                                reasons=[labels[k] for k in sorted(proof)],
                                status=raw.get('STAGE_ID') or raw.get('STATUS_ID') or raw.get('COMPANY_TYPE'),
                                responsible_id=int(owner) if owner.isdigit() and int(owner) > 0 else None))
    if not coverage or not all(coverage.get(entity, False) for entity in FIELDS):
        state = 'incomplete' if coverage else 'unavailable'
    elif any(m.confidence == 'confirmed' for m in matches):
        state = 'confirmed_duplicate'
    else:
        state = 'possible_match' if matches else 'no_match_accessible'
    return CRMCheck(state=state, checked_at=datetime.now(timezone.utc), coverage=coverage,
                    matches=matches, limitations=['Проверены только записи, видимые вебхуку.'])


async def check_sales_crm(companies, *, stopped=lambda: False):
    if not companies:
        return {}, {}
    settings = get_settings()
    try:
        async with LeadHunterCRMClient(settings.require_webhook_url(), timeout=max(settings.bitrix24_timeout_seconds, 20)) as crm:
            records, coverage = await read_index(crm, stopped=stopped)
            return await check_index(companies, records, coverage, crm, stopped=stopped)
    except (Bitrix24ConfigurationError, CRMReadError):
        return {c.name: {'state': 'unavailable'} for c in companies}, {}


async def check_index(companies, records, coverage, crm, *, stopped=lambda: False):
    keys = _index(records)
    result = {}
    service = RelationshipService(crm, max_entities=10, max_interactions=15)
    for company in companies:
        if stopped():
            return {}, {}
        check = match_company(company, records, keys, coverage)
        review = None
        if check.matches and check.state in {'possible_match', 'confirmed_duplicate'}:
            # Deal titles with no binding stay HOLD even when deeper history cannot be retrieved.
            review = await service.review(SimpleNamespace(crm_check=check.model_copy(update={'matches': check.matches[:10]})))
        result[company.name] = {
            'state': check.state, 'checked_at': check.checked_at.isoformat(), 'coverage': coverage,
            'matches': [{'entity': m.entity, 'id': m.id, 'reasons': m.reasons, 'status': m.status} for m in check.matches],
            'relationship': review.relationship_state if review else 'unknown',
            'history_status': review.status if review else 'not_applicable',
            'owner': review.owner_name if review else None,
            'last_interaction': review.last_interaction_at.isoformat() if review and review.last_interaction_at else None,
        }
    return result, {'methods': crm.method_counts, 'coverage': coverage,
                    'scanned': {entity: sum(pair[0] == entity for pair in records) for entity in FIELDS}}
