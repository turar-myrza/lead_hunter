"""Authenticated Studio API for AI Model Connectors, Prompt-to-Product, Prompt-to-Audience, Call Scripts, and Telephony Settings."""

from __future__ import annotations

import base64
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.control_center import auth
from app.core.config import get_settings
from app.services.ads_store import AdsStore
from app.services.ai_engine import AIEngine, AIEngineError, build_call_script_preview
from app.services.platform_store import AVAILABLE_VOICES, PlatformStore
from app.services.sales_store import SalesStore

VERSION = "2.0.0"

router = APIRouter(prefix="/platform/api/studio", tags=["Platform Studio"])


def studio_store() -> PlatformStore:
    settings = get_settings()
    return PlatformStore(SalesStore(settings.lead_hunter_db))


def studio_ai(store: PlatformStore = Depends(studio_store)) -> AIEngine:
    return AIEngine(store, get_settings())


# ------------------------------------------------------------------
# Request Models
# ------------------------------------------------------------------
class AIProviderIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider: str = Field(pattern=r"^(gemini|openai|deepseek|anthropic|local_openai|ollama|vllm)$")
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=500)
    model_name: str = Field(min_length=2, max_length=160)
    purpose: str = Field(default="all", pattern=r"^(all|web_research|audience_generation|voice_scripting)$")
    is_active: bool = True


class ProductIn(BaseModel):
    slug: str = Field(default="", max_length=80)
    name: str = Field(min_length=2, max_length=200)
    name_ru: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=200)
    price_range: str = Field(default="", max_length=120)
    models: list[str] = Field(default_factory=list)
    specs: str = Field(default="", max_length=2000)
    target_use_case: str = Field(default="", max_length=1000)
    pdf_kz: str = Field(default="", max_length=500)
    pdf_ru: str = Field(default="", max_length=500)
    source: str = Field(default="manual", max_length=40)


class ProductPromptIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=1500)
    lang: str = Field(default="ru", pattern=r"^(kz|ru)$")
    auto_save: bool = False


class AudiencePromptIn(BaseModel):
    prompt: str = Field(default="", max_length=2000)
    product_key: str = Field(default="", max_length=120)


class ApplyAudiencesIn(BaseModel):
    campaign_id: int
    segments: list[dict[str, Any]] = Field(min_length=1, max_length=10)


class CompanyProfileIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    bin: str = Field(default="", max_length=24)
    city: str = Field(default="", max_length=80)
    website: str = Field(default="", max_length=300)
    manager_name: str = Field(default="", max_length=120)
    manager_phone: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=1000)
    default_pdf_kz: str = Field(default="", max_length=500)
    default_pdf_ru: str = Field(default="", max_length=500)


class TelephonyConfigIn(BaseModel):
    concurrent_calls: int = Field(default=1, ge=1, le=20)
    daily_call_cap: int = Field(default=200, ge=1, le=5000)
    work_hours: str = Field(default="09:00-19:00 Asia/Almaty", max_length=80)
    transfer_extension: str = Field(default="055", max_length=32)
    voice_name: str = Field(default="Kore", max_length=40)
    default_lang: str = Field(default="kz", pattern=r"^(kz|ru)$")
    custom_greeting_kz: str = Field(default="", max_length=1000)
    custom_greeting_ru: str = Field(default="", max_length=1000)
    custom_prompt_kz: str = Field(default="", max_length=3000)
    custom_prompt_ru: str = Field(default="", max_length=3000)


class CrmConnectorsIn(BaseModel):
    bitrix24_webhook: Optional[str] = None
    moysklad_token: Optional[str] = None
    onec_odata_url: Optional[str] = None
    cms_feed_url: Optional[str] = None


class ScriptPreviewIn(BaseModel):
    product_key: str = Field(default="", max_length=160)
    lang: str = Field(default="kz", pattern=r"^(kz|ru)$")
    custom_greeting: str = Field(default="", max_length=1000)
    custom_prompt: str = Field(default="", max_length=3000)
    ceo: str = Field(default="", max_length=120)
    biz: str = Field(default="", max_length=160)


class VoicePreviewIn(BaseModel):
    voice_name: str = Field(default="Kore", max_length=40)
    lang: str = Field(default="kz", pattern=r"^(kz|ru)$")
    text: str = Field(default="", max_length=600)


class SyncCrmIn(BaseModel):
    source: str = Field(pattern=r"^(bitrix24|moysklad)$")
    limit: int = Field(default=20, ge=1, le=100)


