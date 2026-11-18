"""Offline acceptance tests: commercial value, ageing, contact integrity, CRM identity and READ ONLY."""
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

import httpx
from pydantic import ValidationError
import pytest

from app.integrations.lead_hunter_crm import LeadHunterCRMClient, CRMReadError, READ_METHODS
from app.integrations.bitrix24 import READ_ONLY_METHODS as ANALYST_METHODS
from app.models.lead_hunter import Candidate, CommercialSignal, ResearchBatch
from app.models.lead_hunter_crm import CRMCheck, CRMRecord
from app.services.lead_hunter_duplicates import DuplicateService, match_records, normalize_domain, normalize_phone
from app.services.lead_hunter_scoring import assess, freshness
from app.services.lead_hunter_reporting import csv_v11, report_v11

NOW = datetime.now(timezone.utc)


@pytest.fixture
def prospect():
    return Candidate.model_validate(dict(name='Example Business', city='Шымкент', sector='Опт', website='https://business.kz/',
        identity=dict(source_id='site', quote='Example Business', fact='Имя'),
        geography=dict(source_id='site', quote='Шымкент', fact='Город'),
        sources=[dict(id='site', url='https://business.kz/', title='Official site', kind='company_site', access='public_page',
                      checked_at=NOW.isoformat(), published_on=NOW.date().isoformat(), vacancy_status='not_applicable',
                      excerpt='Example Business Шымкент CRM +7 (700) 123-45-67 info@business.kz Site sales head Person Vacancy active')],
        evidence=[dict(source_id='site', quote='CRM', fact='Uses CRM', signal='existing_crm')],
        contacts=[dict(type='phone', value='+7 (700) 123-45-67', evidence=dict(source_id='site', quote='+7 (700) 123-45-67', fact='Published business number')),
                  dict(type='website', value='https://business.kz/', evidence=dict(source_id='site', quote='Example Business', fact='Official site'))],
        products=['Аудит CRM'], hypothesis='Как распределяются обращения?', next_step='Уточнить распределение заявок.'))


def crm_record(**changes):
    return CRMRecord(**dict(dict(entity='company',id=1,name='ТОО Example Business',phones=['87001234567'], websites=['http://www.business.kz/contact']),**changes))


def complete_check(**changes):
    return CRMCheck(**dict(dict(state='no_match_accessible', checked_at=NOW,
                               coverage={'company':True,'contact':True,'lead':True}),**changes))


def test_general_signals_do_not_make_a_ready_buyer(prospect):
    signals = ['growth','inbound','team','channels','existing_crm','order_complexity']
    prospect.evidence = [prospect.evidence[0].model_copy(update={'signal': CommercialSignal(s)}) for s in signals] * 3
    result = assess(prospect, NOW)
    assert result['score'] == 45
    assert result['priority'] == 'B'
    assert result['purchase_interest'] == 'unconfirmed'
    assert not result['import_ready']
    assert result['contact_score'] == 45


def test_explicit_project_has_higher_commercial_weight(prospect):
    prospect.evidence = [prospect.evidence[0].model_copy(update={'signal': s}) for s in CommercialSignal]
    result = assess(prospect, NOW)
    assert result['score'] == 100
    assert result['priority'] == 'A'
    assert result['purchase_interest'] == 'unconfirmed'  # Need does not imply intent.


@pytest.mark.parametrize('age,status,expected', [(0,'active',1),(30,'active',1),(31,'active',.5),(90,'active',.5),(91,'active',0),
                                               (5,'unknown',.5),(31,'unknown',.25),(0,'archived',0),(0,'closed',0)])
def test_vacancy_freshness_boundaries(prospect, age, status, expected):
    source = prospect.sources[0].model_copy(update={'kind':'vacancy','vacancy_status':status,'published_on':NOW.date()-timedelta(days=age)})
    assert float(freshness(source,NOW)[0]) == expected


def test_unknown_date_or_failed_or_stale_verification_cannot_score(prospect):
    source = prospect.sources[0]
    assert freshness(source.model_copy(update={'kind':'vacancy','published_on':None}), NOW)[0] == 0
    assert freshness(source.model_copy(update={'last_attempt_status':'unavailable'}), NOW)[0] == 0
    assert freshness(source.model_copy(update={'checked_at':NOW-timedelta(days=31)}), NOW)[0] == 0
    assert freshness(source.model_copy(update={'checked_at':NOW+timedelta(days=1)}), NOW)[0] == 0


