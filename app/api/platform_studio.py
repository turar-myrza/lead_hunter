"""Authenticated Studio API for AI Model Connectors, Prompt-to-Product, Prompt-to-Audience, Call Scripts, and Telephony Settings."""

from __future__ import annotations

import base64
import html as html_lib
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.control_center import auth
from app.core.config import get_settings
from app.services.ads_store import AdsStore
from app.services.ai_engine import AIEngine, AIEngineError, build_call_script_preview
from app.services.crm_handoff import CRMHandoffService, verify_webhook_signature
from app.services.discovery.kz_multi_source import (
    KZ_SOURCES_META,
    KazakhstanMultiSourceHarvester,
    parse_bulk_registry_file,
)
from app.services.market_search import MarketSearchEngine
from app.services.platform_store import AVAILABLE_VOICES, PlatformStore
from app.services.sales_store import SalesStore
from app.services.web_scraper import SSRFBlockedError

VERSION = "2.9.1"

ALLOWED_TEMPLATE_EXTS = {".docx", ".pdf", ".html", ".htm", ".txt", ".md"}
MAX_TEMPLATE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_BULK_DATA_EXTS = {".csv", ".json"}
MAX_BULK_DATA_BYTES = 20 * 1024 * 1024  # 20 MB

router = APIRouter(prefix="/platform/api/studio", tags=["Platform Studio"])


def studio_store() -> PlatformStore:
    settings = get_settings()
    return PlatformStore(SalesStore(settings.lead_hunter_db))


def studio_ai(store: PlatformStore = Depends(studio_store)) -> AIEngine:
    return AIEngine(store, get_settings())


def studio_market() -> MarketSearchEngine:
    settings = get_settings()
    return MarketSearchEngine(SalesStore(settings.lead_hunter_db))


def studio_handoff() -> CRMHandoffService:
    settings = get_settings()
    return CRMHandoffService(SalesStore(settings.lead_hunter_db), settings)


def studio_harvester(
    store: PlatformStore = Depends(studio_store),
    market: MarketSearchEngine = Depends(studio_market),
) -> KazakhstanMultiSourceHarvester:
    return KazakhstanMultiSourceHarvester(
        market_engine=market,
        api_keys=store.get_data_source_keys(masked=False),
    )


# ------------------------------------------------------------------
# Request Models
# ------------------------------------------------------------------
class AIProviderIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider: str = Field(pattern=r"^(gemini|openai|deepseek|anthropic|local_openai|ollama|vllm)$")
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=500)
    model_name: str = Field(min_length=2, max_length=160)
    purpose: str = Field(default="all", pattern=r"^(all|web_research|research_tier|audience_generation|reasoning_tier|test_tier|testing|voice_scripting)$")
    is_active: bool = True


class ProductIn(BaseModel):
    slug: str = Field(default="", max_length=80)
    name: str = Field(min_length=2, max_length=200)
    name_ru: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=200)
    price_range: str = Field(default="", max_length=120)
    models: list[str] = Field(default_factory=list)
    specs: str = Field(default="", max_length=2000)
    certifications: str = Field(default="", max_length=500)
    target_use_case: str = Field(default="", max_length=1000)
    pdf_kz: str = Field(default="", max_length=500)
    pdf_ru: str = Field(default="", max_length=500)
    company_id: int = Field(default=0, ge=0)
    source: str = Field(default="manual", max_length=40)


class ProductPromptIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=1500)
    url: str = Field(default="", max_length=500)
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
    is_default: bool = False


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
@router.post("/connectors/models/{provider_id}/ping", dependencies=[Depends(auth)])
async def test_ai_model(provider_id: int, ai: AIEngine = Depends(studio_ai)):
    try:
        return await ai.test_provider(provider_id)
    except AIEngineError as exc:
        raise HTTPException(502, str(exc)) from None
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"network_error: {exc}") from None


