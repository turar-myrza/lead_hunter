"""Authenticated browser control center; no external writes or outreach endpoints."""
import hashlib,hmac,json,secrets,time
from pathlib import Path
from fastapi import APIRouter,Depends,HTTPException,Request,Response
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from app.core.config import get_settings
from app.services.sales_store import SalesStore
from app.services.control_store import ControlStore
from app.services.control_center import ControlCenter
from app.services.catalog import CATALOG, resolve_catalog_item
from app.services.platform_store import PlatformStore
from app.services.ads_manager import dispatch_offer
from app.models.control_center import ProductProfile,CampaignInput,SegmentApproval,Review
from app.services.lead_hunter_open_channels import client_from_settings,inventory
from app.integrations.bitrix24_open_channels import OpenChannelsError

router=APIRouter(prefix='/platform',tags=['Control center'])
WEB=Path(__file__).resolve().parents[1]/'web'
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}

class CallRequest(BaseModel):
    phone:str=Field(min_length=7,max_length=32)
    product:str=Field(default="Зертханалық центрифугалар",min_length=2,max_length=150)
    lang:str=Field(default="kz",pattern=r'^(kz|ru)$')
    details:str=Field(default="",max_length=1000)
    greeting:str=Field(default="",max_length=1000)
    custom_prompt:str=Field(default="",max_length=3000)
    voice:str=Field(default="",max_length=40)
    seller:str=Field(default="",max_length=200)

class OfferRequest(BaseModel):
    phone:str=Field(min_length=7,max_length=32)
    product:str=Field(default="centrifuges",min_length=2,max_length=150)
    lang:str=Field(default="kz",pattern=r'^(kz|ru)$')
    pdf_url:str=Field(default="",max_length=500)

def service():
    settings=get_settings()
    return ControlCenter(ControlStore(SalesStore(settings.lead_hunter_db)),settings)

def csrf(request:Request):
    if request.method not in {'GET','HEAD'}:
        if request.headers.get('x-control-request')!='1':raise HTTPException(403,'Same-origin request required')
        origin=request.headers.get('origin')
        if origin and origin!=str(request.base_url).rstrip('/'):raise HTTPException(403,'Origin rejected')

def auth(request:Request,settings=Depends(get_settings)):
    csrf(request)
    secret=settings.lead_hunter_api_token
    if not secret:raise HTTPException(503,'Control center authentication not configured')
    token=request.cookies.get('control_session','')
    try:
        stamp,nonce,signature=token.split('.')
        expected=hmac.new(secret.get_secret_value().encode(),(stamp+'.'+nonce).encode(),hashlib.sha256).hexdigest()
        if not secrets.compare_digest(expected,signature) or not 0<=int(stamp)-time.time()<=43200:raise ValueError
    except (ValueError,TypeError):raise HTTPException(401,'Sign in required') from None

class Login(BaseModel):
    password:str=Field(min_length=1,max_length=256)

@router.get('')
@router.get('/')
@router.head('')
@router.head('/')
def page():return FileResponse(WEB/'control.html',headers=HEADERS)

@router.get('/assets/{name}')
def asset(name:str):
    if name not in {'control.js','control.css','ads_manager.js','studio.js'}:raise HTTPException(404)
    return FileResponse(WEB/name,headers=HEADERS)

@router.post('/api/login',dependencies=[Depends(csrf)])
def login(data:Login,request:Request,response:Response,svc=Depends(service)):
    key=svc.settings.lead_hunter_api_token
    if not key:raise HTTPException(503,'Authentication not configured')
    ip=hashlib.sha256((request.client.host if request.client else 'unknown').encode()).hexdigest()
    with svc.store.base.connect() as db:
        db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM cc_login WHERE at<?',(time.time()-600,))
        count=db.execute('SELECT count(*) FROM cc_login WHERE ip=?',(ip,)).fetchone()[0]
        if count>=10:raise HTTPException(429,'Wait ten minutes before another sign-in attempt')
        db.execute('INSERT INTO cc_login VALUES(?,?)',(ip,time.time()))
    if not secrets.compare_digest(data.password.encode(),key.get_secret_value().encode()):
        svc.store.audit('login_failed','operator');raise HTTPException(401,'Invalid access password')
    payload=str(int(time.time()+43200))+'.'+secrets.token_hex(16)
    signature=hmac.new(key.get_secret_value().encode(),payload.encode(),hashlib.sha256).hexdigest()
    response.set_cookie('control_session',payload+'.'+signature,httponly=True,secure=svc.settings.control_secure_cookie,samesite='strict',path='/platform',max_age=43200)
    svc.store.audit('login','operator');return {'authenticated':True}

@router.post('/api/logout',dependencies=[Depends(auth)])
def logout(response:Response):
    response.delete_cookie('control_session',path='/platform');return {'authenticated':False}

@router.get('/api/state',dependencies=[Depends(auth)])
def state(svc=Depends(service)):
    result=svc.snapshot()
    result['capabilities']={'product_ai':bool(svc.settings.control_ai_enabled and svc.settings.control_gemini_key),
        'crm_configured':bool(svc.settings.bitrix24_webhook_url or svc.settings.bitrix24_open_channels_webhook_url),
        'crm_writes':True,'external_calls':True,'external_messages':True,'voice_quality':'approved_b2b_v3.3.28',
        'telephony_host':svc.settings.telephony_host,
        'source_adapters':['product_public: GDELT + approved public URLs','evidence_intake','shymkent_crm','kazakhstan_sales','asterisk_sip_sdr','meta_whatsapp_cloud'],
        'social_search':'telegram_scraper_active','individual_discovery':'live_outreach_enabled',
        'daily_ai_call_limit':svc.settings.control_ai_daily_calls}
    pstore = PlatformStore(svc.store.base)
    result['catalog']=pstore.list_products()
    result['company']=pstore.get_company_profile()
    result['telephony']=pstore.get_telephony_config()
    return result