def test_unavailable_contacts_do_not_raise_reachability(prospect):
    prospect.sources[0].last_attempt_status = 'unavailable'
    assert assess(prospect,NOW)['contact_score'] == 0
    assert not match_records(prospect,[crm_record(name='Unrelated')])


def test_network_crm_discount_and_undated_growth(prospect):
    prospect.evidence[0].scope = 'network'
    assert assess(prospect,NOW)['score'] == 3  # 5 * .5, half-up
    prospect.sources[0].published_on = None
    prospect.evidence[0].signal = CommercialSignal.GROWTH
    assert assess(prospect,NOW)['score'] == 0


def test_buying_interest_requires_separate_current_evidence(prospect):
    assert assess(prospect,NOW)['purchase_interest'] == 'unconfirmed'
    prospect.purchase_evidence = [prospect.evidence[0]]
    assert assess(prospect,NOW)['purchase_interest'] == 'public_request'
    prospect.sources[0].checked_at = NOW-timedelta(days=31)
    assert assess(prospect,NOW)['purchase_interest'] == 'unconfirmed'


def test_contact_roles_and_email_are_independent_of_need(prospect):
    baseline = assess(prospect,NOW)['score']
    raw = prospect.model_dump(mode='json')
    raw['contacts'] += [dict(type='email',value='info@business.kz',role='sales',person_name='Person',
        evidence=dict(source_id='site',quote='info@business.kz',fact='Email'),
        role_evidence=dict(source_id='site',quote='sales head Person',fact='Published sales role'))]
    q = assess(Candidate.model_validate(raw),NOW)
    assert q['score'] == baseline
    assert q['contact_score'] == 100
    assert q['contact_level'] == 'responsible_person'
    raw['contacts'][-1].pop('role_evidence')
    with pytest.raises(ValidationError):
        Candidate.model_validate(raw)


@pytest.mark.parametrize('phone,expected',[('+7 (700) 123-45-67','77001234567'),('8 700 123 45 67','77001234567'),
                                          ('7001234567','7001234567'),('123',None),('87001234567 доб. 12',None)])
def test_phone_normalization_without_guessing(phone,expected):
    assert normalize_phone(phone) == expected


@pytest.mark.parametrize('url,expected',[('HTTP://WWW.BUSINESS.KZ/path','business.kz'),('https://business.kz.evil.com','business.kz.evil.com'),
                                        ('https://kz.finder.work/vacancy/123',None),('instagram.com/example',None),('https://www.2gis.kz/firm/1',None)])
def test_shared_domains_cannot_prove_identity(url,expected):
    assert normalize_domain(url) == expected


def test_exact_phone_or_official_domain_matches(prospect):
    matches = match_records(prospect,[crm_record(name='Other legal name')])
    assert matches[0].confidence == 'confirmed'
    prospect.contacts = [c for c in prospect.contacts if c.type != 'phone']
    assert match_records(prospect,[crm_record(phones=[])])[0].confidence == 'confirmed'


def test_name_only_is_never_confirmed(prospect):
    for name in ['ТОО Example Business','Example Busines']:
        assert match_records(prospect,[crm_record(name=name,phones=[],websites=[])])[0].confidence == 'possible'
    assert not match_records(prospect,[crm_record(name='Completely different',phones=[],websites=[])])


def test_shared_phone_with_other_companies_stays_possible(prospect):
    prospect.contacts = [c for c in prospect.contacts if c.type != 'website']
    matches=match_records(prospect,[crm_record(websites=[]),crm_record(id=2,name='Other',websites=[])])
    assert len(matches)==2 and all(m.confidence=='possible' for m in matches)


def test_contact_belongs_to_same_company_not_a_different_phone_owner(prospect):
    company=crm_record(company_id=1)
    contact=crm_record(entity='contact',id=2,company_id=1,name='Employee',company_title='Example Business')
    assert all(m.confidence=='confirmed' for m in match_records(prospect,[company,contact]))


def test_crm_permission_failure_never_becomes_no_duplicates(prospect):
    prospect.crm_check = complete_check(state='unavailable',coverage={})
    assert not assess(prospect,NOW)['import_ready']
    prospect.crm_check = complete_check(state='incomplete',coverage={'company':True,'contact':False,'lead':True})
    assert not assess(prospect,NOW)['import_ready']
    prospect.crm_check = complete_check()
    assert assess(prospect,NOW)['import_ready']
    prospect.crm_check.checked_at = NOW-timedelta(days=2)
    assert not assess(prospect,NOW)['import_ready']
    prospect.crm_check = complete_check(state='confirmed_duplicate')
    assert not assess(prospect,NOW)['import_ready']
    assert 'ответственному' in assess(prospect,NOW)['next_step']


