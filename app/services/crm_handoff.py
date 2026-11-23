"""End-to-end autonomous CRM handoff, HMAC webhook verification, and pipeline conversion analytics."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Optional

import httpx

from app.core.config import Settings
from app.services.platform_store import PlatformStore
from app.services.sales_store import SalesStore, now

VERSION = "2.9.1"

HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS am_call_outcomes(
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL DEFAULT 0,
    phone TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    product_key TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '',
    recording_url TEXT NOT NULL DEFAULT '',
    bitrix_contact_id TEXT NOT NULL DEFAULT '',
    bitrix_deal_id TEXT NOT NULL DEFAULT '',
    handoff_status TEXT NOT NULL DEFAULT 'pending',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_am_outcomes_phone ON am_call_outcomes(phone);
"""


def verify_webhook_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify HMAC-SHA256 webhook signature using constant-time comparison."""
    if not secret or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return secrets.compare_digest(expected, provided)


class CRMHandoffService:
    """Handles signed call-outcome webhooks, Bitrix24 Contact/Deal creation, and pipeline analytics."""

    def __init__(self, base: SalesStore, settings: Settings, transport: Any = None):
        self.base = base
        self.settings = settings
        self.transport = transport
        with base.connect() as db:
            db.executescript(HANDOFF_SCHEMA)

    def _resolve_bitrix_webhook(self) -> str:
        pstore = PlatformStore(self.base)
        cfg = pstore.get_crm_connectors(masked=False)
        webhook = (cfg.get("bitrix24_webhook") or "").strip().rstrip("/")
        if not webhook and self.settings.bitrix24_webhook_url:
            webhook = self.settings.bitrix24_webhook_url.get_secret_value().strip().rstrip("/")
        return webhook

    async def handoff_to_bitrix24(
        self,
        phone: str,
        company: str = "",
        director: str = "",
        product_name: str = "",
        transcript: str = "",
        recording_url: str = "",
        email: str = "",
        opportunity_kzt: float = 0.0,
    ) -> dict[str, Any]:
        """Create Contact + Deal + Timeline Comment with transcript & recording in Bitrix24."""
        webhook = self._resolve_bitrix_webhook()
        if not webhook:
            return {"ok": False, "status": "skipped_no_webhook", "contact_id": "", "deal_id": ""}

        comment_block = (
            f"AI Voice SDR Qualified Lead\n"
            f"Product: {product_name or 'B2B Solution'}\n"
            f"Phone: {phone}\n"
            f"Recording: {recording_url or 'N/A'}\n\n"
            f"Transcript:\n{transcript[:3000]}"
        ).strip()

        async with httpx.AsyncClient(transport=self.transport, timeout=20.0) as client:
            contact_payload = {
                "fields": {
                    "NAME": director or company or phone,
                    "COMPANY_TITLE": company,
                    "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}],
                    "OPENED": "Y",
                    "COMMENTS": comment_block,
                }
            }
            if email:
                contact_payload["fields"]["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]

            c_resp = await client.post(f"{webhook}/crm.contact.add.json", json=contact_payload)
            if c_resp.status_code != 200:
                raise ValueError(f"bitrix_contact_http_{c_resp.status_code}")
            contact_id = str(c_resp.json().get("result") or "")

            deal_payload = {
                "fields": {
                    "TITLE": f"Lead Hunter AI: {company or phone} — {product_name or 'B2B'}",
                    "CONTACT_ID": contact_id,
                    "CURRENCY_ID": "KZT",
                    "OPPORTUNITY": float(opportunity_kzt or 0.0),
                    "OPENED": "Y",
                    "COMMENTS": comment_block,
                }
            }
            d_resp = await client.post(f"{webhook}/crm.deal.add.json", json=deal_payload)
            if d_resp.status_code != 200:
                raise ValueError(f"bitrix_deal_http_{d_resp.status_code}")
            deal_id = str(d_resp.json().get("result") or "")

            if deal_id:
                await client.post(
                    f"{webhook}/crm.timeline.comment.add.json",
                    json={
                        "fields": {
                            "ENTITY_ID": deal_id,
                            "ENTITY_TYPE": "deal",
                            "COMMENT": comment_block,
                        }
                    },
                )

        return {
            "ok": True,
            "status": "created",
            "contact_id": contact_id,
            "deal_id": deal_id,
        }

    async def process_call_outcome(
        self,
        phone: str,
        outcome: str,
        lead_id: int = 0,
        company: str = "",
        director: str = "",
        product_key: str = "",
        transcript: str = "",
        recording_url: str = "",
        opportunity_kzt: float = 0.0,
    ) -> dict[str, Any]:
        """Record call outcome and auto-handoff qualified leads to Bitrix24."""
        norm_outcome = (outcome or "no_answer").strip().lower()
        contact_id = ""
        deal_id = ""
        handoff_status = "not_required"

        if norm_outcome == "qualified":
            res = await self.handoff_to_bitrix24(
                phone=phone,
                company=company,
                director=director,
                product_name=product_key,
                transcript=transcript,
                recording_url=recording_url,
                opportunity_kzt=opportunity_kzt,
            )
            contact_id = res.get("contact_id", "")
            deal_id = res.get("deal_id", "")
            handoff_status = res.get("status", "failed")

        ts = now().isoformat()
        with self.base.connect() as db:
            row_id = db.execute(
                """
                INSERT INTO am_call_outcomes(
                    lead_id, phone, company, product_key, outcome,
                    transcript, recording_url, bitrix_contact_id, bitrix_deal_id,
                    handoff_status, created
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lead_id,
                    phone,
                    company,
                    product_key,
                    norm_outcome,
                    transcript[:5000],
                    recording_url[:500],
                    contact_id,
                    deal_id,
                    handoff_status,
                    ts,
                ),
            ).lastrowid
            if lead_id:
                new_status = "handoff_crm" if deal_id else norm_outcome
                try:
                    db.execute(
                        "UPDATE am_lead SET status=?, updated=? WHERE id=?",
                        (new_status, ts, lead_id),
                    )
                except Exception:
                    pass

        return {
            "id": row_id,
            "outcome": norm_outcome,
            "handoff_status": handoff_status,
            "bitrix_contact_id": contact_id,
            "bitrix_deal_id": deal_id,
        }

    def get_pipeline_analytics(self) -> dict[str, Any]:
        """Aggregate end-to-end funnel metrics across Products, Seeds, Leads, Calls, Offers, and CRM Deals."""
        def _safe_count(db, sql: str, params: tuple = ()) -> int:
            try:
                r = db.execute(sql, params).fetchone()
                return int(r[0]) if r else 0
            except Exception:
                return 0

        day_prefix = now().date().isoformat()
        with self.base.connect() as db:
            products = _safe_count(db, "SELECT COUNT(*) FROM am_products")
            seeds = _safe_count(db, "SELECT COUNT(*) FROM am_market_seeds")
            campaigns = _safe_count(db, "SELECT COUNT(*) FROM am_campaign")
            audiences = _safe_count(db, "SELECT COUNT(*) FROM am_audience")
            leads = _safe_count(db, "SELECT COUNT(*) FROM am_lead")
            calls_total = _safe_count(db, "SELECT COUNT(*) FROM cc_outreach_call WHERE status='initiated'")
            calls_today = _safe_count(db, "SELECT COUNT(*) FROM cc_outreach_call WHERE status='initiated' AND at LIKE ?", (f"{day_prefix}%",))
            offers_sent = _safe_count(db, "SELECT COUNT(*) FROM cc_outreach_offer WHERE status='sent'")
            outcomes_total = _safe_count(db, "SELECT COUNT(*) FROM am_call_outcomes")
            calls_qualified = _safe_count(db, "SELECT COUNT(*) FROM am_call_outcomes WHERE outcome='qualified'")
            deals_created = _safe_count(db, "SELECT COUNT(*) FROM am_call_outcomes WHERE bitrix_deal_id != ''")

        qual_rate = round((calls_qualified / outcomes_total) * 100.0, 1) if outcomes_total > 0 else 0.0
        return {
            "products": products,
            "seeds_indexed": seeds,
            "campaigns": campaigns,
            "audiences": audiences,
            "leads": leads,
            "calls_total": calls_total,
            "calls_today": calls_today,
            "offers_sent": offers_sent,
            "outcomes_total": outcomes_total,
            "calls_qualified": calls_qualified,
            "deals_created_in_crm": deals_created,
            "qualification_rate_pct": qual_rate,
        }
