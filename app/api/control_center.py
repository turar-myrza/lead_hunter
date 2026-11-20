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
from app.models.control_center import ProductProfile,CampaignInput,SegmentApproval,Review
from app.services.lead_hunter_open_channels import client_from_settings,inventory
from app.integrations.bitrix24_open_channels import OpenChannelsError

router=APIRouter(prefix='/platform',tags=['Control center'])
WEB=Path(__file__).resolve().parents[1]/'web'
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}

CATALOG=[
    {
        "id":"centrifuges",
        "name":"Зертханалық центрифугалар",
        "name_ru":"Лабораторные центрифуги",
        "category":"Медициналық және зертханалық жабдықтар",
        "price_range":"185 000 – 1 850 000 ₸",
        "models":["DLAB DM0412","Biosan LMC-3000","Armed 80-2S","Eppendorf 5424 R"],
        "specs":"15 000 айн/мин дейін, 1.5-50 мл пробиркалар, жасушалық және қан сарысуы талдауы, ресми кепілдік 12-24 ай.",
        "pdf_kz":"https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
        "pdf_ru":"https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
    },
    {
        "id":"tires",
        "name":"Автокөлік шиналары (Жеңіл және жүк көліктері)",
        "name_ru":"Автомобильные шины (Легковые и грузовые)",
        "category":"Автокөлік тауарлары және парктік жабдықтау",
        "price_range":"24 500 – 85 000 ₸",
        "models":["Viatti Strada Asimmetrico","Cordiant Comfort 2","Triangle AdvanteX","Cordiant Gravity"],
        "specs":"R14-R18 жазғы/қысқы шиналар, күшейтілген бүйірлік қабырғалар, дистрибьютордың тікелей қоймасынан көтерме баға.",
        "pdf_kz":"https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
        "pdf_ru":"https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
    },
    {
        "id":"scalpels",
        "name":"Хирургиялық скальпельдер мен бір реттік лезвиялар",
        "name_ru":"Хирургические скальпели и одноразовые лезвия",
        "category":"Медициналық шығын материалдары",
        "price_range":"45 – 180 ₸ / дана",
        "models":["Лезвия №10, №11, №15, №20, №24","Скальпель брюшистый/остроконечный"],
        "specs":"Көміртекті және тот баспайтын хирургиялық болат, жеке стерильді фольга қаптама, ҚР тіркеу куәлігі бар.",
        "pdf_kz":"https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
        "pdf_ru":"https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
    }
]

class CallRequest(BaseModel):
    phone:str=Field(min_length=7,max_length=32)
    product:str=Field(default="Зертханалық центрифугалар",min_length=2,max_length=150)
    lang:str=Field(default="kz",pattern=r'^(kz|ru)$')
    details:str=Field(default="",max_length=1000)

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
        'crm_writes':True,'external_calls':True,'external_messages':True,'voice_quality':'approved_b2b_v3.3.28',
        'telephony_host':svc.settings.telephony_host,
        'source_adapters':['product_public: GDELT + approved public URLs','evidence_intake','shymkent_crm','kazakhstan_sales','asterisk_sip_sdr','meta_whatsapp_cloud'],
        'social_search':'telegram_scraper_active','individual_discovery':'live_outreach_enabled',
        'daily_ai_call_limit':svc.settings.control_ai_daily_calls}
    result['catalog']=CATALOG
    return result

@router.get('/api/outreach/catalog',dependencies=[Depends(auth)])
def outreach_catalog():
    return CATALOG

@router.get('/api/outreach/history',dependencies=[Depends(auth)])
def outreach_history(svc=Depends(service)):
    return svc.store.outreach_history(50)

@router.post('/api/outreach/call',dependencies=[Depends(auth)])
def start_outreach_call(data:CallRequest,svc=Depends(service)):
    from calling.pbx_caller import call_lead
    phone=data.phone.strip()
    product=data.product.strip()
    lang=data.lang.strip().lower()
    details=data.details.strip()

    res=call_lead(
        phone=phone,
        products=[product],
        details=details,
        lang=lang,
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
    from integrations.whatsapp_sender import send_whatsapp_template
    phone=data.phone.strip()
    product_id=data.product.strip()
    lang=data.lang.strip().lower()
    meta_lang="kk" if lang=="kz" else "ru"

    matched=next((c for c in CATALOG if c["id"]==product_id or c["name"]==product_id or c["name_ru"]==product_id), CATALOG[0])
    doc_url=data.pdf_url.strip() or (matched["pdf_kz"] if meta_lang=="kk" else matched["pdf_ru"])
    doc_title=(matched["name"] if meta_lang=="kk" else matched["name_ru"])+".pdf"

    prod_name=matched["name"] if meta_lang=="kk" else matched["name_ru"]
    components=[
        {
            "type":"header",
            "parameters":[
                {
                    "type":"document",
                    "document":{
                        "link":doc_url,
                        "filename":doc_title
                    }
                }
            ]
        },
        {
            "type":"body",
            "parameters":[
                {"type":"text","text":prod_name}
            ]
        }
    ]

    try:
        res=send_whatsapp_template(
            to_phone=phone,
            template_name="commercial_offer_pdf_doc",
            language_code=meta_lang,
            components=components,
            phone_number_id=svc.settings.meta_phone_id,
            access_token=svc.settings.meta_access_token.get_secret_value() if svc.settings.meta_access_token else None
        )
        wamid=res.get("messages",[{}])[0].get("id","sent")
        svc.store.log_outreach_offer(phone,prod_name,lang,"commercial_offer_pdf_doc",doc_url,"sent",wamid)
        return {"status":"sent","wamid":wamid,"phone":phone,"pdf_url":doc_url}
    except Exception as exc:
        svc.store.log_outreach_offer(phone,prod_name,lang,"commercial_offer_pdf_doc",doc_url,"failed","",str(exc))
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
