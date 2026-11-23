"""Authenticated API endpoints for the Meta-Ads-Manager-style Lead Hunter workflow."""

import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.control_center import auth
from app.core.config import get_settings
from app.models.ads_manager import AudienceIn, CampaignIn, KATO_MAP, QueueIn, ToggleIn
from app.services.ads_manager import AdsManager, ALMATY_TZ, is_dial_window
from app.services.ads_store import AdsStore
from app.services.catalog import CATALOG
from app.services.platform_store import PlatformStore
from app.services.sales_store import SalesStore, now
from version import VERSION as APP_VERSION

VERSION = "2.4.0"

router = APIRouter(prefix="/platform/api/ads", tags=["Ads manager"])


def ads_service() -> AdsManager:
    settings = get_settings()
    return AdsManager(AdsStore(SalesStore(settings.lead_hunter_db)), settings)


def _invoke(call):
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def _parse_ids(raw: Optional[str]) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise HTTPException(400, f"Invalid numeric id '{part}'") from None
    return ids


@router.get("/meta", dependencies=[Depends(auth)])
def get_meta(mgr: AdsManager = Depends(ads_service)):
    whitelist = mgr.fetch_whitelist()
    raw_src = mgr.store.get_setting("am_source_status")
    source_status = {}
    if raw_src:
        try:
            source_status = json.loads(raw_src)
        except json.JSONDecodeError:
            source_status = {}
    today_prefix = now().astimezone(ALMATY_TZ).date().isoformat()
    pstore = PlatformStore(mgr.store.base)
    dyn_catalog = [
        {**p, "id": p["slug"]} for p in pstore.list_products()
    ] or CATALOG
    return {
        "version": APP_VERSION,
        "catalog": dyn_catalog,
        "regions": [
            {"id": "all", "label": "All KZ"},
            {"id": "almaty", "label": "Almaty", "kato": KATO_MAP["almaty"]},
            {"id": "astana", "label": "Astana", "kato": KATO_MAP["astana"]},
            {"id": "shymkent", "label": "Shymkent", "kato": KATO_MAP["shymkent"]},
        ],
        "segments": [
            {"id": "b2b", "label": "B2B Commercial", "desc": "Private clinics, distributors, companies"},
            {"id": "b2g", "label": "B2G Tenders", "desc": "Goszakup procurement lots & public orgs"},
            {"id": "b2c", "label": "B2C Inbound / Manual", "desc": "Permissioned or manually imported CSV lists"},
        ],
        "sources": [
            {"id": "csv_seed", "label": "Verified CSV Seeds (2026)"},
            {"id": "goszakup", "label": "Goszakup Live Registry"},
            {"id": "manual_import", "label": "Manual CSV Import"},
        ],
        "objectives": [
            {"id": "call_then_offer", "label": "AI Call -> WhatsApp PDF on verbal consent"},
            {"id": "offer_then_call", "label": "Send WhatsApp PDF first -> AI Follow-up Call"},
            {"id": "call_only", "label": "AI Voice SDR Call only"},
        ],
        "queue": mgr.store.queue_snapshot(50),
        "whitelist_count": len(whitelist),
        "dial_window_open": is_dial_window(mgr.clock()),
        "daily_call_cap": mgr.settings.ads_daily_call_cap,
        "calls_today": mgr.store.count_calls_today(today_prefix),
        "source_status": source_status,
    }


@router.get("/campaigns", dependencies=[Depends(auth)])
def list_campaigns(mgr: AdsManager = Depends(ads_service)):
    return {"items": mgr.store.list_campaigns()}


@router.post("/campaigns", dependencies=[Depends(auth)])
def create_campaign(data: CampaignIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.create_campaign(data.name, data.product_key, data.objective, data.lang))


@router.put("/campaigns/{campaign_id}", dependencies=[Depends(auth)])
def update_campaign(campaign_id: int, data: CampaignIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.update_campaign(campaign_id, data.name, data.product_key, data.objective, data.lang))


@router.post("/campaigns/{campaign_id}/toggle", dependencies=[Depends(auth)])
def toggle_campaign(campaign_id: int, data: ToggleIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.toggle_campaign(campaign_id, data.enabled))


@router.post("/campaigns/{campaign_id}/duplicate", dependencies=[Depends(auth)])
def duplicate_campaign(campaign_id: int, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.duplicate_campaign(campaign_id))


@router.get("/audiences", dependencies=[Depends(auth)])
def list_audiences(campaign_ids: Optional[str] = None, mgr: AdsManager = Depends(ads_service)):
    cids = _parse_ids(campaign_ids)
    return {"items": mgr.store.list_audiences(cids or None)}