def test_import_csv_excludes_unchecked_and_confirmed_duplicates(prospect):
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['query'])
    assert len(list(csv.DictReader(StringIO(csv_v11(batch)),delimiter=';')))==1
    assert not list(csv.DictReader(StringIO(csv_v11(batch,import_only=True)),delimiter=';'))
    prospect.crm_check=complete_check()
    assert len(list(csv.DictReader(StringIO(csv_v11(batch,import_only=True)),delimiter=';')))==1
    prospect.crm_check.state='confirmed_duplicate'
    assert not list(csv.DictReader(StringIO(csv_v11(batch,import_only=True)),delimiter=';'))
    assert 'Следующий шаг менеджера' in report_v11(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize('method',['crm.lead.add','crm.company.update','crm.contact.delete','crm.deal.add','batch','im.message.add','telephony.externalcall.register','crm.activity.add','crm.status.list/../crm.lead.add'])
async def test_write_methods_blocked_before_network(method):
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'result':[]})
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0) as client:
        with pytest.raises(CRMReadError,match='blocked'):
            await client.read(method,{})
    assert not calls
    assert 'crm.company.list' not in ANALYST_METHODS  # Analyst safety boundary unchanged.


@pytest.mark.asyncio
async def test_pagination_and_only_allowlisted_requests(prospect):
    seen=[]
    async def handler(request):
        method=request.url.path.split('/')[-1].removesuffix('.json')
        body=json.loads(request.content)
        seen.append((method,body))
        if method=='crm.company.list':
            if body['start']==0:
                return httpx.Response(200,json={'result':[{'ID':'1','TITLE':'Other','PHONE':[{'VALUE':'87001234567'}],'ASSIGNED_BY_ID':'7','COMPANY_TYPE':'CUSTOMER'}],'next':50})
            return httpx.Response(200,json={'result':[{'ID':'2','TITLE':'Another'}]})
        if method=='user.get':
            return httpx.Response(200,json={'result':[{'ID':'7','NAME':'Manager','LAST_NAME':'One'}]})
        if method=='crm.status.list':
            return httpx.Response(200,json={'result':[{'ENTITY_ID':'COMPANY_TYPE','STATUS_ID':'CUSTOMER','NAME':'Клиент'}]})
        if method=='crm.activity.list':
            assert body['filter']['BINDINGS']==[{'OWNER_TYPE_ID':4,'OWNER_ID':1}]
            return httpx.Response(200,json={'result':[{'ID':'20','SUBJECT':'Встреча','COMPLETED':'Y'}]})
        return httpx.Response(200,json={'result':[]})
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['query'])
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0) as client:
        await DuplicateService(client).check(batch)
    check=batch.candidates[0].crm_check
    assert check.state=='confirmed_duplicate'
    assert check.scanned['company']==2
    assert check.matches[0].responsible_name=='Manager One'
    assert check.matches[0].status=='Клиент'
    assert check.matches[0].history[0]['SUBJECT']=='Встреча'
    assert all(method in READ_METHODS for method,_ in seen)


@pytest.mark.asyncio
async def test_access_denied_partial_result_stays_incomplete(prospect):
    async def handler(request):
        if 'crm.contact.list' in request.url.path:
            return httpx.Response(403,json={'error':'ACCESS_DENIED','error_description':'secret should not leak'})
        if 'crm.company.list' in request.url.path:
            return httpx.Response(200,json={'result':[{'ID':'8','TITLE':'Unrelated'}]})
        return httpx.Response(200,json={'result':[]})
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['query'])
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0) as client:
        await DuplicateService(client).check(batch)
    assert prospect.crm_check.state=='incomplete'
    assert 'secret' not in prospect.crm_check.model_dump_json()
    assert not assess(prospect)['import_ready']


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[{'result':[],'next':50},{'result':[{'ID':'1'}],'next':0},{'result':{},'next':50}])
async def test_bad_pagination_fails_closed(payload):
    async def handler(request):
        return httpx.Response(200,json=payload)
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0,max_pages=2) as client:
        with pytest.raises(CRMReadError):
            async for _ in client.pages('crm.company.list',{}):
                pass


@pytest.mark.asyncio
async def test_redirect_not_followed_and_secrets_not_exposed():
    seen=[]
    async def handler(request):
        seen.append(request)
        return httpx.Response(302,headers={'Location':'https://evil.test/'})
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0) as client:
        with pytest.raises(CRMReadError) as exc:
            await client.read('crm.company.list',{})
    assert len(seen)==1 and 'secret' not in str(exc.value)


