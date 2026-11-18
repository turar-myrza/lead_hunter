"""Relationship history must not confuse identity, sales outcome and delivery."""
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.lead_hunter_crm import CRMReadError, LeadHunterCRMClient, READ_METHODS
from app.models.lead_hunter_crm import CRMCheck, CRMMatch
from app.models.lead_hunter_relationships import CRMReference, RelationshipReview
from app.services.lead_hunter_relationships import RelationshipService, plain_text, reviewed_fact


class HistoryClient:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.company = {'ID': '1', 'TITLE': 'Company', 'ASSIGNED_BY_ID': '7', 'COMPANY_TYPE': 'CUSTOMER',
                        'PHONE': [{'VALUE': '+7 777 123 4567'}], 'WEB': [{'VALUE': 'https://company.kz'}],
                        'DATE_MODIFY': '2026-11-05T10:00:00+05:00'}
        self.lead = {'ID': '20', 'TITLE': 'Old prospect', 'COMPANY_ID': '1', 'STATUS_ID': 'LOSE',
                     'STATUS_SEMANTIC_ID': 'F', 'ASSIGNED_BY_ID': '7'}
        self.deals = [{'ID': '100', 'TITLE': 'CRM implementation', 'COMPANY_ID': '1',
                       'STAGE_ID': 'WON', 'CATEGORY_ID': '0', 'STAGE_SEMANTIC_ID': 'S',
                       'ASSIGNED_BY_ID': '7'},
                      {'ID': '101', 'TITLE': 'New automation', 'COMPANY_ID': '1',
                       'STAGE_ID': 'C17:LOSE', 'CATEGORY_ID': '17', 'STAGE_SEMANTIC_ID': 'F',
                       'ASSIGNED_BY_ID': '8', 'UF_CRM_REASON': '2'}]

    async def read(self, method, params):
        self.calls.append((method, params))
        assert method in READ_METHODS
        if method in self.fail:
            raise CRMReadError('CRM rejected read request: ACCESS_DENIED')
        if method.endswith('.fields'):
            return {'result': {'UF_CRM_REASON': {'title': 'Причина отказа', 'type': 'enumeration',
                                               'items': [{'ID': '2', 'VALUE': 'Выбрали другого партнера'}]},
                               'UF_CRM_FILE': {'title': 'Документы проекта', 'type': 'file'},
                               'UF_CRM_PRIVATE': {'title': 'Паспорт', 'type': 'string'}}}
        if method == 'crm.company.get':
            return {'result': self.company}
        if method == 'crm.contact.get':
            return {'result': {'ID': str(params['id']), 'NAME': 'Contact', 'COMPANY_ID': '1', 'ASSIGNED_BY_ID': '7'}}
        if method == 'crm.lead.get':
            return {'result': self.lead}
        if method == 'user.get':
            return {'result': [{'ID': str(params['FILTER']['ID']), 'NAME': 'Manager', 'LAST_NAME': str(params['FILTER']['ID'])}]}
        if method == 'crm.status.list':
            group = params['filter']['ENTITY_ID']
            return {'result': [
                {'ID': '1', 'STATUS_ID': 'CUSTOMER', 'NAME': 'Клиент'},
                {'ID': '2', 'STATUS_ID': 'WON', 'NAME': 'Успешно', 'SEMANTICS': 'S'},
                {'ID': '3', 'STATUS_ID': 'C17:LOSE' if group.endswith('_17') else 'LOSE',
                 'NAME': 'Выбрали другого партнера', 'EXTRA': {'SEMANTICS': 'failure'}}]}
        if method == 'crm.category.list':
            return {'result': {'categories': [{'id': 0, 'name': 'Sales'}, {'id': 17, 'name': 'Automation'}]}}
        if method == 'crm.company.contact.items.get':
            return {'result': [{'CONTACT_ID': '10'}]}
        if method.endswith('productrows.get'):
            return {'result': [{'ID': '1', 'PRODUCT_ID': '3', 'PRODUCT_NAME': 'Внедрение Битрикс24', 'QUANTITY': '1'}]}
        return {'result': []}

    async def pages(self, method, params):
        # Log through read as well to assert exact methods; data here follows page schema.
        await self.read(method, params)
        if method == 'crm.deal.list':
            yield self.deals
        elif method == 'crm.lead.list':
            yield [self.lead]
        elif method == 'crm.activity.list' and params['filter']['BINDINGS'][0]['OWNER_TYPE_ID'] == 4:
            yield [{'ID': '500', 'SUBJECT': 'Completed meeting', 'CREATED': '2026-10-01T09:00:00+05:00',
                    'START_TIME': '2026-10-01T10:00:00+05:00', 'END_TIME': '2026-10-01T10:30:00+05:00',
                    'COMPLETED': 'Y', 'DESCRIPTION': '<p>Обсудили развитие.</p>'},
                   {'ID': '501', 'SUBJECT': 'Planned meeting', 'CREATED': '2026-10-02T09:00:00+05:00',
                    'START_TIME': '2027-10-01T10:00:00+05:00', 'COMPLETED': 'N'}]
        elif method == 'crm.timeline.comment.list' and params['filter']['ENTITY_TYPE'] == 'deal' and params['filter']['ENTITY_ID'] == 100:
            yield [{'ID': '700', 'CREATED': '2026-10-03T10:00:00+05:00',
                    'COMMENT': 'Внедрение Битрикс24 выполнено, клиент принял работы.'}]
        else:
            yield []