@router.post("/audiences", dependencies=[Depends(auth)])
def create_audience(data: AudienceIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(
        lambda: mgr.store.create_audience(
            data.campaign_id,
            data.name,
            data.segment,
            data.rules.model_dump(mode="json"),
            data.auto_discovery,
        )
    )


@router.put("/audiences/{audience_id}", dependencies=[Depends(auth)])
def update_audience(audience_id: int, data: AudienceIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(
        lambda: mgr.store.update_audience(
            audience_id,
            data.campaign_id,
            data.name,
            data.segment,
            data.rules.model_dump(mode="json"),
            data.auto_discovery,
        )
    )


@router.post("/audiences/{audience_id}/toggle", dependencies=[Depends(auth)])
def toggle_audience(audience_id: int, data: ToggleIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.toggle_audience(audience_id, data.enabled))


@router.post("/audiences/{audience_id}/discover", dependencies=[Depends(auth)])
def trigger_discovery(audience_id: int, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.request_discovery(audience_id))


@router.get("/leads", dependencies=[Depends(auth)])
def list_leads(
    campaign_ids: Optional[str] = None,
    audience_ids: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    mgr: AdsManager = Depends(ads_service),
):
    safe_limit = max(1, min(500, limit))
    safe_offset = max(0, offset)
    return mgr.store.list_leads(
        campaign_ids=_parse_ids(campaign_ids) or None,
        audience_ids=_parse_ids(audience_ids) or None,
        status=status,
        q=q,
        limit=safe_limit,
        offset=safe_offset,
    )


@router.post("/leads/{lead_id}/toggle", dependencies=[Depends(auth)])
def toggle_lead(lead_id: int, data: ToggleIn, mgr: AdsManager = Depends(ads_service)):
    return _invoke(lambda: mgr.store.toggle_lead(lead_id, data.enabled))


@router.post("/leads/{lead_id}/call", dependencies=[Depends(auth)])
def call_single_lead(lead_id: int, mgr: AdsManager = Depends(ads_service)):
    try:
        return mgr.call_one_lead(lead_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        raise HTTPException(502, f"Telephony call failed: {exc}") from None


@router.post("/leads/{lead_id}/offer", dependencies=[Depends(auth)])
def send_offer_lead(lead_id: int, mgr: AdsManager = Depends(ads_service)):
    try:
        return mgr.send_offer_to_lead(lead_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        raise HTTPException(502, f"WhatsApp offer failed: {exc}") from None


@router.post("/leads/queue", dependencies=[Depends(auth)])
def enqueue_leads(data: QueueIn, mgr: AdsManager = Depends(ads_service)):
    whitelist = mgr.fetch_whitelist()
    mgr.store.refresh_whitelist_flags(whitelist)
    return mgr.store.enqueue(data.lead_ids, data.lang, data.gap_seconds, mgr.clock())


@router.get("/leads/export.csv", dependencies=[Depends(auth)])
def export_leads_csv(
    campaign_ids: Optional[str] = None,
    audience_ids: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    mgr: AdsManager = Depends(ads_service),
):
    data = mgr.store.list_leads(
        campaign_ids=_parse_ids(campaign_ids) or None,
        audience_ids=_parse_ids(audience_ids) or None,
        status=status,
        q=q,
        limit=5000,
        offset=0,
    )
    buf = io.StringIO()
    buf.write("\ufeff")
    fields = [
        "id",
        "company",
        "bin",
        "city",
        "segment",
        "category",
        "service",
        "director",
        "phone_e164",
        "whitelisted",
        "contract_sum_kzt",
        "icp_score",
        "status",
        "source",
        "source_url",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in data["items"]:
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="lead_hunter_leads.csv"'},
    )


@router.get("/queue", dependencies=[Depends(auth)])
def get_queue(mgr: AdsManager = Depends(ads_service)):
    return mgr.store.queue_snapshot(100)


@router.post("/queue/pause", dependencies=[Depends(auth)])
def pause_queue(mgr: AdsManager = Depends(ads_service)):
    mgr.store.set_setting("am_queue_paused", "1")
    mgr.store.audit("am_queue_paused", "operator")
    return mgr.store.queue_snapshot(50)


@router.post("/queue/resume", dependencies=[Depends(auth)])
def resume_queue(mgr: AdsManager = Depends(ads_service)):
    mgr.store.set_setting("am_queue_paused", "0")
    mgr.store.audit("am_queue_resumed", "operator")
    return mgr.store.queue_snapshot(50)


@router.post("/queue/clear", dependencies=[Depends(auth)])
def clear_queue(mgr: AdsManager = Depends(ads_service)):
    cleared = mgr.store.clear_queue()
    return {"cleared": cleared, **mgr.store.queue_snapshot(50)}