# ------------------------------------------------------------------
# 1. AI Model & Token Connectors
# ------------------------------------------------------------------
@router.get("/connectors/models", dependencies=[Depends(auth)])
def list_ai_models(store: PlatformStore = Depends(studio_store)):
    settings = get_settings()
    return {
        "items": store.list_ai_providers(include_secrets=False),
        "env_gemini_configured": bool(settings.control_gemini_key),
        "env_gemini_model": settings.control_ai_model,
    }


@router.post("/connectors/models", dependencies=[Depends(auth)])
def create_ai_model(data: AIProviderIn, store: PlatformStore = Depends(studio_store)):
    return store.save_ai_provider(data.model_dump())


@router.put("/connectors/models/{provider_id}", dependencies=[Depends(auth)])
def update_ai_model(provider_id: int, data: AIProviderIn, store: PlatformStore = Depends(studio_store)):
    try:
        return store.save_ai_provider(data.model_dump(), provider_id=provider_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.delete("/connectors/models/{provider_id}", dependencies=[Depends(auth)])
def delete_ai_model(provider_id: int, store: PlatformStore = Depends(studio_store)):
    if not store.delete_ai_provider(provider_id):
        raise HTTPException(404, "ai_provider_not_found")
    return {"deleted": True, "id": provider_id}


@router.post("/connectors/models/{provider_id}/test", dependencies=[Depends(auth)])
async def test_ai_model(provider_id: int, ai: AIEngine = Depends(studio_ai)):
    try:
        return await ai.test_provider(provider_id)
    except AIEngineError as exc:
        raise HTTPException(502, str(exc)) from None
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"network_error: {exc}") from None


# ------------------------------------------------------------------
# 2. Dynamic Product Catalog & Prompt-to-Product
# ------------------------------------------------------------------
@router.get("/products", dependencies=[Depends(auth)])
def list_studio_products(store: PlatformStore = Depends(studio_store)):
    return {"items": store.list_products()}


@router.post("/products", dependencies=[Depends(auth)])
def create_studio_product(data: ProductIn, store: PlatformStore = Depends(studio_store)):
    return store.save_product(data.model_dump())