def candidate(confidence='confirmed', state=None):
    return SimpleNamespace(crm_check=CRMCheck(
        state=state or ('confirmed_duplicate' if confidence == 'confirmed' else 'possible_match'),
        coverage={'company': True, 'contact': True, 'lead': True},
        matches=[CRMMatch(entity='company', id=1, name='Company', confidence=confidence,
                          reasons=['identifier' if confidence == 'confirmed' else 'similar name'])]))


@pytest.mark.asyncio
async def test_reads_full_scoped_history_but_never_infers_delivery_or_decline():
    client = HistoryClient()
    review = await RelationshipService(client).review(candidate())
    assert review.identity == 'confirmed'
    assert review.relationship_state == 'active_customer'
    assert review.status == 'partial'  # Semantic review is still mandatory.
    assert review.coverage['collection_complete']
    assert {e.id for e in review.entities if e.entity == 'deal'} == {100, 101}
    assert review.performed_services == [] and review.declined_offers == []
    assert review.owner_name == 'Manager 7'
    assert review.last_event_at == datetime.fromisoformat('2026-10-03T10:00:00+05:00')
    assert review.last_interaction_at is None  # Record dates do not prove a customer conversation.
    assert any(e.custom_fields.get('UF_CRM_REASON') == 'Причина отказа: Выбрали другого партнера' for e in review.entities)
    assert all('UF_CRM_FILE' not in e.custom_fields and 'UF_CRM_PRIVATE' not in e.custom_fields for e in review.entities)
    assert any(r.field == 'WEB' for e in review.entities for r in e.references)
    assert all(m in READ_METHODS for m, _ in client.calls)
    assert not any('file' in m or 'record' in m for m, _ in client.calls)


@pytest.mark.asyncio
async def test_similar_name_cannot_establish_customer_status():
    review = await RelationshipService(HistoryClient()).review(candidate('possible'))
    assert review.identity == 'possible'
    assert review.relationship_state == 'unknown'
    assert all(f.confidence == 'requires_review' for f in review.facts)
    assert review.status == 'partial'
    assert review.last_interaction_at is None


@pytest.mark.asyncio
async def test_denied_comments_make_history_partial_without_remote_dump():
    client = HistoryClient()
    client.fail.add('crm.timeline.comment.list')
    review = await RelationshipService(client).review(candidate())
    assert not review.coverage['collection_complete']
    assert review.status == 'partial'
    assert any('ACCESS_DENIED' in text for text in review.limitations)


@pytest.mark.asyncio
async def test_cutoff_is_explicit_not_full_history():
    review = await RelationshipService(HistoryClient(), max_deals=1).review(candidate())
    assert not review.coverage['collection_complete']
    assert any('предел 1' in s for s in review.limitations)


@pytest.mark.asyncio
async def test_missing_history_requires_complete_identity_scan():
    client = HistoryClient()
    c = SimpleNamespace(crm_check=CRMCheck(state='no_match_accessible',
        coverage={'company': True, 'contact': True, 'lead': True}))
    review = await RelationshipService(client).review(c)
    assert review.status == 'complete' and review.relationship_state == 'no_history'
    assert client.calls == []
    c.crm_check = CRMCheck(state='incomplete', coverage={'company': True})
    assert (await RelationshipService(client).review(c)).status == 'unavailable'