@router.get("/ai-usage", dependencies=[Depends(auth)])
def get_ai_usage(limit: int = 50, store: PlatformStore = Depends(studio_store)):
    return store.get_ai_usage_summary(limit=limit)


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
        researched = await ai.research_product_from_prompt(data.prompt, lang=data.lang, url=data.url)
    except SSRFBlockedError as exc:
        raise HTTPException(400, f"ssrf_blocked: {exc}") from None
    except AIEngineError as exc:
        raise HTTPException(502, str(exc)) from None
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ai_transport_error: {exc}") from None

    company = store.get_company_profile()
    draft = {
        "name": researched.get("name_kz") or researched.get("name") or data.prompt[:80],
        "name_ru": researched.get("name_ru") or researched.get("name") or data.prompt[:80],
        "category": researched.get("category") or "",
        "price_range": researched.get("price_range_kzt") or researched.get("price_range") or "",
        "models": researched.get("models") if isinstance(researched.get("models"), list) else [],
        "specs": researched.get("specs") or "",
        "certifications": researched.get("certifications") or "",
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
        "scraped_url": researched.get("scraped_url", ""),
        "model_used": researched.get("model_used", ""),
        "latency_ms": researched.get("latency_ms", 0),
        "cost_usd": researched.get("cost_usd", 0.0),
    }


@router.get("/products/{product_id}/offer-preview", dependencies=[Depends(auth)])
def product_offer_preview(product_id: int, lang: str = "ru", store: PlatformStore = Depends(studio_store)):
    prod = store.get_product(str(product_id))
    if not prod:
        raise HTTPException(404, "product_not_found")
    comp = (
        store.get_company_entity(prod["company_id"])
        if prod.get("company_id")
        else None
    ) or store.get_company_profile()
    is_kz = (lang or "ru").lower() == "kz"
    e = html_lib.escape

    title = e(prod.get("name") if is_kz else (prod.get("name_ru") or prod.get("name") or ""))
    company_name = e(comp.get("company_name") or "ТОО «CRM Consulting»")
    bin_num = e(comp.get("bin") or "")
    city = e(comp.get("city") or "Алматы")
    website = e(comp.get("website") or "")
    mgr_name = e(comp.get("manager_name") or "")
    mgr_phone = e(comp.get("manager_phone") or "")
    price = e(prod.get("price_range") or "По запросу / Сұраныс бойынша")
    specs = e(prod.get("specs") or "")
    certs = e(prod.get("certifications") or "CE / ISO / ЕАЭС")
    models_html = "".join(f"<li>{e(str(m))}</li>" for m in (prod.get("models") or []))

    heading = "КОММЕРЦИЯЛЫҚ ҰСЫНЫС" if is_kz else "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ"
    lbl_price = "Бағасы" if is_kz else "Стоимость"
    lbl_specs = "Техникалық сипаттамасы" if is_kz else "Технические характеристики"
    lbl_certs = "Сертификаттар" if is_kz else "Сертификация"
    lbl_models = "Қолжетімді модельдер" if is_kz else "Доступные модели"
    lbl_contact = "Байланыс менеджері" if is_kz else "Контактный менеджер"

    html_doc = f"""<!doctype html>
<html lang="{e(lang)}">
<head><meta charset="utf-8"><title>{heading} — {title}</title></head>
<body style="font-family:Arial,sans-serif;max-width:760px;margin:24px auto;color:#111827;line-height:1.5;">
  <div style="border-bottom:3px solid #0284c7;padding-bottom:12px;margin-bottom:20px;">
    <h2 style="margin:0;color:#0284c7;">{company_name}</h2>
    <div style="font-size:13px;color:#4b5563;">БИН: {bin_num} · {city} · {website}</div>
  </div>
  <h1 style="font-size:20px;margin:0 0 8px 0;">{heading}</h1>
  <h2 style="font-size:18px;color:#0f172a;margin:0 0 16px 0;">{title}</h2>
  <p><strong>{lbl_price}:</strong> {price}</p>
  <p><strong>{lbl_specs}:</strong> {specs}</p>
  <p><strong>{lbl_certs}:</strong> {certs}</p>
  {"<p><strong>" + lbl_models + ":</strong></p><ul>" + models_html + "</ul>" if models_html else ""}
  <div style="margin-top:28px;padding-top:12px;border-top:1px solid #d1d5db;font-size:14px;">
    <strong>{lbl_contact}:</strong> {mgr_name} · {mgr_phone}
  </div>
</body>
</html>"""
    return {
        "product_id": prod["id"],
        "lang": "kz" if is_kz else "ru",
        "title": prod.get("name") if is_kz else (prod.get("name_ru") or prod.get("name")),
        "company_name": comp.get("company_name"),
        "html": html_doc,
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
    market: MarketSearchEngine = Depends(studio_market),
):
    product = store.get_product(data.product_key) if data.product_key else None
    try:
        result = await ai.analyze_audiences_from_prompt(data.prompt, product=product)
    except AIEngineError as exc:
        raise HTTPException(502, str(exc)) from None
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ai_transport_error: {exc}") from None

    # Enrich each proposed segment with real FTS5 seed match count
    try:
        if market.count_indexed() == 0:
            market.index_csv_seeds(Path(__file__).resolve().parents[2] / "data")
        for seg in result.get("segments") or []:
            seg["matched_seed_count"] = market.estimate_segment_volume(
                seg.get("keywords") or [],
                seg.get("regions") or [],
            )
    except Exception:
        pass
    return result