@router.put("/products/{product_id}", dependencies=[Depends(auth)])
def update_studio_product(product_id: int, data: ProductIn, store: PlatformStore = Depends(studio_store)):
    try:
        return store.save_product(data.model_dump(), product_id=product_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.delete("/products/{product_id}", dependencies=[Depends(auth)])
def delete_studio_product(product_id: int, store: PlatformStore = Depends(studio_store)):
    if not store.delete_product(product_id):
        raise HTTPException(404, "product_not_found")
    return {"deleted": True, "id": product_id}


@router.post("/products/from-prompt", dependencies=[Depends(auth)])
async def product_from_prompt(
    data: ProductPromptIn,
    store: PlatformStore = Depends(studio_store),
    ai: AIEngine = Depends(studio_ai),
):
    try:
        researched = await ai.research_product_from_prompt(data.prompt, lang=data.lang)
    except AIEngineError as exc:
        raise HTTPException(502, str(exc)) from None
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ai_transport_error: {exc}") from None

    company = store.get_company_profile()
    draft = {
        "name": researched.get("name") or data.prompt[:80],
        "name_ru": researched.get("name_ru") or researched.get("name") or data.prompt[:80],
        "category": researched.get("category") or "",
        "price_range": researched.get("price_range") or "",
        "models": researched.get("models") if isinstance(researched.get("models"), list) else [],
        "specs": researched.get("specs") or "",
        "target_use_case": researched.get("target_use_case") or "",
        "pdf_kz": company.get("default_pdf_kz", ""),
        "pdf_ru": company.get("default_pdf_ru", ""),
        "source": "ai_prompt",
    }
    saved = store.save_product(draft) if data.auto_save else None
    return {
        "draft": draft,
        "saved": saved,
        "sources": researched.get("sources", []),
        "model_used": researched.get("model_used", ""),
    }


@router.post("/products/sync-crm", dependencies=[Depends(auth)])
async def sync_products_from_crm(data: SyncCrmIn, store: PlatformStore = Depends(studio_store)):
    cfg = store.get_crm_connectors(masked=False)
    imported = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        if data.source == "bitrix24":
            webhook = (cfg.get("bitrix24_webhook") or "").strip().rstrip("/")
            if not webhook:
                settings = get_settings()
                if settings.bitrix24_webhook_url:
                    webhook = settings.bitrix24_webhook_url.get_secret_value().strip().rstrip("/")
            if not webhook:
                raise HTTPException(400, "Bitrix24 webhook URL is not configured in Connectors.")
            resp = await client.get(f"{webhook}/crm.product.list.json", params={"start": 0})
            if resp.status_code != 200:
                raise HTTPException(502, f"Bitrix24 HTTP {resp.status_code}")
            rows = (resp.json().get("result") or [])[: data.limit]
            for row in rows:
                name = (row.get("NAME") or "").strip()
                if not name:
                    continue
                price = row.get("PRICE")
                curr = row.get("CURRENCY_ID") or "KZT"
                saved = store.save_product(
                    {
                        "name": name,
                        "name_ru": name,
                        "category": "Bitrix24 Catalog",
                        "price_range": f"{price} {curr}" if price else "",
                        "specs": (row.get("DESCRIPTION") or "")[:500],
                        "source": "bitrix24",
                    }
                )
                imported.append(saved)
        elif data.source == "moysklad":
            token = (cfg.get("moysklad_token") or "").strip()
            if not token:
                raise HTTPException(400, "MoySklad Bearer token is not configured in Connectors.")
            resp = await client.get(
                "https://api.moysklad.ru/api/remap/1.2/entity/product",
                headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
                params={"limit": data.limit},
            )
            if resp.status_code != 200:
                raise HTTPException(502, f"MoySklad HTTP {resp.status_code}")
            rows = (resp.json().get("rows") or [])[: data.limit]
            for row in rows:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                sale_prices = row.get("salePrices") or []
                price_val = sale_prices[0].get("value", 0) / 100.0 if sale_prices else 0
                saved = store.save_product(
                    {
                        "name": name,
                        "name_ru": name,
                        "category": row.get("pathName") or "MoySklad Catalog",
                        "price_range": f"{int(price_val):,} ₸".replace(",", " ") if price_val else "",
                        "specs": (row.get("description") or "")[:500],
                        "source": "moysklad",
                    }
                )
                imported.append(saved)
    return {"imported_count": len(imported), "items": imported}


# ------------------------------------------------------------------
# 3. Prompt-to-Audience Analyzer
# ------------------------------------------------------------------
@router.post("/audiences/analyze-prompt", dependencies=[Depends(auth)])
async def analyze_audiences_prompt(
    data: AudiencePromptIn,
    store: PlatformStore = Depends(studio_store),
    ai: AIEngine = Depends(studio_ai),
):
    product = store.get_product(data.product_key) if data.product_key else None
    try:
        result = await ai.analyze_audiences_from_prompt(data.prompt, product=product)
    except AIEngineError as exc:
        raise HTTPException(502, str(exc)) from None
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ai_transport_error: {exc}") from None
    return result


@router.post("/audiences/apply", dependencies=[Depends(auth)])
def apply_analyzed_audiences(data: ApplyAudiencesIn, store: PlatformStore = Depends(studio_store)):
    ads_store = AdsStore(store.base)
    created = []
    valid_regions = {"all", "almaty", "astana", "shymkent"}
    for seg in data.segments:
        segment_type = str(seg.get("segment") or "b2b").lower()
        if segment_type not in {"b2b", "b2g", "b2c"}:
            segment_type = "b2b"
        raw_regions = seg.get("regions") or ["all"]
        regions = [r for r in raw_regions if r in valid_regions] or ["all"]
        keywords = [str(k).strip() for k in (seg.get("keywords") or []) if str(k).strip()][:15]
        rules = {
            "regions": regions,
            "keywords": keywords,
            "sources": ["csv_seed", "goszakup"] if segment_type == "b2g" else ["csv_seed"],
            "mobile_only": False,
            "has_director": False,
            "min_icp_score": 0,
            "min_sum_kzt": None,
            "max_leads": 200,
        }
        aud = ads_store.create_audience(
            campaign_id=data.campaign_id,
            name=str(seg.get("name") or "AI Segment")[:140],
            segment=segment_type,
            rules=rules,
            auto_discovery=True,
        )
        created.append(aud)
    return {"created_count": len(created), "items": created}


# ------------------------------------------------------------------
# 4. Settings: Company Profile, Telephony, Connectors & Script Preview
# ------------------------------------------------------------------
@router.get("/settings", dependencies=[Depends(auth)])
def get_studio_settings(store: PlatformStore = Depends(studio_store)):
    return {
        "company": store.get_company_profile(),
        "telephony": store.get_telephony_config(),
        "connectors": store.get_crm_connectors(masked=True),
        "voices": AVAILABLE_VOICES,
    }


@router.put("/settings/company", dependencies=[Depends(auth)])
def update_company_profile(data: CompanyProfileIn, store: PlatformStore = Depends(studio_store)):
    return store.save_company_profile(data.model_dump())


@router.put("/settings/telephony", dependencies=[Depends(auth)])
def update_telephony_config(data: TelephonyConfigIn, store: PlatformStore = Depends(studio_store)):
    return store.save_telephony_config(data.model_dump())


@router.put("/settings/connectors", dependencies=[Depends(auth)])
def update_crm_connectors(data: CrmConnectorsIn, store: PlatformStore = Depends(studio_store)):
    return store.save_crm_connectors(data.model_dump(exclude_none=True))


@router.post("/script/preview", dependencies=[Depends(auth)])
def preview_call_script(data: ScriptPreviewIn, store: PlatformStore = Depends(studio_store)):
    products = store.list_products()
    product = store.get_product(data.product_key) if data.product_key else (products[0] if products else {"name": "Өнім", "name_ru": "Продукт"})
    if not product:
        product = {"name": data.product_key or "Өнім", "name_ru": data.product_key or "Продукт", "specs": "", "price_range": ""}
    company = store.get_company_profile()
    telephony = store.get_telephony_config()
    greeting = data.custom_greeting or (
        telephony.get("custom_greeting_kz", "") if data.lang == "kz" else telephony.get("custom_greeting_ru", "")
    )
    prompt = data.custom_prompt or (
        telephony.get("custom_prompt_kz", "") if data.lang == "kz" else telephony.get("custom_prompt_ru", "")
    )
    return build_call_script_preview(
        product=product,
        company=company,
        telephony=telephony,
        lang=data.lang,
        custom_greeting=greeting,
        custom_prompt=prompt,
        ceo=data.ceo,
        biz=data.biz,
    )


@router.post("/voice/preview", dependencies=[Depends(auth)])
async def preview_voice_audio(data: VoicePreviewIn, store: PlatformStore = Depends(studio_store)):
    """Generate a playable audio sample using Gemini TTS if available, or return clean browser TTS fallback."""
    company = store.get_company_profile()
    seller = company.get("company_name") or "CRM Consulting"
    sample_text = (data.text or "").strip()
    if not sample_text:
        if data.lang == "kz":
            sample_text = f"Қайырлы күн! Менің есімім Айнұр, {seller} компаниясынан хабарласып тұрмын. Сіздерге тиімді ұсынысымыз бар."
        else:
            sample_text = f"Добрый день! Меня зовут Айнур, компания {seller}. Звоню вам с выгодным коммерческим предложением."

    settings = get_settings()
    api_key = ""
    for p in store.list_ai_providers(include_secrets=True):
        if p.get("is_active") and p.get("provider") == "gemini" and p.get("api_key"):
            api_key = p["api_key"]
            break
    if not api_key and settings.control_gemini_key:
        api_key = settings.control_gemini_key.get_secret_value()

    if api_key:
        try:
            transport = httpx.AsyncHTTPTransport(proxy=settings.control_ai_proxy or None, trust_env=False)
            async with httpx.AsyncClient(transport=transport, timeout=25.0) as client:
                resp = await client.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent",
                    headers={"x-goog-api-key": api_key},
                    json={
                        "contents": [{"parts": [{"text": sample_text}]}],
                        "generationConfig": {
                            "responseModalities": ["AUDIO"],
                            "speechConfig": {
                                "voiceConfig": {
                                    "prebuiltVoiceConfig": {"voiceName": data.voice_name or "Kore"}
                                }
                            },
                        },
                    },
                )
            if resp.status_code == 200:
                body = resp.json()
                inline = body["candidates"][0]["content"]["parts"][0].get("inlineData") or {}
                if inline.get("data"):
                    return {
                        "mode": "gemini_audio",
                        "mime_type": inline.get("mimeType") or "audio/L16;rate=24000",
                        "audio_base64": inline["data"],
                        "text": sample_text,
                        "voice_name": data.voice_name,
                        "lang": data.lang,
                    }
        except Exception:
            pass

    return {
        "mode": "browser_tts",
        "text": sample_text,
        "voice_name": data.voice_name,
        "lang": "kk-KZ" if data.lang == "kz" else "ru-RU",
    }
