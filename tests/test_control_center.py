import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import Settings,get_settings
from app.api.control_center import service
from app.models.control_center import ProductProfile,ProductIntelligence,PublicDemandExtraction,QuickProductInput,Segment,Review
from app.models.universal_lead_hunter import ProductBrief,RunRequest
from app.services.sales_store import SalesStore
from app.services.control_store import ControlStore
from app.services.control_center import ControlCenter
from app.services.product_discovery import ProductDiscovery
from app.integrations.product_intelligence import ProductAI,IntelligenceError
from app.integrations.lead_hunter_public import PublicResponse
from app.services.universal_sources import Discovery
from datetime import datetime,timezone


def profile():
    return ProductProfile(brief=ProductBrief(name='Cake trays',description='Rigid trays for safe cake transport',category='Packaging',market='Warsaw',buyer_paths={'organization'}),public_analysis_allowed=True)

def segment(name='Bakeries'):
    return Segment(name=name,path='organization',characteristics='Bakeries making custom cakes',roles=['Procurement','Owner'],motivations=['Stable transport'],signals=['Custom cakes'],queries=['Warsaw custom cakes'],criteria=['Makes cakes'])

def intelligence():
    return ProductIntelligence(value_proposition='Stable cake transport',problems_solved=['Cake movement'],use_cases=['Delivery'],purchase_barriers=['Fit'],missing_information=['Price'],segments=[segment(),segment('Cake workshops')])

class FakeAI:
    async def generate(self,*args):return intelligence()

@pytest.fixture
def svc(tmp_path):
    settings=Settings(_env_file=None,lead_hunter_api_token='test-password',lead_hunter_db=tmp_path/'state.db',lead_hunter_output=tmp_path/'out',
        control_ai_enabled=False,control_ai_web_research_enabled=False)
    return ControlCenter(ControlStore(SalesStore(settings.lead_hunter_db)),settings,FakeAI())

@pytest.fixture
def client(svc):
    app.dependency_overrides[get_settings]=lambda:svc.settings
    app.dependency_overrides[service]=lambda:svc
    with TestClient(app,base_url='https://testserver') as c:yield c
    app.dependency_overrides.clear()

def login(client):
    client.headers['X-Control-Request']='1'
    response=client.post('/platform/api/login',json={'password':'test-password'})
    assert response.status_code==200
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']