@router.get("/audiences/search-seeds", dependencies=[Depends(auth)])
def search_audience_seeds(
    q: str = "",
    region: str = "",
    min_budget: float = 0.0,
    limit: int = 50,
    market: MarketSearchEngine = Depends(studio_market),
):
    if market.count_indexed() == 0:
        market.index_csv_seeds(Path(__file__).resolve().parents[2] / "data")
    return market.search(query=q, region=region, min_budget_kzt=min_budget, limit=limit)


@router.post("/audiences/reindex", dependencies=[Depends(auth)])
def reindex_audience_seeds(market: MarketSearchEngine = Depends(studio_market)):
    count = market.index_csv_seeds(Path(__file__).resolve().parents[2] / "data")
    return {"indexed": count, "total_indexed": market.count_indexed()}


@router.post("/audiences/apply", dependencies=[Depends(auth)])
def apply_analyzed_audiences(data: ApplyAudiencesIn, store: PlatformStore = Depends(studio_store)):
    ads_store = AdsStore(store.base)
    created = []
    valid_regions = {"all", "almaty", "astana", "shymkent", "karaganda", "aktobe", "atyrau"}
    for seg in data.segments:
        segment_type = str(seg.get("segment") or "b2b").lower()
        if segment_type not in {"b2b", "b2g", "b2c"}:
            segment_type = "b2b"
        raw_regions = seg.get("regions") or ["all"]
        regions = [r for r in raw_regions if r in valid_regions] or ["all"]
        keywords = [str(k).strip() for k in (seg.get("keywords") or []) if str(k).strip()][:15]
        rules = {
            "regions": regions,
            "keywords": keywords[:10],
            "sources": (
                ["csv_seed", "uchet_pk", "e_ondiris", "goszakup"]
                if segment_type == "b2g"
                else (["manual_import"] if segment_type == "b2c" else ["csv_seed", "satu_kz", "uchet_pk"])
            ),
            "require_phone": True,
            "year": 2026,
        }
        try:
            aud = ads_store.create_audience(
                campaign_id=data.campaign_id,
                name=str(seg.get("name") or "AI Segment")[:140],
                segment=segment_type,
                rules=rules,
                auto_discovery=True,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
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


# ------------------------------------------------------------------
# 6. End-to-End Autonomous CRM Handoff & Pipeline Analytics
# ------------------------------------------------------------------
class CallOutcomeIn(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    outcome: str = Field(pattern=r"^(qualified|callback|rejected|no_answer)$")
    lead_id: int = 0
    company: str = Field(default="", max_length=200)
    director: str = Field(default="", max_length=120)
    product_key: str = Field(default="", max_length=120)
    transcript: str = Field(default="", max_length=5000)
    recording_url: str = Field(default="", max_length=500)
    opportunity_kzt: float = Field(default=0.0, ge=0.0)


@router.post("/webhooks/call-outcome")
async def webhook_call_outcome(
    request: Request,
    handoff: CRMHandoffService = Depends(studio_handoff),
):
    """Cryptographically verified webhook receiving PBX call outcomes and creating Bitrix24 Deals."""
    raw_body = await request.body()
    sig = request.headers.get("X-LeadHunter-Signature", "")
    secret = handoff.settings.lead_hunter_api_token.get_secret_value() if handoff.settings.lead_hunter_api_token else ""
    if not verify_webhook_signature(raw_body, sig, secret):
        raise HTTPException(401, "invalid_webhook_signature")

    try:
        payload = CallOutcomeIn.model_validate_json(raw_body)
    except Exception as exc:
        raise HTTPException(400, f"invalid_payload: {exc}") from None

    return await handoff.process_call_outcome(
        phone=payload.phone,
        outcome=payload.outcome,
        lead_id=payload.lead_id,
        company=payload.company,
        director=payload.director,
        product_key=payload.product_key,
        transcript=payload.transcript,
        recording_url=payload.recording_url,
        opportunity_kzt=payload.opportunity_kzt,
    )


@router.post("/leads/{lead_id}/handoff", dependencies=[Depends(auth)])
async def manual_lead_handoff(
    lead_id: int,
    handoff: CRMHandoffService = Depends(studio_handoff),
):
    astore = AdsStore(handoff.base)
    lead = astore.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead_not_found")
    return await handoff.process_call_outcome(
        phone=lead.get("phone_e164") or lead.get("phone_raw") or "",
        outcome="qualified",
        lead_id=lead_id,
        company=lead.get("company") or "",
        director=lead.get("director") or "",
        product_key=lead.get("service") or lead.get("product_key") or "",
        transcript=f"Manual CRM handoff triggered for {lead.get('company', '')} (ICP Score: {lead.get('icp_score', 0)}).",
        opportunity_kzt=float(lead.get("contract_sum_kzt") or 0.0),
    )


@router.get("/analytics", dependencies=[Depends(auth)])
def get_pipeline_analytics(handoff: CRMHandoffService = Depends(studio_handoff)):
    return handoff.get_pipeline_analytics()


# ------------------------------------------------------------------
# 7. Multi-Company Entities (Advertise from Multiple Legal Entities)
# ------------------------------------------------------------------
@router.get("/companies", dependencies=[Depends(auth)])
def list_company_entities(store: PlatformStore = Depends(studio_store)):
    return {"items": store.list_company_entities()}


@router.post("/companies", dependencies=[Depends(auth)])
def create_company_entity(data: CompanyProfileIn, store: PlatformStore = Depends(studio_store)):
    return store.save_company_entity(data.model_dump())


@router.put("/companies/{entity_id}", dependencies=[Depends(auth)])
def update_company_entity(entity_id: int, data: CompanyProfileIn, store: PlatformStore = Depends(studio_store)):
    try:
        return store.save_company_entity(data.model_dump(), entity_id=entity_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.post("/companies/{entity_id}/activate", dependencies=[Depends(auth)])
def activate_company_entity(entity_id: int, store: PlatformStore = Depends(studio_store)):
    try:
        return store.set_default_company_entity(entity_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.delete("/companies/{entity_id}", dependencies=[Depends(auth)])
def delete_company_entity(entity_id: int, store: PlatformStore = Depends(studio_store)):
    try:
        if not store.delete_company_entity(entity_id):
            raise HTTPException(404, "company_entity_not_found")
        return {"deleted": True, "id": entity_id}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# ------------------------------------------------------------------
# 8. Document Templates Library (Offer, Inquiry, Contract, etc.)
# ------------------------------------------------------------------
def _templates_dir(store: PlatformStore) -> Path:
    base_dir = Path(store.base.path).parent / "templates"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


@router.get("/templates", dependencies=[Depends(auth)])
def list_document_templates(doc_type: str = "", store: PlatformStore = Depends(studio_store)):
    return {"items": store.list_document_templates(doc_type=doc_type)}


@router.post("/templates/upload", dependencies=[Depends(auth)])
async def upload_document_template(
    name: str = Form(..., min_length=2, max_length=160),
    doc_type: str = Form(default="offer"),
    lang: str = Form(default="ru"),
    company_id: int = Form(default=0),
    is_active: bool = Form(default=False),
    content_text: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None),
    store: PlatformStore = Depends(studio_store),
):
    norm_type = doc_type.strip().lower()
    if norm_type not in {"offer", "inquiry", "contract", "presentation", "other"}:
        raise HTTPException(400, "invalid_doc_type")
    norm_lang = "kz" if lang.strip().lower() == "kz" else "ru"

    saved_file_name = ""
    saved_file_path = ""
    saved_mime = ""
    extracted_text = (content_text or "").strip()

    if file is not None and file.filename:
        orig_name = Path(file.filename).name
        ext = Path(orig_name).suffix.lower()
        if ext not in ALLOWED_TEMPLATE_EXTS:
            raise HTTPException(400, f"unsupported_template_extension:{ext}")
        raw_bytes = await file.read()
        if len(raw_bytes) > MAX_TEMPLATE_BYTES:
            raise HTTPException(413, "template_file_too_large")

        safe_base = re.sub(r"[^A-Za-z0-9._\-]", "_", orig_name)[:120]
        tdir = _templates_dir(store)
        dest = (tdir / f"{uuid.uuid4().hex[:12]}_{safe_base}").resolve()
        if not dest.is_relative_to(tdir.resolve()):
            raise HTTPException(400, "invalid_template_path")
        dest.write_bytes(raw_bytes)
        saved_file_name = orig_name
        saved_file_path = str(dest)
        saved_mime = file.content_type or "application/octet-stream"
        if not extracted_text and ext in {".html", ".htm", ".txt", ".md"}:
            extracted_text = raw_bytes.decode("utf-8", errors="replace")[:30000]

    return store.save_document_template({
        "name": name,
        "doc_type": norm_type,
        "lang": norm_lang,
        "company_id": company_id,
        "file_name": saved_file_name,
        "file_path": saved_file_path,
        "mime_type": saved_mime,
        "content_text": extracted_text,
        "is_active": is_active,
    })


@router.post("/templates/{template_id}/activate", dependencies=[Depends(auth)])
def activate_document_template(template_id: int, store: PlatformStore = Depends(studio_store)):
    try:
        return store.set_active_document_template(template_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/templates/{template_id}/download", dependencies=[Depends(auth)])
def download_document_template(template_id: int, store: PlatformStore = Depends(studio_store)):
    tpl = store.get_document_template(template_id)
    if not tpl or not tpl.get("file_path"):
        raise HTTPException(404, "template_file_not_found")
    tdir = _templates_dir(store).resolve()
    target = Path(tpl["file_path"]).resolve()
    if not target.is_relative_to(tdir) or not target.exists():
        raise HTTPException(404, "template_file_missing")
    return FileResponse(target, filename=tpl.get("file_name") or target.name)


@router.delete("/templates/{template_id}", dependencies=[Depends(auth)])
def delete_document_template(template_id: int, store: PlatformStore = Depends(studio_store)):
    tpl = store.get_document_template(template_id)
    if not tpl:
        raise HTTPException(404, "template_not_found")
    if tpl.get("file_path"):
        tdir = _templates_dir(store).resolve()
        target = Path(tpl["file_path"]).resolve()
        if target.is_relative_to(tdir) and target.exists():
            try:
                target.unlink()
            except OSError:
                pass
    store.delete_document_template(template_id)
    return {"deleted": True, "id": template_id}


# ------------------------------------------------------------------
# 9. Simultaneous Kazakhstan Multi-Source Harvester & BIN Enricher
# ------------------------------------------------------------------
class DataSourceKeysIn(BaseModel):
    twogis_api_key: Optional[str] = None
    egov_api_key: Optional[str] = None
    statsnet_api_key: Optional[str] = None
    kompra_api_key: Optional[str] = None


class MultiSourceHarvestIn(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["satu_kz", "uchet_pk", "csv_seed"], min_length=1, max_length=9)
    keywords: list[str] = Field(default_factory=list, max_length=10)
    regions: list[str] = Field(default_factory=lambda: ["all"], min_length=1, max_length=7)
    require_phone: bool = False
    auto_enrich_bin: bool = True
    index_to_fts: bool = True
    limit: int = Field(default=30, ge=1, le=100)
    save_to_campaign_id: int = Field(default=0, ge=0)
    save_to_audience_id: int = Field(default=0, ge=0)


class EnrichBinIn(BaseModel):
    bin: str = Field(default="", max_length=24)
    company: str = Field(default="", max_length=200)
    lead_id: int = Field(default=0, ge=0)


@router.get("/discovery/sources", dependencies=[Depends(auth)])
def list_kz_discovery_sources(
    store: PlatformStore = Depends(studio_store),
    market: MarketSearchEngine = Depends(studio_market),
):
    keys_masked = store.get_data_source_keys(masked=True)
    raw_keys = store.get_data_source_keys(masked=False)
    items = []
    for meta in KZ_SOURCES_META:
        key_field = meta.get("key_field") or ""
        configured = True if not meta["requires_key"] else bool(raw_keys.get(key_field))
        items.append({
            **meta,
            "configured": configured,
            "status_label": "Ready (No Key Needed)" if not meta["requires_key"] else ("API Key Active" if configured else "Optional API Key"),
        })
    return {
        "sources": items,
        "keys": keys_masked,
        "total_fts_indexed": market.count_indexed(),
    }


@router.put("/discovery/keys", dependencies=[Depends(auth)])
def update_kz_discovery_keys(
    data: DataSourceKeysIn,
    store: PlatformStore = Depends(studio_store),
):
    return store.save_data_source_keys(data.model_dump(exclude_none=True))


@router.post("/discovery/harvest", dependencies=[Depends(auth)])
async def run_simultaneous_kz_harvest(
    data: MultiSourceHarvestIn,
    store: PlatformStore = Depends(studio_store),
    harvester: KazakhstanMultiSourceHarvester = Depends(studio_harvester),
):
    """Run all selected Kazakhstan data sources concurrently, deduplicate, enrich BIN/Director, and optionally save to Campaign."""
    result = await harvester.discover_simultaneous(
        sources=data.sources,
        keywords=data.keywords,
        regions=data.regions,
        require_phone=data.require_phone,
        auto_enrich_bin=data.auto_enrich_bin,
        index_to_fts=data.index_to_fts,
        limit=data.limit,
    )

    saved_to_ads = None
    if data.save_to_campaign_id > 0 and result.get("items"):
        astore = AdsStore(store.base)
        camp = astore.get_campaign(data.save_to_campaign_id)
        if not camp:
            raise HTTPException(404, "campaign_not_found")
        aud_id = data.save_to_audience_id
        if aud_id <= 0:
            existing_auds = astore.list_audiences([camp["id"]])
            if existing_auds:
                aud_id = existing_auds[0]["id"]
            else:
                new_aud = astore.create_audience(
                    campaign_id=camp["id"],
                    name="KZ Multi-Source Harvest",
                    segment="b2b",
                    rules={
                        "regions": [r for r in data.regions if r in {"almaty", "astana", "shymkent", "karaganda", "aktobe", "atyrau", "all"}] or ["all"],
                        "keywords": data.keywords[:10],
                        "sources": [s for s in data.sources if s in {"csv_seed", "goszakup", "manual_import", "satu_kz", "uchet_pk", "twogis", "egov_open_data", "statsnet", "e_ondiris"}] or ["satu_kz", "uchet_pk"],
                        "require_phone": data.require_phone,
                    },
                    auto_discovery=False,
                )
                aud_id = new_aud["id"]

        lead_rows = [
            {
                "campaign_id": camp["id"],
                "audience_id": aud_id,
                "company": r.get("company") or "",
                "bin": r.get("bin") or "",
                "city": r.get("city") or "",
                "category": r.get("category") or "",
                "service": camp.get("service") or camp.get("product_key") or "",
                "director": r.get("director") or "",
                "phone_raw": r.get("phone_raw") or "",
                "phone_e164": r.get("phone_e164") or "",
                "email": r.get("email") or "",
                "contract_sum_kzt": r.get("contract_sum_kzt"),
                "source": r.get("source") or "kz_multi",
                "source_url": r.get("source_url") or "",
                "icp_score": int(r.get("icp_score") or 50),
                "whitelisted": False,
            }
            for r in result["items"]
        ]
        counts = astore.upsert_leads(lead_rows)
        saved_to_ads = {"campaign_id": camp["id"], "audience_id": aud_id, **counts}

    result["saved_to_ads"] = saved_to_ads
    return result


@router.post("/discovery/enrich-bin", dependencies=[Depends(auth)])
async def enrich_company_bin(
    data: EnrichBinIn,
    store: PlatformStore = Depends(studio_store),
    harvester: KazakhstanMultiSourceHarvester = Depends(studio_harvester),
):
    """Instant concurrent BIN & legal enrichment via PK.Uchet.kz, data.egov.kz, Statsnet, and FTS5."""
    try:
        dossier = await harvester.enrich_by_bin(bin_code=data.bin, company_name=data.company)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    updated_lead = None
    if data.lead_id > 0 and dossier.get("found"):
        astore = AdsStore(store.base)
        updated_lead = astore.update_lead_enrichment(
            data.lead_id,
            bin_val=dossier.get("bin") or "",
            director=dossier.get("director") or "",
            city=dossier.get("city") or "",
            phone_raw=dossier.get("phone_raw") or "",
            phone_e164=dossier.get("phone_e164") or "",
            email=dossier.get("email") or "",
            source_append="uchet_pk",
            icp_bonus=15 if dossier.get("bin") else 5,
        )
    dossier["updated_lead"] = updated_lead
    return dossier


@router.post("/discovery/import-bulk", dependencies=[Depends(auth)])
async def import_bulk_kz_registry(
    source_tag: str = Form(default="egov_bulk"),
    file: UploadFile = File(...),
    market: MarketSearchEngine = Depends(studio_market),
):
    """Upload a bulk Open Data CSV or JSON registry (data.egov.kz / e-Ondiris KTP / custom) directly into SQLite FTS5."""
    orig_name = Path(file.filename or "dataset.csv").name
    ext = Path(orig_name).suffix.lower()
    if ext not in ALLOWED_BULK_DATA_EXTS:
        raise HTTPException(400, f"unsupported_bulk_extension:{ext}")
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_BULK_DATA_BYTES:
        raise HTTPException(413, "bulk_file_too_large")

    clean_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", (source_tag or "egov_bulk").strip())[:40] or "egov_bulk"
    try:
        records = parse_bulk_registry_file(raw_bytes, orig_name, source_tag=clean_tag)
    except Exception as exc:
        raise HTTPException(400, f"invalid_registry_file:{exc}") from None

    inserted = market.upsert_seeds(records)
    return {
        "filename": orig_name,
        "source_tag": clean_tag,
        "parsed_rows": len(records),
        "newly_indexed": inserted,
        "total_fts_indexed": market.count_indexed(),
    }



