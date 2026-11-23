"""SQLite persistence layer for AI Model Connectors, Dynamic Product Catalog, Company Profile, and Telephony Settings."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.services.catalog import CATALOG
from app.services.sales_store import SalesStore, now

VERSION = "2.6.0"

STUDIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS am_ai_providers(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'all',
    is_active INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS am_ai_usage(
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL DEFAULT 0,
    provider_name TEXT NOT NULL,
    model_name TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'all',
    purpose TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS am_products(
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    name_ru TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price_range TEXT NOT NULL DEFAULT '',
    models TEXT NOT NULL DEFAULT '[]',
    specs TEXT NOT NULL DEFAULT '',
    certifications TEXT NOT NULL DEFAULT '',
    target_use_case TEXT NOT NULL DEFAULT '',
    pdf_kz TEXT NOT NULL DEFAULT '',
    pdf_ru TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
"""

DEFAULT_COMPANY_PROFILE = {
    "company_name": "ТОО «CRM Consulting»",
    "bin": "230540010755",
    "city": "Алматы",
    "website": "https://crmconsulting.kz",
    "manager_name": "Дәурен Тайыров",
    "manager_phone": "+77017211156",
    "description": "B2B жабдықтар, медициналық техника және IT шешімдер жеткізушісі",
    "default_pdf_kz": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges_kz.pdf",
    "default_pdf_ru": "https://chat.whatsappcalls.kz/brand-assets/offers/KP_Centrifuges.pdf",
}

DEFAULT_TELEPHONY_CONFIG = {
    "concurrent_calls": 1,
    "daily_call_cap": 200,
    "work_hours": "09:00-19:00 Asia/Almaty",
    "transfer_extension": "055",
    "voice_name": "Kore",
    "default_lang": "kz",
    "custom_greeting_kz": "",
    "custom_greeting_ru": "",
    "custom_prompt_kz": "",
    "custom_prompt_ru": "",
}

DEFAULT_CRM_CONNECTORS = {
    "bitrix24_webhook": "",
    "moysklad_token": "",
    "onec_odata_url": "",
    "cms_feed_url": "",
}

AVAILABLE_VOICES = [
    {"id": "Kore", "label": "Kore (Female · Firm & Professional)", "gender": "female", "style": "Clear B2B corporate tone"},
    {"id": "Aoede", "label": "Aoede (Female · Warm & Consultative)", "gender": "female", "style": "Friendly consultative tone"},
    {"id": "Puck", "label": "Puck (Male · Energetic & Direct)", "gender": "male", "style": "Fast-paced sales tone"},
    {"id": "Fenrir", "label": "Fenrir (Male · Deep & Authoritative)", "gender": "male", "style": "Executive negotiation tone"},
    {"id": "Charon", "label": "Charon (Male · Calm & Informative)", "gender": "male", "style": "Technical advisor tone"},
]


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "product").lower()).strip("-")
    return s[:48] or "product"