def test_cli_private_export_and_no_network_for_unchecked(prospect,tmp_path):
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['query'])
    path=tmp_path/'research.json'
    path.write_text(batch.model_dump_json())
    result=subprocess.run([sys.executable,'-m','app.lead_hunter','export',str(path),'--output',str(tmp_path/'out')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'out/review.csv').stat().st_mode & 0o777 == 0o600
    assert (tmp_path/'out/companies.csv').read_bytes().startswith(b'\xef\xbb\xbf')
    assert len(list(csv.DictReader((tmp_path/'out/review.csv').open(encoding='utf-8-sig'),delimiter=';')))==1
    assert not list(csv.DictReader((tmp_path/'out/companies.csv').open(encoding='utf-8-sig'),delimiter=';'))


def test_shared_phone_and_domain_not_automatically_confirmed(prospect):
    matches = match_records(prospect, [crm_record(), crm_record(id=2,name='Other business')])
    assert len(matches) == 2
    assert all(m.confidence == 'possible' for m in matches)


def test_vacancy_status_and_named_person_require_verbatim_provenance(prospect):
    raw=prospect.model_dump(mode='json')
    raw['sources'][0]['vacancy_status']='archived'
    with pytest.raises(ValidationError,match='status requires'):
        Candidate.model_validate(raw)
    raw=prospect.model_dump(mode='json')
    raw['public_responsibles']=[dict(name='Person',role='sales head',evidence=dict(source_id='site',quote='sales head Person',fact='Published role'))]
    candidate=Candidate.model_validate(raw)
    assert assess(candidate,NOW)['contact_score']==45  # Name/role alone is not a direct contact.
    raw['public_responsibles'][0]['name']='Invented'
    with pytest.raises(ValidationError):
        Candidate.model_validate(raw)


@pytest.mark.asyncio
async def test_two_pages_with_repeated_ids_leave_incomplete_state(prospect):
    async def handler(request):
        method=request.url.path.split('/')[-1].removesuffix('.json')
        if method=='crm.company.list':
            return httpx.Response(200,json={'result':[{'ID':'10','TITLE':'Unrelated'}],'next':50 if json.loads(request.content)['start']==0 else 100})
        return httpx.Response(200,json={'result':[]})
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['query'])
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0) as client:
        await DuplicateService(client).check(batch)
    assert prospect.crm_check.state=='incomplete'
    assert not prospect.crm_check.coverage['company']
    assert not assess(prospect,NOW)['import_ready']


@pytest.mark.asyncio
async def test_history_denied_preserves_duplicate_without_inventing_history(prospect):
    async def handler(request):
        if 'crm.company.list' in request.url.path:
            return httpx.Response(200,json={'result':[{'ID':'1','TITLE':'Existing','PHONE':[{'VALUE':'87001234567'}]}]})
        if 'crm.activity.list' in request.url.path:
            return httpx.Response(403,json={'error':'ACCESS_DENIED'})
        return httpx.Response(200,json={'result':[]})
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['query'])
    async with LeadHunterCRMClient('https://crm.test/rest/1/secret/',transport=httpx.MockTransport(handler),pause=0) as client:
        await DuplicateService(client).check(batch)
    assert prospect.crm_check.state=='confirmed_duplicate'
    match=prospect.crm_check.matches[0]
    assert match.history==[]
    assert any('недоступна' in x for x in match.limitations)


@pytest.mark.parametrize('signals,score,priority',[
    (['growth','inbound','team'],30,'C'),
    (['growth','inbound','team','channels'],35,'B'),
    (['processing_problem','automation_project','team'],60,'B'),
    (['processing_problem','automation_project','inbound'],65,'A'),
])
def test_v11_priority_boundaries(prospect,signals,score,priority):
    prospect.evidence=[prospect.evidence[0].model_copy(update={'signal':CommercialSignal(s)}) for s in signals]
    q=assess(prospect,NOW)
    assert q['score']==score and q['priority']==priority


def test_subdomain_is_not_silently_assumed_same_business(prospect):
    prospect.contacts=[c for c in prospect.contacts if c.type=='website']
    assert not match_records(prospect,[crm_record(name='Other',phones=[],websites=['https://branch.business.kz/'])])
    assert not match_records(prospect,[crm_record(name='Other',phones=[],websites=['https://business.kz.evil.com/'])])


