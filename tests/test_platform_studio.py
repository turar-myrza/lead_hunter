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


def test_control_html_includes_declarative_scripts_and_cache_busting():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "web" / "control.html").read_text(encoding="utf-8")
    assert "studio.js" in html
    assert "ads_manager.js" in html
    assert "control.js" in html
    assert "control.css" in html
    assert "Universal Cockpit" in html
    # Ensure cache busting query parameters are present on assets
    assert "?v=" in html


def test_xss_escaping_in_script_preview():
    store = PlatformStore(SalesStore(":memory:"))
    company = store.save_company_profile({"company_name": "<script>alert('xss')</script> B2B Org"})
    telephony = store.save_telephony_config({"voice_name": "Kore"})
    prod = store.save_product({
        "name": "<img src=x onerror=alert(1)> Centrifuge",
        "name_ru": "Safe Centrifuge",
        "price_range": "100 000 ₸",
        "specs": "Safe specs",
    })
    preview = build_call_script_preview(prod, company, telephony, lang="kz")
    # Preview data must contain values as pure data without crashing or raw unescaped template execution
    assert "<script>" in preview["greeting"]
    assert "alert" in preview["greeting"]
    assert preview["seller_company"] == "<script>alert('xss')</script> B2B Org"


def test_ai_model_tiers_and_provider_routing(tmp_path):
    store = PlatformStore(SalesStore(tmp_path / "tiers.sqlite3"))
    store.save_ai_provider({
        "name": "Gemini Search Grounding",
        "provider": "gemini",
        "api_key": "gemini-research-key-12345",
        "base_url": "",
        "model_name": "gemini-2.5-flash",
        "purpose": "research_tier",
        "is_active": True,
    })
    store.save_ai_provider({
        "name": "DeepSeek Reasoner",
        "provider": "deepseek",
        "api_key": "deepseek-reasoning-key-12345",
        "base_url": "",
        "model_name": "deepseek-reasoner",
        "purpose": "reasoning_tier",
        "is_active": True,
    })
    store.save_ai_provider({
        "name": "Local Ollama Test",
        "provider": "local_openai",
        "api_key": "",
        "base_url": "http://127.0.0.1:11434/v1",
        "model_name": "qwen2.5:0.5b",
        "purpose": "test_tier",
        "is_active": True,
    })

    ai = AIEngine(store, Settings(lead_hunter_api_token="test", lead_hunter_db=tmp_path / "tiers.sqlite3"))
    
    # 1. Research tier picks Gemini
    cands_res = ai.resolve_candidate_providers(purpose="research_tier")
    assert cands_res[0]["name"] == "Gemini Search Grounding"
    assert cands_res[0]["model_name"] == "gemini-2.5-flash"

    # 2. Reasoning tier picks DeepSeek
    cands_reas = ai.resolve_candidate_providers(purpose="reasoning_tier")
    assert cands_reas[0]["name"] == "DeepSeek Reasoner"
    assert cands_reas[0]["model_name"] == "deepseek-reasoner"

    # 3. Test tier picks Local Ollama
    cands_test = ai.resolve_candidate_providers(purpose="test_tier")
    assert cands_test[0]["name"] == "Local Ollama Test"
    assert cands_test[0]["model_name"] == "qwen2.5:0.5b"