def _mask_key(key: str) -> str:
    raw = (key or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return raw[:4] + "••••" + raw[-4:]


class PlatformStore:
    """Durable store for AI providers, dynamic catalog products, company profile, and telephony settings."""

    def __init__(self, base: SalesStore):
        self.base = base
        with base.connect() as db:
            db.executescript(STUDIO_SCHEMA)
            cols = {r["name"] for r in db.execute("PRAGMA table_info(am_products)").fetchall()}
            if "certifications" not in cols:
                db.execute("ALTER TABLE am_products ADD COLUMN certifications TEXT NOT NULL DEFAULT ''")
        self._seed_default_products()

    def _seed_default_products(self) -> None:
        with self.base.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM am_products").fetchone()[0]
            if count > 0:
                return
            ts = now().isoformat()
            for item in CATALOG:
                db.execute(
                    """
                    INSERT OR IGNORE INTO am_products(
                        slug, name, name_ru, category, price_range, models, specs,
                        target_use_case, pdf_kz, pdf_ru, source, created, updated
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item["id"],
                        item["name"],
                        item.get("name_ru", item["name"]),
                        item.get("category", ""),
                        item.get("price_range", ""),
                        json.dumps(item.get("models", []), ensure_ascii=False),
                        item.get("specs", ""),
                        "",
                        item.get("pdf_kz", ""),
                        item.get("pdf_ru", ""),
                        "seed",
                        ts,
                        ts,
                    ),
                )

    def _get_json_setting(self, key: str, default: dict) -> dict:
        with self.base.connect() as db:
            row = db.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        if not row:
            return dict(default)
        try:
            merged = dict(default)
            merged.update(json.loads(row[0]))
            return merged
        except (ValueError, TypeError):
            return dict(default)

    def _set_json_setting(self, key: str, payload: dict) -> dict:
        raw = json.dumps(payload, ensure_ascii=False)
        with self.base.connect() as db:
            db.execute(
                "INSERT INTO setting(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, raw),
            )
        return payload

    # ------------------------------------------------------------------
    # Company Profile, Telephony & CRM Connectors
    # ------------------------------------------------------------------
    def get_company_profile(self) -> dict:
        return self._get_json_setting("company_profile", DEFAULT_COMPANY_PROFILE)

    def save_company_profile(self, data: dict) -> dict:
        current = self.get_company_profile()
        current.update({k: v for k, v in data.items() if k in DEFAULT_COMPANY_PROFILE})
        return self._set_json_setting("company_profile", current)

    def get_telephony_config(self) -> dict:
        return self._get_json_setting("telephony_config", DEFAULT_TELEPHONY_CONFIG)

    def save_telephony_config(self, data: dict) -> dict:
        current = self.get_telephony_config()
        current.update({k: v for k, v in data.items() if k in DEFAULT_TELEPHONY_CONFIG})
        return self._set_json_setting("telephony_config", current)

    def get_crm_connectors(self, masked: bool = True) -> dict:
        cfg = self._get_json_setting("crm_connectors", DEFAULT_CRM_CONNECTORS)
        if not masked:
            return cfg
        return {
            "bitrix24_webhook": _mask_key(cfg.get("bitrix24_webhook", "")),
            "bitrix24_configured": bool(cfg.get("bitrix24_webhook")),
            "moysklad_token": _mask_key(cfg.get("moysklad_token", "")),
            "moysklad_configured": bool(cfg.get("moysklad_token")),
            "onec_odata_url": cfg.get("onec_odata_url", ""),
            "cms_feed_url": cfg.get("cms_feed_url", ""),
        }

    def save_crm_connectors(self, data: dict) -> dict:
        current = self.get_crm_connectors(masked=False)
        for k in DEFAULT_CRM_CONNECTORS:
            if k in data and data[k] is not None:
                val = str(data[k]).strip()
                # Do not overwrite secret with masked placeholder
                if "••••" not in val:
                    current[k] = val
        self._set_json_setting("crm_connectors", current)
        return self.get_crm_connectors(masked=True)

    # ------------------------------------------------------------------
    # AI Model & Token Connectors
    # ------------------------------------------------------------------
    def list_ai_providers(self, include_secrets: bool = False) -> list[dict]:
        with self.base.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM am_ai_providers ORDER BY is_active DESC, id ASC").fetchall()]
        result = []
        for r in rows:
            r["is_active"] = bool(r["is_active"])
            r["has_key"] = bool(r.get("api_key"))
            if not include_secrets:
                r["api_key_masked"] = _mask_key(r.get("api_key", ""))
                r.pop("api_key", None)
            result.append(r)
        return result

    def get_ai_provider(self, provider_id: int, include_secrets: bool = False) -> Optional[dict]:
        with self.base.connect() as db:
            row = db.execute("SELECT * FROM am_ai_providers WHERE id=?", (provider_id,)).fetchone()
        if not row:
            return None
        r = dict(row)
        r["is_active"] = bool(r["is_active"])
        r["has_key"] = bool(r.get("api_key"))
        if not include_secrets:
            r["api_key_masked"] = _mask_key(r.get("api_key", ""))
            r.pop("api_key", None)
        return r

    def save_ai_provider(self, data: dict, provider_id: Optional[int] = None) -> dict:
        ts = now().isoformat()
        with self.base.connect() as db:
            if provider_id is None:
                pid = db.execute(
                    """
                    INSERT INTO am_ai_providers(name, provider, api_key, base_url, model_name, purpose, is_active, created, updated)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        data["name"].strip(),
                        data["provider"].strip().lower(),
                        (data.get("api_key") or "").strip(),
                        (data.get("base_url") or "").strip(),
                        data["model_name"].strip(),
                        (data.get("purpose") or "all").strip().lower(),
                        1 if data.get("is_active", True) else 0,
                        ts,
                        ts,
                    ),
                ).lastrowid
            else:
                existing = db.execute("SELECT api_key FROM am_ai_providers WHERE id=?", (provider_id,)).fetchone()
                if not existing:
                    raise ValueError("ai_provider_not_found")
                raw_key = (data.get("api_key") or "").strip()
                final_key = existing["api_key"] if (not raw_key or "••••" in raw_key) else raw_key
                db.execute(
                    """
                    UPDATE am_ai_providers
                    SET name=?, provider=?, api_key=?, base_url=?, model_name=?, purpose=?, is_active=?, updated=?
                    WHERE id=?
                    """,
                    (
                        data["name"].strip(),
                        data["provider"].strip().lower(),
                        final_key,
                        (data.get("base_url") or "").strip(),
                        data["model_name"].strip(),
                        (data.get("purpose") or "all").strip().lower(),
                        1 if data.get("is_active", True) else 0,
                        ts,
                        provider_id,
                    ),
                )
                pid = provider_id
        return self.get_ai_provider(pid, include_secrets=False)  # type: ignore[return-value]

    def delete_ai_provider(self, provider_id: int) -> bool:
        with self.base.connect() as db:
            rc = db.execute("DELETE FROM am_ai_providers WHERE id=?", (provider_id,)).rowcount
        return rc > 0

    # ------------------------------------------------------------------
    # Dynamic Product Catalog
    # ------------------------------------------------------------------
    def _unpack_product(self, row: Any) -> dict:
        d = dict(row)
        try:
            d["models"] = json.loads(d.get("models") or "[]")
        except (ValueError, TypeError):
            d["models"] = []
        # Compatibility alias so frontend & Ads Manager can use either id or slug
        d["product_id"] = d["id"]
        d["key"] = d["slug"]
        return d

    def list_products(self) -> list[dict]:
        with self.base.connect() as db:
            rows = db.execute("SELECT * FROM am_products ORDER BY id DESC").fetchall()
        return [self._unpack_product(r) for r in rows]

    def get_product(self, key_or_id: Any) -> Optional[dict]:
        with self.base.connect() as db:
            if isinstance(key_or_id, int) or (isinstance(key_or_id, str) and key_or_id.isdigit()):
                row = db.execute("SELECT * FROM am_products WHERE id=?", (int(key_or_id),)).fetchone()
            else:
                needle = str(key_or_id or "").strip()
                row = db.execute(
                    "SELECT * FROM am_products WHERE slug=? OR name=? OR name_ru=?",
                    (needle, needle, needle),
                ).fetchone()
        return self._unpack_product(row) if row else None

    def save_product(self, data: dict, product_id: Optional[int] = None) -> dict:
        ts = now().isoformat()
        models = data.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        models_json = json.dumps(models, ensure_ascii=False)
        name = data["name"].strip()
        name_ru = (data.get("name_ru") or name).strip()

        with self.base.connect() as db:
            if product_id is None:
                base_slug = _slugify(data.get("slug") or name_ru or name)
                slug = base_slug
                idx = 2
                while db.execute("SELECT 1 FROM am_products WHERE slug=?", (slug,)).fetchone():
                    slug = f"{base_slug}-{idx}"
                    idx += 1
                pid = db.execute(
                    """
                    INSERT INTO am_products(
                        slug, name, name_ru, category, price_range, models, specs,
                        certifications, target_use_case, pdf_kz, pdf_ru, source, created, updated
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        slug,
                        name,
                        name_ru,
                        (data.get("category") or "").strip(),
                        (data.get("price_range") or data.get("price_range_kzt") or "").strip(),
                        models_json,
                        (data.get("specs") or "").strip(),
                        (data.get("certifications") or "").strip(),
                        (data.get("target_use_case") or "").strip(),
                        (data.get("pdf_kz") or "").strip(),
                        (data.get("pdf_ru") or "").strip(),
                        (data.get("source") or "manual").strip(),
                        ts,
                        ts,
                    ),
                ).lastrowid
            else:
                rc = db.execute(
                    """
                    UPDATE am_products
                    SET name=?, name_ru=?, category=?, price_range=?, models=?, specs=?,
                        certifications=?, target_use_case=?, pdf_kz=?, pdf_ru=?, updated=?
                    WHERE id=?
                    """,
                    (
                        name,
                        name_ru,
                        (data.get("category") or "").strip(),
                        (data.get("price_range") or data.get("price_range_kzt") or "").strip(),
                        models_json,
                        (data.get("specs") or "").strip(),
                        (data.get("certifications") or "").strip(),
                        (data.get("target_use_case") or "").strip(),
                        (data.get("pdf_kz") or "").strip(),
                        (data.get("pdf_ru") or "").strip(),
                        ts,
                        product_id,
                    ),
                ).rowcount
                if not rc:
                    raise ValueError("product_not_found")
                pid = product_id
        return self.get_product(pid)  # type: ignore[return-value]

    def delete_product(self, product_id: int) -> bool:
        with self.base.connect() as db:
            rc = db.execute("DELETE FROM am_products WHERE id=?", (product_id,)).rowcount
        return rc > 0

    # ------------------------------------------------------------------
    # AI Token & Cost Usage Tracking
    # ------------------------------------------------------------------
    def record_ai_usage(
        self,
        provider_id: int,
        provider_name: str,
        model_name: str,
        tier: str,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        estimated_cost_usd: float,
        latency_ms: int,
        status: str = "success",
    ) -> int:
        ts = now().isoformat()
        total = max(0, prompt_tokens) + max(0, completion_tokens)
        with self.base.connect() as db:
            return db.execute(
                """
                INSERT INTO am_ai_usage(
                    provider_id, provider_name, model_name, tier, purpose,
                    prompt_tokens, completion_tokens, total_tokens,
                    estimated_cost_usd, latency_ms, status, created
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider_id,
                    provider_name,
                    model_name,
                    tier,
                    purpose,
                    max(0, prompt_tokens),
                    max(0, completion_tokens),
                    total,
                    round(max(0.0, estimated_cost_usd), 6),
                    max(0, latency_ms),
                    status,
                    ts,
                ),
            ).lastrowid

    def get_ai_usage_summary(self, limit: int = 50) -> dict:
        with self.base.connect() as db:
            stats = db.execute(
                """
                SELECT
                    COUNT(*) as total_calls,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0) as estimated_cost_usd,
                    COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms
                FROM am_ai_usage
                """
            ).fetchone()
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM am_ai_usage ORDER BY id DESC LIMIT ?", (max(1, limit),)
                ).fetchall()
            ]
        total_calls = stats["total_calls"] if stats else 0
        prompt_tokens = stats["prompt_tokens"] if stats else 0
        completion_tokens = stats["completion_tokens"] if stats else 0
        total_tokens = stats["total_tokens"] if stats else 0
        cost_usd = float(stats["estimated_cost_usd"] if stats else 0.0)
        avg_latency = float(stats["avg_latency_ms"] if stats else 0.0)

        return {
            "total_calls": total_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(cost_usd, 6),
            "estimated_cost_kzt": round(cost_usd * 500.0, 2),
            "avg_latency_ms": int(avg_latency),
            "recent": rows,
        }

