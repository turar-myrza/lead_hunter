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
from app.models.control_center import ProductProfile,CampaignInput,SegmentApproval,Review,OfferInput
from app.services.lead_hunter_open_channels import client_from_settings,inventory
from app.integrations.bitrix24_open_channels import OpenChannelsError

router=APIRouter(prefix='/platform',tags=['Control center'])
WEB=Path(__file__).resolve().parents[1]/'web'
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}

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
def page():return FileResponse(WEB/'control.html',headers=HEADERS)

@router.get('/assets/{name}')
def asset(name:str):
    if name not in {'control.js','control.css'}:raise HTTPException(404)
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
        'crm_writes':False,'external_calls':False,'external_messages':False,'voice_quality':'not_approved',
        'source_adapters':['product_public: GDELT + approved public URLs','evidence_intake','shymkent_crm','kazakhstan_sales'],
        'social_search':'unavailable','individual_discovery':'permissioned inbound adapter required',
        'daily_ai_call_limit':svc.settings.control_ai_daily_calls}
    return result

@router.get('/api/knowledge',dependencies=[Depends(auth)])
def knowledge(svc=Depends(service)):
    configured=svc.settings.control_knowledge_path
    if not configured:return {'available':False,'reason':'knowledge_file_not_configured'}
    try:
        path=Path(configured)
        if path.stat().st_size>200000:return {'available':False,'reason':'knowledge_file_too_large'}
        content=path.read_text(encoding='utf-8')
    except (OSError,UnicodeError):return {'available':False,'reason':'knowledge_file_unavailable'}
    return {'available':True,'content':content,'sha256':hashlib.sha256(content.encode()).hexdigest(),
        'mode':'read_only_deployment_snapshot','voice_agent_loading':'not_verified',
        'sent_to_external_ai':False}

@router.post('/api/products',dependencies=[Depends(auth)])
def create_product(data:ProductProfile,svc=Depends(service)):
    return svc.store.save_product(data.model_dump(mode='json'))

@router.put('/api/products/{ident}',dependencies=[Depends(auth)])
def update_product(ident:int,data:ProductProfile,svc=Depends(service)):
    return invoke(lambda:svc.store.save_product(data.model_dump(mode='json'),ident))

@router.post('/api/products/{ident}/analyze',dependencies=[Depends(auth)])
def analyze(ident:int,svc=Depends(service)):return invoke(lambda:svc.analyze(ident))

@router.post('/api/products/{ident}/offers',dependencies=[Depends(auth)])
def offer(ident:int,data:OfferInput,svc=Depends(service)):
    return invoke(lambda:svc.offer(ident,data))

@router.get('/api/offers/{ident}/{extension}',dependencies=[Depends(auth)])
def download_offer(ident:int,extension:str,svc=Depends(service)):
    job=svc.store.job(ident)
    if extension not in {'pdf','docx'} or not job or job['kind']!='offer' or job['status']!='completed':
        raise HTTPException(404,'Document not available')
    path=Path(svc.settings.lead_hunter_output).resolve()/'offers'/str(ident)/('offer.'+extension)
    if not path.is_file():raise HTTPException(404,'Document not available')
    return FileResponse(path,filename='offer.'+extension,headers=HEADERS)

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