@pytest.mark.asyncio
async def test_provider_fallback_on_429_rate_limit(tmp_path):
    store = PlatformStore(SalesStore(tmp_path / "fallback.sqlite3"))
    store.save_ai_provider({
        "name": "Primary Provider",
        "provider": "openai",
        "api_key": "primary-key-12345678",
        "base_url": "https://primary.example.com/v1",
        "model_name": "gpt-4o",
        "purpose": "all",
        "is_active": True,
    })
    store.save_ai_provider({
        "name": "Secondary Fallback",
        "provider": "deepseek",
        "api_key": "secondary-key-12345678",
        "base_url": "https://secondary.example.com/v1",
        "model_name": "deepseek-chat",
        "purpose": "all",
        "is_active": True,
    })

    def fallback_mock(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "primary.example.com" in url:
            # Primary returns 429 Too Many Requests
            return httpx.Response(429, json={"error": {"message": "Rate limit reached. Try again later."}})
        elif "secondary.example.com" in url:
            # Secondary succeeds
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "{\"fallback_success\": true}"}}],
                "usage": {"prompt_tokens": 15, "completion_tokens": 8},
            })
        return httpx.Response(404)

    ai = AIEngine(
        store=store,
        settings=Settings(lead_hunter_api_token="test", lead_hunter_db=tmp_path / "fallback.sqlite3"),
        transport=httpx.MockTransport(fallback_mock),
    )

    res = await ai.complete(
        system_prompt="Test fallback",
        user_prompt="Run query",
        purpose="all",
        expect_json=True,
    )
    # The secondary fallback provider must complete the request seamlessly
    assert res["provider_name"] == "Secondary Fallback"
    assert res["model"] == "deepseek-chat"
    assert res["data"]["fallback_success"] is True

    # Usage must reflect recorded tokens and successful fallback
    summary = store.get_ai_usage_summary()
    assert summary["total_calls"] == 2  # 1 error on primary, 1 success on secondary
    assert summary["prompt_tokens"] == 15
    assert summary["completion_tokens"] == 8


@pytest.mark.asyncio
async def test_provider_health_check_ping_api(tmp_path):
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    AdsStore(base)

    p = pstore.save_ai_provider({
        "name": "Fast Flash Ping",
        "provider": "openai",
        "api_key": "sk-secret-ping-token-12345",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-4o-mini",
        "purpose": "test_tier",
        "is_active": True,
    })

    def ping_mock(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2},
        })

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[studio_store] = lambda: pstore
    app.dependency_overrides[studio_ai] = lambda: AIEngine(pstore, settings, transport=httpx.MockTransport(ping_mock))
    app.dependency_overrides[cc_service] = lambda: ControlCenter(ControlStore(base), settings)

    try:
        with TestClient(app) as client:
            h = {"X-Control-Request": "1"}
            lg = client.post("/platform/api/login", json={"password": "studio-test-secret"}, headers=h)
            assert lg.status_code == 200

            # Ping endpoint
            res = client.post(f"/platform/api/studio/connectors/models/{p['id']}/ping", headers=h)
            assert res.status_code == 200
            data = res.json()
            assert data["ok"] is True
            assert data["provider"] == "openai"
            assert data["model"] == "gpt-4o-mini"
            assert data["reply"] == "OK"
            assert data["latency_ms"] >= 0

            # Usage overview endpoint
            usg = client.get("/platform/api/studio/ai-usage", headers=h)
            assert usg.status_code == 200
            u_data = usg.json()
            assert u_data["total_calls"] >= 1
            assert u_data["prompt_tokens"] >= 8
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_zero_token_leakage_in_errors_and_tracebacks(tmp_path):
    store = PlatformStore(SalesStore(tmp_path / "leak.sqlite3"))
    sensitive_token = "sk-super-secret-production-token-998877665544"
    store.save_ai_provider({
        "name": "Failing Upstream",
        "provider": "openai",
        "api_key": sensitive_token,
        "base_url": "https://broken.example.com/v1",
        "model_name": "gpt-4o",
        "purpose": "all",
        "is_active": True,
    })

    def leak_mock(request: httpx.Request) -> httpx.Response:
        # Simulate upstream server echoing headers/keys in an error
        return httpx.Response(401, text=f"Unauthorized: token {sensitive_token} invalid")

    ai = AIEngine(
        store=store,
        settings=Settings(lead_hunter_api_token="test", lead_hunter_db=tmp_path / "leak.sqlite3"),
        transport=httpx.MockTransport(leak_mock),
    )

    with pytest.raises(AIEngineError) as exc_info:
        await ai.complete("sys", "usr", purpose="all", expect_json=False)

    err_str = str(exc_info.value)
    # The raw token must NEVER appear in the exception string
    assert sensitive_token not in err_str
    assert "••••" in err_str


