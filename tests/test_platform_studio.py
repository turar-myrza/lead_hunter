"""Tests for Platform Studio v2.0.0: AI Connectors, Prompt-to-Product, Prompt-to-Audience, and Isolated Call Scripts."""

import json
import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.ads_manager import ads_service
from app.api.control_center import service as cc_service
from app.api.platform_studio import studio_ai, studio_store
from app.core.config import Settings, get_settings
from app.main import app
from app.services.ads_manager import AdsManager
from app.services.ads_store import AdsStore
from app.services.ai_engine import AIEngine, build_call_script_preview
from app.services.control_center import ControlCenter
from app.services.control_store import ControlStore
from app.services.platform_store import PlatformStore
from app.services.sales_store import SalesStore


def _make_settings(tmp_path):
    return Settings(
        lead_hunter_api_token="studio-test-secret",
        lead_hunter_db=tmp_path / "studio.sqlite3",
        control_secure_cookie=False,
        control_ai_enabled=True,
        control_gemini_key="fake-gemini-key",
    )


def test_ai_connectors_masking_and_local_model(tmp_path):
    store = PlatformStore(SalesStore(tmp_path / "studio.sqlite3"))
    p1 = store.save_ai_provider({
        "name": "DeepSeek Cloud",
        "provider": "deepseek",
        "api_key": "sk-deepseek-1234567890abcdef",
        "base_url": "",
        "model_name": "deepseek-chat",
        "purpose": "audience_generation",
        "is_active": True,
    })
    assert p1["has_key"] is True
    assert "1234567890" not in p1["api_key_masked"]
    assert "api_key" not in p1

    p2 = store.save_ai_provider({
        "name": "Local Ollama Qwen",
        "provider": "local_openai",
        "api_key": "",
        "base_url": "http://192.168.1.50:11434/v1",
        "model_name": "qwen2.5:14b",
        "purpose": "all",
        "is_active": True,
    })
    assert p2["base_url"] == "http://192.168.1.50:11434/v1"
    assert len(store.list_ai_providers()) == 2


def test_call_script_isolation_zero_whatsapp_crm_leakage(tmp_path):
    store = PlatformStore(SalesStore(tmp_path / "studio.sqlite3"))
    company = store.save_company_profile({"company_name": "ТОО «Аврора-2014»"})
    telephony = store.save_telephony_config({"voice_name": "Aoede", "transfer_extension": "055"})
    prod = store.get_product("centrifuges")
    assert prod is not None

    preview_kz = build_call_script_preview(prod, company, telephony, lang="kz")
    assert "Аврора-2014" in preview_kz["greeting"]
    assert "центрифуга" in preview_kz["greeting"].lower()
    assert "whatsappcalls" not in preview_kz["system_prompt"].lower()
    assert "аудиожазбалары" not in preview_kz["system_prompt"].lower()
    assert preview_kz["voice_name"] == "Aoede"


def test_studio_api_prompt_to_product_and_audience_apply(tmp_path):
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    AdsStore(base)

    def mock_handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="ignore")
        if "Product/Service prompt" in body:
            payload = {
                "name": "DLAB DM0412 Зертханалық центрифуга",
                "name_ru": "Лабораторная центрифуга DLAB DM0412",
                "category": "Медициналық жабдықтар",
                "price_range": "245 000 – 310 000 ₸",
                "models": ["DM0412", "DM0412S"],
                "specs": "4500 RPM, 12x15ml rotor, CE/ISO certified, 24 months warranty.",
                "target_use_case": "Clinical blood serum separation in diagnostic labs.",
            }
        else:
            payload = {
                "summary": "High demand across clinical labs and hospitals in Kazakhstan.",
                "segments": [
                    {
                        "name": "Private Diagnostic Labs (Almaty)",
                        "segment": "b2b",
                        "description": "Routine blood diagnostics requiring reliable centrifuges.",
                        "decision_makers": ["Lab Director", "Chief Physician"],
                        "buying_signals": ["Clinic expansion", "Equipment replacement"],
                        "regions": ["almaty", "astana"],
                        "keywords": ["лаборатория", "клиника"],
                        "estimated_volume": "150-250 labs",
                    }
                ],
            }
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]},
        )

    transport = httpx.MockTransport(mock_handler)
    ai = AIEngine(pstore, settings, transport=transport)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[cc_service] = lambda: ControlCenter(ControlStore(base), settings)
    app.dependency_overrides[ads_service] = lambda: AdsManager(AdsStore(base), settings)
    app.dependency_overrides[studio_store] = lambda: pstore
    app.dependency_overrides[studio_ai] = lambda: ai
    try:
        with TestClient(app) as client:
            h = {"X-Control-Request": "1"}
            r = client.post("/platform/api/login", json={"password": "studio-test-secret"}, headers=h)
            assert r.status_code == 200

            # 1. Prompt-to-Product
            pr = client.post(
                "/platform/api/studio/products/from-prompt",
                json={"prompt": "DLAB DM0412 centrifuge", "lang": "ru", "auto_save": True},
                headers=h,
            )
            assert pr.status_code == 200
            saved_prod = pr.json()["saved"]
            assert saved_prod["name_ru"] == "Лабораторная центрифуга DLAB DM0412"

            # 2. Prompt-to-Audience
            ar = client.post(
                "/platform/api/studio/audiences/analyze-prompt",
                json={"product_key": saved_prod["slug"], "prompt": "Find private labs in Almaty"},
                headers=h,
            )
            assert ar.status_code == 200
            segs = ar.json()["segments"]
            assert len(segs) == 1

            # 3. Create campaign with new dynamic product slug & apply audience
            cr = client.post(
                "/platform/api/ads/campaigns",
                json={"name": "DLAB Q2", "product_key": saved_prod["slug"], "objective": "call_then_offer", "lang": "kz"},
                headers=h,
            )
            assert cr.status_code == 200
            cid = cr.json()["id"]

            ap = client.post(
                "/platform/api/studio/audiences/apply",
                json={"campaign_id": cid, "segments": segs},
                headers=h,
            )
            assert ap.status_code == 200
            assert ap.json()["created_count"] == 1

            # 4. Script preview endpoint
            sp = client.post(
                "/platform/api/studio/script/preview",
                json={"product_key": saved_prod["slug"], "lang": "kz"},
                headers=h,
            )
            assert sp.status_code == 200
            assert "DLAB DM0412" in sp.json()["greeting"]
    finally:
        app.dependency_overrides.clear()