def test_public_purchase_request_needs_current_date_and_not_expired(prospect):
    prospect.purchase_evidence=[prospect.evidence[0]]
    prospect.sources[0].published_on=None
    assert assess(prospect,NOW)['purchase_interest']=='unconfirmed'
    prospect.sources[0].published_on=NOW.date()-timedelta(days=31)
    assert assess(prospect,NOW)['purchase_interest']=='unconfirmed'
    prospect.sources[0].published_on=NOW.date()
    prospect.sources[0].valid_until=NOW.date()-timedelta(days=1)
    assert assess(prospect,NOW)['purchase_interest']=='unconfirmed'
    prospect.sources[0].valid_until=None
    prospect.sources[0].kind='vacancy'
    prospect.sources[0].vacancy_status='active'
    assert assess(prospect,NOW)['purchase_interest']=='unconfirmed'


def test_timezone_boundary_does_not_make_a_source_future(prospect):
    from datetime import time
    checked=datetime.combine(NOW.date(),time(0,30),tzinfo=timezone(timedelta(hours=5)))
    source=prospect.sources[0].model_copy(update={'checked_at':checked})
    utc=checked.astimezone(timezone.utc)
    assert freshness(source,utc)[0]==1


@pytest.mark.parametrize('signals,expected,priority', [
    (['processing_problem','growth','inbound','team','channels','existing_crm'],70,'A'),
    ([s.value for s in CommercialSignal],100,'A'),
    (['growth','inbound','team','channels','existing_crm'],40,'B'),
    (['channels'],5,'C'),
])
def test_control_examples_need_is_independent_of_purchase_intent(prospect,signals,expected,priority):
    """Hypothetical scenarios: internal operational needs, no vendor purchase request."""
    facts={
        'processing_problem':'Зафиксированы потери заявок при передаче между сотрудниками.',
        'automation_project':'Внутренняя команда выполняет проект автоматизации обработки заказов.',
        'growth':'Открывается новый филиал компании.',
        'inbound':'Ежедневно поступает сто обращений клиентов.',
        'team':'В действующем отделе продаж работают три менеджера.',
        'channels':'Клиенты обращаются по телефону и WhatsApp.',
        'order_complexity':'Заказы требуют резервирования остатков и согласования поставки.',
        'existing_crm':'Компания ведёт клиентскую базу в CRM.',
    }
    prospect.sources[0].excerpt+='\n'+'\n'.join(facts[s] for s in signals)
    prospect.evidence=[prospect.evidence[0].model_copy(update={'signal':CommercialSignal(s),'quote':facts[s],'fact':facts[s]}) for s in signals]
    prospect.purchase_evidence=[]
    prospect=Candidate.model_validate(prospect.model_dump())
    q=assess(prospect,NOW)
    assert (q['score'],q['priority'],q['purchase_interest'])==(expected,priority,'unconfirmed')
    prospect.contacts=[]
    assert assess(prospect,NOW)['score']==expected  # No contact bonus is hidden in NEED_SCORE.


def test_full_scale_and_upper_range_reachable_without_purchase(prospect):
    from itertools import combinations
    from app.services.lead_hunter_scoring import WEIGHTS_V11
    assert sum(WEIGHTS_V11.values())==100
    template=prospect.evidence[0]
    attainable=set()
    for size in range(9):
        for signals in combinations(list(CommercialSignal),size):
            prospect.evidence=[template.model_copy(update={'signal':s}) for s in signals]
            q=assess(prospect,NOW)
            assert 0<=q['score']<=100 and q['purchase_interest']=='unconfirmed'
            attainable.add(q['score'])
    assert {70,75,80,85,90,95,100}<=attainable


def test_import_ready_true_only_filters_local_csv_and_has_no_network(prospect,monkeypatch,tmp_path):
    def forbidden_network(*args,**kwargs):
        raise AssertionError('CSV export must never connect to a network, including when IMPORT_READY=true')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden_network)
    prospect.crm_check=complete_check()
    batch=ResearchBatch(version='1.1',candidates=[prospect],search_log=['offline audit'])
    assert assess(prospect,NOW)['import_ready'] is True
    output=csv_v11(batch,import_only=True,as_of=NOW)
    from app.lead_hunter import write_private
    path=tmp_path/'ready.csv'
    write_private(path,output,'utf-8-sig')
    rows=list(csv.DictReader(path.open(encoding='utf-8-sig'),delimiter=';'))
    assert len(rows)==1 and rows[0]['IMPORT_READY']=='true'