@pytest.mark.asyncio
async def test_analyst_fact_requires_recorded_exact_reference():
    review = await RelationshipService(HistoryClient()).review(candidate())
    reference = next(i.reference for e in review.entities for i in e.interactions if i.id == 700)
    fact = reviewed_fact(review, text='Внедрение принято клиентом.', reference=reference,
                         service_key='bitrix24_implementation')
    assert fact.confidence == 'explicit'
    forged = reference.model_copy(update={'quote': 'Client wants AI now'})
    with pytest.raises(ValueError, match='recorded'):
        reviewed_fact(review, text='New need', reference=forged)


@pytest.mark.asyncio
async def test_follows_lead_contact_company_chain_with_provenance_without_identity_guess():
    client = HistoryClient()
    client.lead = {'ID': '20', 'TITLE': 'Incoming call', 'CONTACT_ID': '10',
                   'PHONE': [{'VALUE': '+7 777 123 4567'}]}
    c = SimpleNamespace(crm_check=CRMCheck(state='possible_match',
        coverage={'company': True, 'contact': True, 'lead': True},
        matches=[CRMMatch(entity='lead', id=20, name='Incoming call', confidence='possible',
                          reasons=['Shared public phone'])]))
    review = await RelationshipService(client).review(c)
    contact = next(e for e in review.entities if e.entity == 'contact' and e.id == 10)
    company = next(e for e in review.entities if e.entity == 'company' and e.id == 1)
    assert any(r.entity == 'lead' and r.id == 20 and r.field == 'CONTACT_ID' and r.quote == '10'
               for r in contact.related_via)
    assert any(r.entity == 'contact' and r.id == 10 and r.field == 'COMPANY_ID' and r.quote == '1'
               for r in company.related_via)
    assert review.identity == 'possible' and review.relationship_state == 'unknown'


def test_reject_unconfirmed_customer_classification_and_strip_private_links():
    with pytest.raises(ValueError, match='Unconfirmed'):
        RelationshipReview(identity='possible', relationship_state='active_customer')
    text = plain_text('<script>secret</script><p>Useful</p> https://portal.kz/rest/1/SECRET/token')
    assert text == 'Useful [private link omitted]'


@pytest.mark.asyncio
@pytest.mark.parametrize('method,result', [
    ('crm.company.get', []), ('crm.company.get', {'ID': '2'}),
    ('crm.deal.fields', []), ('crm.deal.fields', {'UF': 'not an object'}),
    ('crm.category.list', []), ('crm.category.list', {'categories': {}}),
    ('crm.deal.productrows.get', {}), ('crm.timeline.comment.list', [True]),
])
async def test_read_list_and_object_shapes_are_not_interchangeable(method, result):
    async def respond(request):
        return httpx.Response(200, json={'result': result})
    async with LeadHunterCRMClient('https://portal.bitrix24.kz/rest/1/TEST/',
                                   transport=httpx.MockTransport(respond), pause=0) as client:
        with pytest.raises(CRMReadError):
            await client.read(method, {'id': 1})


@pytest.mark.asyncio
async def test_write_batch_redirects_and_empty_error_are_blocked_before_parse():
    requests = []
    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'error': '', 'error_description': 'SECRET invalid credential'})
    async with LeadHunterCRMClient('https://portal.bitrix24.kz/rest/1/TEST/',
                                   transport=httpx.MockTransport(respond), pause=0) as client:
        for method in ['batch', 'crm.company.update', 'crm.timeline.comment.add',
                       'crm.activity.add', 'telephony.externalcall.register', 'crm.quote.add']:
            with pytest.raises(CRMReadError, match='allowlist'):
                await client.read(method, {})
        assert requests == []
        with pytest.raises(CRMReadError) as error:
            await client.read('crm.company.get', {'id': 1})
        assert 'SECRET' not in str(error.value)
    async def redirect(request):
        return httpx.Response(302, headers={'location': 'https://other.invalid'})
    async with LeadHunterCRMClient('https://portal.bitrix24.kz/rest/1/TEST/',
                                   transport=httpx.MockTransport(redirect), pause=0) as client:
        with pytest.raises(CRMReadError, match='redirects'):
            await client.read('crm.company.get', {'id': 1})