def test_auth_csrf_and_public_shell(client):
    assert client.get('/platform').status_code==200
    assert client.get('/platform/api/state').status_code==401
    assert client.post('/platform/api/login',json={'password':'test-password'}).status_code==403
    login(client)
    assert client.get('/platform/api/state').status_code==200
    assert client.post('/platform/api/products',json=profile().model_dump(mode='json'),headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/platform/assets/secret').status_code==404
    client.post('/platform/api/logout')
    assert client.get('/platform/api/state').status_code==401

async def test_persistent_analysis_approval_and_invalidation(svc):
    product=svc.store.save_product(profile().model_dump(mode='json'))
    job=svc.analyze(product['id']);assert job['status']=='queued'
    with pytest.raises(ValueError,match='already_active'):svc.analyze(product['id'])
    assert await svc.worker_once()
    persisted=ControlStore(SalesStore(svc.store.base.path)).snapshot()
    assert persisted['jobs'][0]['status']=='completed'
    assert len(persisted['segments'])==2
    s=persisted['segments'][0]
    with pytest.raises(ValueError,match='approval'):svc.campaign(s['id'],20)
    svc.approve_segment(s['id'],Segment.model_validate(s['payload']),True)
    campaign=svc.campaign(s['id'],20)
    assert campaign['payload']['target']==20
    svc.store.save_product(profile().model_dump(mode='json'),product['id'])
    assert not svc.store.segment(s['id'])['approved']

def test_public_ai_approval_required(svc):
    p=profile().model_copy(update={'public_analysis_allowed':False})
    ident=svc.store.save_product(p.model_dump(mode='json'))['id']
    with pytest.raises(ValueError,match='public_material'):svc.analyze(ident)


async def test_quick_start_needs_only_a_short_product_brief_and_queues_analysis(svc):
    started=svc.quick_start(QuickProductInput(
        name='Cake delivery trays',
        description='Rigid reusable trays that help bakeries transport custom cakes safely.',
        market='Warsaw',
        public_analysis_allowed=True,
    ))
    product=started['product']
    assert product['payload']['brief']['category']=='Категория определяется AI после анализа продукта'
    assert set(product['payload']['brief']['buyer_paths'])=={'individual','organization'}
    assert started['analysis_job']['status']=='queued'
    assert await svc.worker_once()
    assert svc.store.job(started['analysis_job']['id'])['status']=='completed'


def test_quick_start_keeps_public_ai_consent_explicit(svc):
    with pytest.raises(ValueError,match='public_material'):
        svc.quick_start(QuickProductInput(name='Example product',description='A short useful product description.',market='Shymkent'))

async def test_model_error_is_persisted_not_fake_analysis(svc):
    class Broken:
        async def generate(self,*a):raise IntelligenceError('ai_provider_http_403')
    svc.ai=Broken();p=svc.store.save_product(profile().model_dump(mode='json'));j=svc.analyze(p['id'])
    await svc.worker_once()
    assert svc.store.job(j['id'])['error']=='ai_provider_http_403'
    assert not svc.store.snapshot()['segments']

async def test_ai_usage_and_budget(svc):
    s=svc.settings.model_copy(update={'control_ai_enabled':True,'control_gemini_key':__import__('pydantic').SecretStr('mock-key'),'control_ai_daily_calls':1})
    def respond(request):
        body=json.loads(request.content)
        assert 'CRM history' not in body['contents'][0]['parts'][0]['text']
        return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':intelligence().model_dump_json()}]}}],'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':20}})
    ai=ProductAI(s,svc.store,httpx.MockTransport(respond))
    assert (await ai.generate('product_analysis',profile().model_dump(mode='json'),ProductIntelligence)).segments
    assert svc.store.snapshot()['usage'][0]['input_tokens']==10
    with pytest.raises(IntelligenceError,match='daily'):await ai.generate('product_analysis',{},ProductIntelligence)


async def test_grounded_public_web_only_returns_cited_safe_public_pages(svc):
    settings=svc.settings.model_copy(update={
        'control_ai_enabled':True,
        'control_gemini_key':__import__('pydantic').SecretStr('mock-key'),
        'control_ai_web_research_enabled':True,
        'control_ai_web_research_max_sources':8,
    })
    def respond(request):
        body=json.loads(request.content)
        assert body['tools']==[{'google_search':{}}]
        assert 'CRM history' not in body['contents'][0]['parts'][0]['text']
        return httpx.Response(200,json={'candidates':[{'groundingMetadata':{'groundingChunks':[
            {'web':{'uri':'https://company.kz/news/growth','title':'Company growth'}},
            {'web':{'uri':'https://t.me/s/public_company','title':'Public channel'}},
            {'web':{'uri':'https://t.me/private_group','title':'Not a public channel view'}},
            {'web':{'uri':'https://company.kz/news/growth','title':'Duplicate'}},
        ]}}],'usageMetadata':{'promptTokenCount':5,'candidatesTokenCount':6}})
    ai=ProductAI(settings,svc.store,httpx.MockTransport(respond))
    sources=await ai.search_public_web(query='Find public companies with B2B sales growth in Shymkent.',context={'market':'Shymkent'})
    assert [item['url'] for item in sources]==['https://company.kz/news/growth','https://t.me/s/public_company']
    assert svc.store.snapshot()['usage'][0]['operation']=='public_web_research'


async def test_grounded_adapter_resolves_only_google_grounding_redirects(svc,monkeypatch):
    from app.services.grounded_public_discovery import GroundedPublicDiscovery
    s=segment()
    class SearchAI:
        async def search_public_web(self,**kwargs):
            return [
                {'url':'https://vertexaisearch.cloud.google.com/grounding-api-redirect/token','title':'Google citation'},
                {'url':'https://company.kz/news/growth','title':'Direct public page'},
            ]
    adapter=GroundedPublicDiscovery(SearchAI(),s)
    async def resolve(url):
        assert 'vertexaisearch.cloud.google.com' in url
        return 'https://bakery.kz/news/branch'
    monkeypatch.setattr(adapter.fetcher,'resolve_grounding_redirect',resolve)
    result=await adapter.discover(RunRequest(brief=profile().brief,limit=5))
    assert result.payload==['https://bakery.kz/news/branch','https://company.kz/news/growth']


async def test_google_grounding_redirect_is_resolved_without_general_redirects(monkeypatch):
    from app.integrations.lead_hunter_public import PublicFetcher, PublicResponse
    fetcher=PublicFetcher()
    async def request(url, host, delay, *, allow_redirect=False):
        assert host=='vertexaisearch.cloud.google.com'
        assert allow_redirect is True
        return PublicResponse(url,302,'',b'',datetime.now(timezone.utc),'https://bakery.kz/news/new-branch')
    monkeypatch.setattr(fetcher,'_get',request)
    assert await fetcher.resolve_grounding_redirect('https://vertexaisearch.cloud.google.com/grounding-api-redirect/token') == 'https://bakery.kz/news/new-branch'


async def test_b2c_public_demand_keeps_source_signal_not_a_person(monkeypatch):
    from app.services.b2c_public_demand import B2CPublicDemandDiscovery
    brief=ProductBrief(name='Support belt',description='A wearable product for back support.',category='Consumer product',market='Shymkent',buyer_paths={'individual'})
    request=RunRequest(brief=brief,source_ids=['b2c_public_demand'],limit=5)
    segment=Segment(name='Drivers',path='individual',characteristics='Drivers in Shymkent',roles=['Buyer'],motivations=['Comfort'],signals=['Public topic'],queries=['support belt'],criteria=['Opt-in'])
    class SearchAI:
        async def search_public_web(self,**kwargs): return [{'url':'https://t.me/s/public_channel','title':'Public channel'}]
        async def generate(self,operation,payload,schema):
            assert operation=='public_b2c_demand_page'
            return PublicDemandExtraction.model_validate({'signals':[{
                'source_label':'Public Shymkent channel',
                'source_label_quote':'Public Shymkent channel',
                'location_quote':'Shymkent',
                'demand_quote':'Shymkent readers discuss support belts for long drives.'
            }]})
    adapter=B2CPublicDemandDiscovery(SearchAI(),segment)
    text=('<h1>Public Shymkent channel</h1><p>Shymkent readers discuss support belts for long drives.</p>'
          '<p>This public channel publishes practical city information and transportation topics.</p>')
    async def fetch(*args,**kwargs):return PublicResponse('https://t.me/s/public_channel',200,'text/html',text.encode(),datetime.now(timezone.utc))
    monkeypatch.setattr(adapter.fetcher,'fetch',fetch)
    discovered=await adapter.discover(request)
    verified=await adapter.verify(request,discovered)
    assert len(verified.records)==1
    candidate=verified.records[0].candidate
    assert candidate.path=='individual' and candidate.subject_kind=='public_demand_signal'
    from app.services.universal_qualification import build_opportunity,qualify
    assert candidate.evidence[-1].kind=='market_signal'
    assert build_opportunity(request,candidate,qualify(request,candidate),source_id='b2c_public_demand').engagement.permission=='blocked'


async def test_b2c_public_demand_rejects_medical_claims(monkeypatch):
    from app.services.b2c_public_demand import B2CPublicDemandDiscovery
    brief=ProductBrief(name='Support belt',description='Consumer support product.',category='Consumer product',market='Shymkent',buyer_paths={'individual'})
    request=RunRequest(brief=brief,source_ids=['b2c_public_demand'],limit=5)
    audience=Segment(name='Drivers',path='individual',characteristics='Drivers in Shymkent',roles=['Buyer'],motivations=['Comfort'],signals=['Public topic'],queries=['support belt'],criteria=['Opt-in'])
    class Model:
        async def search_public_web(self,**kwargs):return [{'url':'https://t.me/s/public_channel','title':'Public channel'}]
        async def generate(self,*args):return PublicDemandExtraction.model_validate({'signals':[{
            'source_label':'Public Shymkent channel','source_label_quote':'Public Shymkent channel',
            'location_quote':'Shymkent','demand_quote':'Shymkent readers discuss treatment of chronic back pain.'}]})
    adapter=B2CPublicDemandDiscovery(Model(),audience)
    text=('<h1>Public Shymkent channel</h1><p>Shymkent readers discuss treatment of chronic back pain.</p>'
          '<p>This public channel publishes practical city information and transportation topics.</p>')
    async def fetch(*args,**kwargs):return PublicResponse('https://t.me/s/public_channel',200,'text/html',text.encode(),datetime.now(timezone.utc))
    monkeypatch.setattr(adapter.fetcher,'fetch',fetch)
    assert (await adapter.verify(request,await adapter.discover(request))).records==[]


def test_strategy_enables_grounded_research_only_when_configured(svc):
    crm=ProductProfile(brief=ProductBrief(name='CRM implementation',description='Bitrix24 sales automation',category='CRM services',market='Шымкент',buyer_paths={'organization'}),public_analysis_allowed=True)
    segment=Segment(name='B2B sales companies',path='organization',characteristics='Growing B2B firms',roles=['Owner'],motivations=['Control sales'],signals=['Sales hiring'],queries=['seller query'],criteria=['Multiple salespeople'])
    assert svc._discovery_strategy(crm,segment)['source_ids']==['shymkent_crm']
    svc.settings=svc.settings.model_copy(update={
        'control_ai_web_research_enabled':True,
        'control_ai_enabled':True,
        'control_gemini_key':__import__('pydantic').SecretStr('mock-key'),
    })
    assert svc._discovery_strategy(crm,segment)['source_ids']==['shymkent_crm','grounded_public_web']


def test_individual_strategy_uses_public_demand_research_without_crm(svc):
    product=ProductProfile(brief=ProductBrief(name='Support belt',description='Consumer back-support product',category='Consumer goods',market='Shymkent',buyer_paths={'individual'}),public_analysis_allowed=True)
    segment=Segment(name='Drivers',path='individual',characteristics='Drivers',roles=['Buyer'],motivations=['Comfort'],signals=['Public discussion'],queries=['support belt'],criteria=['Opt-in'])
    strategy=svc._discovery_strategy(product,segment)
    assert strategy['source_ids']==['b2c_public_demand'] and strategy['crm_read_only'] is False


def test_worker_recovery_requeues_only_adaptive_discovery(svc):
    from app.control_worker import recover_interrupted_jobs
    product=svc.store.save_product(profile().model_dump(mode='json'))
    discovery=svc.store.enqueue('discovery',product,{'product':product['payload'],'segment_id':1,'segment':segment().model_dump(mode='json'),'target':1})
    second=svc.store.save_product(profile().model_dump(mode='json'))
    analysis=svc.store.enqueue('analysis',second,second['payload'])
    with svc.store.base.connect() as db:
        db.execute("UPDATE cc_job SET status='running' WHERE id IN (?,?)",(discovery['id'],analysis['id']))
    assert recover_interrupted_jobs(svc.store.base)=={'resumed_discovery':1,'review_required':1}
    assert svc.store.job(discovery['id'])['status']=='queued'
    assert svc.store.job(analysis['id'])['status']=='failed'

async def test_product_source_requires_exact_quotes_and_location(svc):
    from app.models.control_center import Extraction
    text='<h1>Example Bakery</h1><p>Example Bakery in Warsaw makes custom cakes.</p><p>Procurement lead Anna chooses packaging.</p>'+'<p>We prepare celebration cakes for transport and delivery.</p>'
    class Fetch:
        async def fetch(self,*a,**k):return PublicResponse('https://bakery.example.org/',200,'text/html',text.encode(),datetime.now(timezone.utc))
    class Model:
        async def generate(self,*a):return Extraction.model_validate({'organizations':[
            {'name':'Example Bakery','industry':'Bakery','location':'Warsaw','identity_quote':'Example Bakery in Warsaw makes custom cakes.','location_quote':'Example Bakery in Warsaw makes custom cakes.','need_quote':'We prepare celebration cakes for transport and delivery.','role_quote':'Procurement lead Anna chooses packaging.','role_actor':'Anna','role_kind':'procurement'},
            {'name':'Invented Bakery','industry':'Bakery','location':'Warsaw','identity_quote':'Invented Bakery','location_quote':'Warsaw'}]})
    request=RunRequest(brief=profile().brief,limit=20)
    result=await ProductDiscovery(Model(),fetcher=Fetch()).verify(request,Discovery(['https://bakery.example.org/']))
    assert len(result.records)==1 and result.records[0].candidate.roles[0].actor=='Anna'
    assert result.records[0].candidate.evidence[0].basis=='public_page'


async def test_product_source_does_not_queue_company_without_a_direct_need_signal():
    from app.models.control_center import Extraction
    text='<h1>Example Retail</h1><p>Example Retail in Shymkent operates 20 stores.</p>'
    class Fetch:
        async def fetch(self,*a,**k):return PublicResponse('https://retail.example.org/',200,'text/html',text.encode(),datetime.now(timezone.utc))
    class Model:
        async def generate(self,*a):return Extraction.model_validate({'organizations':[
            {'name':'Example Retail','industry':'Retail','location':'Shymkent','identity_quote':'Example Retail in Shymkent operates 20 stores.','location_quote':'Example Retail in Shymkent operates 20 stores.'}
        ]})
    request=RunRequest(brief=ProductBrief(name='CRM',description='Automation',category='Services',market='Shymkent',buyer_paths={'organization'}),limit=5)
    result=await ProductDiscovery(Model(),fetcher=Fetch()).verify(request,Discovery(['https://retail.example.org/']))
    assert result.records==[]

async def test_campaign_same_orchestrator_and_no_outreach(svc,monkeypatch):
    from app.services import control_center as module
    from app.models.universal_lead_hunter import Candidate,Evidence
    from app.services.universal_sources import VerifiedCandidate,Verification
    product=svc.store.save_product(profile().model_dump(mode='json'));svc.analyze(product['id']);await svc.worker_once()
    s=svc.store.snapshot()['segments'][0];svc.approve_segment(s['id'],Segment.model_validate(s['payload']),True)
    async def discover(self,r):return Discovery([])
    async def verify(self,r,d):return Verification([VerifiedCandidate(Candidate(id='sample',label='Example Bakery',path='organization',evidence=[Evidence(id='need',kind='need',excerpt='Custom cake packaging requirement',url='https://example.org/',basis='public_page')]))])
    monkeypatch.setattr(module.ProductDiscovery,'discover',discover);monkeypatch.setattr(module.ProductDiscovery,'verify',verify)
    from app.services import sales_crm
    async def crm(*a,**k):return {'Example Bakery':{'state':'no_match_accessible'}},{}
    monkeypatch.setattr(sales_crm,'check_sales_crm',crm)
    j=svc.campaign(s['id'],20);await svc.worker_once();j=svc.store.job(j['id'])
    assert j['status']=='partial' and j['result']['found']==1 and j['result']['shortfall']==19
    snapshot=svc.snapshot();job=next(x for x in snapshot['jobs'] if x['id']==j['id'])
    assert len(job['workflow']['stages'])==8
    assert job['adaptive']['goal']['current_objective'].startswith('Find named organizations')
    assert job['adaptive']['attempts'][0]['capability_id']=='product_public'
    assert job['adaptive']['attempts'][0]['evaluation']['evidence_quality']=='verifiable'
    assert job['opportunities'][0]['engagement']['permission']=='review_only'
    with pytest.raises(ValueError,match='qualified'):svc.handoff(j['id'],'sample')
    svc.review(j['id'],'sample',Review(state='qualified',ethics_passed=True,rationale='Reviewed published packaging requirement',evidence_ids=['need'],next_action='Confirm specification internally'))
    assert svc.handoff(j['id'],'sample')['writes_enabled'] is False

def test_crm_shymkent_search_strategy_is_automatic(svc):
    crm=ProductProfile(brief=ProductBrief(name='CRM implementation',description='Bitrix24 sales automation',category='CRM services',market='Шымкент',buyer_paths={'organization'}),public_analysis_allowed=True)
    product=svc.store.save_product(crm.model_dump(mode='json'))
    analysis=svc.store.enqueue('analysis',product,crm.model_dump(mode='json'))
    svc.store.finish(analysis['id'],'completed',{'analysis':'synthetic for strategy test'})
    segment=Segment(name='B2B sales companies',path='organization',characteristics='Growing B2B firms',roles=['Owner'],motivations=['Control sales'],signals=['Sales hiring'],queries=['seller query'],criteria=['Multiple salespeople'],source_urls=['https://example.org/'])
    with svc.store.base.connect() as db:
        db.execute('INSERT INTO cc_segment(job_id,product_id,revision,payload,approved) VALUES(?,?,?,?,1)',
                   (analysis['id'],product['id'],product['revision'],segment.model_dump_json()))
    saved=svc.store.snapshot()['segments'][0]
    job=svc.campaign(saved['id'],5)
    assert job['payload']['strategy']['source_ids']==['shymkent_crm']
    assert job['payload']['strategy']['research_context']=='crm_sales_operations'
    assert job['payload']['strategy']['crm_read_only'] is True

def test_rate_limit(client):
    client.headers['X-Control-Request']='1'
    for _ in range(10):assert client.post('/platform/api/login',json={'password':'wrong'}).status_code==401
    assert client.post('/platform/api/login',json={'password':'test-password'}).status_code==429

async def test_existing_offer_engine_queue_and_download(client,svc,monkeypatch):
    import sys
    from types import SimpleNamespace
    called=[]
    def generate(**kwargs):
        called.append(kwargs)
        Path(kwargs['output_docx_path']).write_bytes(b'test docx')
        Path(kwargs['output_pdf_path']).write_bytes(b'%PDF-test')
        return {'docx':kwargs['output_docx_path'],'pdf':kwargs['output_pdf_path']}
    monkeypatch.setitem(sys.modules,'documents.offer_generator',SimpleNamespace(generate_commercial_offer=generate))
    product=svc.store.save_product(profile().model_dump(mode='json'))
    payload={'client_company':'Synthetic bakery','lang':'kz','items':[{'name':'Trays','price':'100'}]}
    assert client.post(f"/platform/api/products/{product['id']}/offers",json=payload).status_code in (401,403)
    login(client)
    assert client.post(f"/platform/api/products/{product['id']}/offers",json=payload).status_code==409
    payload['template_reviewed']=True
    response=client.post(f"/platform/api/products/{product['id']}/offers",json=payload)
    ident=response.json()['id']
    assert client.get(f'/platform/api/offers/{ident}/pdf').status_code==404
    assert await svc.worker_once()
    assert called[0]['lang']=='kz'
    assert called[0]['items'][0]['name']=='Trays'
    assert svc.store.job(ident)['result']['sent'] is False
    assert client.get(f'/platform/api/offers/{ident}/pdf').content==b'%PDF-test'
    assert client.get(f'/platform/api/offers/{ident}/env').status_code==404
    client.post('/platform/api/logout')
    assert client.get(f'/platform/api/offers/{ident}/pdf').status_code==401

def test_offer_path_and_language_validation(client,svc):
    login(client)
    product=svc.store.save_product(profile().model_dump(mode='json'))
    payload={'client_company':'../../escape','items':[{'name':'Test'}],'template_reviewed':True}
    assert client.post(f"/platform/api/products/{product['id']}/offers",json=payload).status_code==422

def test_knowledge_is_authenticated_read_only_snapshot(client,svc,tmp_path):
    assert client.get('/platform/api/knowledge').status_code==401
    login(client)
    assert client.get('/platform/api/knowledge').json()['available'] is False
    p=tmp_path/'knowledge.md';p.write_text('Synthetic product facts')
    svc.settings.control_knowledge_path=str(p)
    result=client.get('/platform/api/knowledge').json()
    assert result['content']=='Synthetic product facts'
    assert result['sent_to_external_ai'] is False
    assert result['voice_agent_loading']=='not_verified'
    assert client.put('/platform/api/knowledge',json={'content':'changed'}).status_code==405
    assert p.read_text()=='Synthetic product facts'