@router.get('/api/outreach/catalog',dependencies=[Depends(auth)])
def outreach_catalog(svc=Depends(service)):
    return PlatformStore(svc.store.base).list_products()

@router.get('/api/outreach/history',dependencies=[Depends(auth)])
def outreach_history(svc=Depends(service)):
    return svc.store.outreach_history(50)

@router.post('/api/outreach/call',dependencies=[Depends(auth)])
def start_outreach_call(data:CallRequest,svc=Depends(service)):
    from calling.pbx_caller import call_lead
    pstore = PlatformStore(svc.store.base)
    company = pstore.get_company_profile()
    telephony = pstore.get_telephony_config()
    phone=data.phone.strip()
    product=data.product.strip()
    lang=data.lang.strip().lower()
    details=data.details.strip()
    seller=(data.seller or company.get('company_name') or '').strip()
    voice=(data.voice or telephony.get('voice_name') or 'Kore').strip()
    greeting=(data.greeting or (telephony.get('custom_greeting_kz') if lang=='kz' else telephony.get('custom_greeting_ru')) or '').strip()
    custom_prompt=(data.custom_prompt or (telephony.get('custom_prompt_kz') if lang=='kz' else telephony.get('custom_prompt_ru')) or '').strip()
    transfer=(telephony.get('transfer_extension') or '055').strip()

    res=call_lead(
        phone=phone,
        products=[product],
        details=details,
        lang=lang,
        greeting=greeting,
        seller=seller,
        voice=voice,
        custom_prompt=custom_prompt,
        transfer_target=transfer,
        host=svc.settings.telephony_host,
        user=svc.settings.telephony_ssh_user,
        remote_script=svc.settings.telephony_caller_script,
    )
    status='initiated' if res.get('success') else 'failed'
    response_text=(res.get('stdout','') or res.get('stderr',''))[:500]
    svc.store.log_outreach_call(phone,product,lang,status,response_text)

    if not res.get('success'):
        raise HTTPException(502,f"Telephony error: {res.get('stderr') or res.get('stdout') or 'Unknown error'}")

    return {'status':'initiated','phone':phone,'product':product,'lang':lang,'mode':res.get('mode'),'output':response_text}

@router.post('/api/outreach/offer',dependencies=[Depends(auth)])
def send_outreach_offer(data:OfferRequest,svc=Depends(service)):
    phone=data.phone.strip()
    product_id=data.product.strip()
    lang=data.lang.strip().lower()
    matched=dict(resolve_catalog_item(product_id))
    if data.pdf_url.strip():
        key="pdf_kz" if lang=="kz" else "pdf_ru"
        matched[key]=data.pdf_url.strip()
    try:
        return dispatch_offer(phone,matched,lang,svc.settings,svc.store)
    except Exception as exc:
        raise HTTPException(502,f"WhatsApp dispatch failed: {str(exc)}") from exc

@router.post('/api/products',dependencies=[Depends(auth)])
def create_product(data:ProductProfile,svc=Depends(service)):
    return svc.store.save_product(data.model_dump(mode='json'))

@router.put('/api/products/{ident}',dependencies=[Depends(auth)])
def update_product(ident:int,data:ProductProfile,svc=Depends(service)):
    return invoke(lambda:svc.store.save_product(data.model_dump(mode='json'),ident))

@router.post('/api/products/{ident}/analyze',dependencies=[Depends(auth)])
def analyze(ident:int,svc=Depends(service)):return invoke(lambda:svc.analyze(ident))

@router.put('/api/segments/{ident}',dependencies=[Depends(auth)])
def segment(ident:int,data:SegmentApproval,svc=Depends(service)):
    invoke(lambda:svc.approve_segment(ident,data.segment,data.approved));return {'saved':True,'approved':data.approved}

@router.post('/api/campaigns',dependencies=[Depends(auth)])
def campaign(data:CampaignInput,svc=Depends(service)):return invoke(lambda:svc.campaign(data.segment_id,data.target))

@router.put('/api/jobs/{job}/prospects/{candidate}/review',dependencies=[Depends(auth)])
def review(job:int,candidate:str,data:Review,svc=Depends(service)):
    invoke(lambda:svc.review(job,candidate,data));return {'saved':True}

@router.post('/api/jobs/{job}/prospects/{candidate}/handoff',dependencies=[Depends(auth)])
def handoff(job:int,candidate:str,svc=Depends(service)):return invoke(lambda:svc.handoff(job,candidate))

@router.get('/api/crm-status',dependencies=[Depends(auth)])
async def crm_status(svc=Depends(service)):
    try:
        async with client_from_settings(svc.settings) as client:
            scopes=await client.read('scope')
            result={'reachable':True,'crm_scope':'crm' in scopes,'open_channels_scope':'imopenlines' in scopes,'writes_enabled':False}
            if 'imopenlines' in scopes:result['inventory']=await inventory(client)
            return result
    except OpenChannelsError as exc:return {'reachable':False,'reason':exc.code,'writes_enabled':False}

def invoke(call):
    try:return call()
    except ValueError as exc:raise HTTPException(409,str(exc)) from None
