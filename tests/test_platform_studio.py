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
from app.services.ai_engine import AIEngine, AIEngineError, build_call_script_preview
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


@pytest.mark.asyncio
async def test_ssrf_prevention_blocks_private_and_metadata_urls(tmp_path):
    from app.services.web_scraper import SSRFBlockedError, validate_public_url

    blocked_targets = [
        "http://127.0.0.1:8088/platform",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ]
    for target in blocked_targets:
        with pytest.raises(SSRFBlockedError):
            validate_public_url(target, resolve_dns=False)

    # Also test API endpoint rejects SSRF URL with HTTP 400
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    AdsStore(base)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[studio_store] = lambda: pstore
    app.dependency_overrides[studio_ai] = lambda: AIEngine(pstore, settings)
    app.dependency_overrides[cc_service] = lambda: ControlCenter(ControlStore(base), settings)
    try:
        with TestClient(app) as client:
            h = {"X-Control-Request": "1"}
            client.post("/platform/api/login", json={"password": "studio-test-secret"}, headers=h)
            resp = client.post(
                "/platform/api/studio/products/from-prompt",
                json={"prompt": "Test product", "url": "http://169.254.169.254/latest/meta-data/"},
                headers=h,
            )
            assert resp.status_code == 400
            assert "ssrf_blocked" in resp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_html_to_markdown_and_prompt_url_scraping(tmp_path):
    from app.services.web_scraper import html_to_markdown

    sample_html = """
    <html>
      <head><title>DLAB DM0412 Lab Centrifuge</title></head>
      <body>
        <nav>Skip Navigation</nav>
        <script>alert('ignore me');</script>
        <h1>DLAB DM0412 Clinical Centrifuge</h1>
        <p>Price: 285 000 KZT in Almaty</p>
        <table><tr><th>Max Speed</th><td>4500 RPM</td></tr><tr><th>Certs</th><td>CE, ISO 13485</td></tr></table>
      </body>
    </html>
    """
    md = html_to_markdown(sample_html)
    assert "DLAB DM0412 Clinical Centrifuge" in md["markdown"]
    assert "alert('ignore me')" not in md["markdown"]
    assert "Skip Navigation" not in md["markdown"]
    assert "| Max Speed | 4500 RPM |" in md["markdown"]

    store = PlatformStore(SalesStore(tmp_path / "scrape.sqlite3"))
    settings = _make_settings(tmp_path)

    def scrape_and_ai_mock(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "shop.example.kz" in url:
            return httpx.Response(200, text=sample_html)
        # Gemini endpoint
        body = request.content.decode("utf-8", errors="ignore")
        assert "4500 RPM" in body  # Scraped markdown was injected into AI context
        payload = {
            "name_kz": "DLAB DM0412 Клиникалық центрифуга",
            "name_ru": "Клиническая центрифуга DLAB DM0412",
            "category": "Медициналық жабдықтар",
            "price_range_kzt": "285 000 ₸",
            "models": ["DM0412"],
            "specs": "4500 RPM, 12x15ml rotor",
            "certifications": "CE, ISO 13485",
            "target_use_case": "Diagnostic labs and clinics",
        }
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})

    ai = AIEngine(store, settings, transport=httpx.MockTransport(scrape_and_ai_mock))
    result = await ai.research_product_from_prompt(
        prompt="Research this centrifuge",
        url="https://shop.example.kz/dlab-dm0412",
    )
    assert result["name_kz"] == "DLAB DM0412 Клиникалық центрифуга"
    assert result["price_range_kzt"] == "285 000 ₸"
    assert result["certifications"] == "CE, ISO 13485"
    assert result["scraped_url"] == "https://shop.example.kz/dlab-dm0412"


def test_product_offer_preview_xss_and_branding(tmp_path):
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    AdsStore(base)
    p = pstore.save_product({
        "name": "Centrifuge X1",
        "name_ru": "Центрифуга X1",
        "price_range": "320 000 ₸",
        "specs": "<script>alert('xss')</script> 4500 RPM",
        "certifications": "CE, ISO 13485, GOST",
        "models": ["X1-Pro"],
    })
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[studio_store] = lambda: pstore
    app.dependency_overrides[cc_service] = lambda: ControlCenter(ControlStore(base), settings)
    try:
        with TestClient(app) as client:
            h = {"X-Control-Request": "1"}
            client.post("/platform/api/login", json={"password": "studio-test-secret"}, headers=h)
            res = client.get(f"/platform/api/studio/products/{p['id']}/offer-preview?lang=ru", headers=h)
            assert res.status_code == 200
            html = res.json()["html"]
            assert "КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ" in html
            assert "CE, ISO 13485, GOST" in html
            assert "<script>alert" not in html
            assert "&lt;script&gt;" in html
    finally:
        app.dependency_overrides.clear()


def test_fts5_100k_seed_query_under_50ms(tmp_path):
    from app.services.market_search import MarketSearchEngine

    engine = MarketSearchEngine(SalesStore(tmp_path / "bench_100k.sqlite3"))
    # Generate 100,000 seed records
    records = []
    for i in range(100_000):
        if i % 500 == 0:
            records.append({
                "bin": f"230540{i:06d}",
                "company": f"Almaty Diagnostic Centrifuge Lab #{i}",
                "city": "Almaty",
                "region": "almaty",
                "category": "Medical Equipment",
                "activity": "Clinical centrifuge blood separation laboratory",
                "contract_sum_kzt": 5_500_000.0,
            })
        else:
            records.append({
                "bin": f"190140{i:06d}",
                "company": f"Kazakhstan Trade Partner TOO #{i}",
                "city": "Astana" if i % 2 == 0 else "Shymkent",
                "category": "General B2B",
                "activity": "Wholesale industrial logistics supply",
                "contract_sum_kzt": 1_200_000.0,
            })

    inserted = engine.bulk_insert_seeds(records)
    assert inserted == 100_000
    assert engine.count_indexed() == 100_000

    res = engine.search(query="centrifuge diagnostic", region="almaty", limit=25)
    assert res["count"] == 25
    assert res["total_indexed"] == 100_000
    assert res["query_ms"] < 50.0, f"Expected sub-50ms query, got {res['query_ms']}ms"


def test_fts5_sql_injection_and_syntax_sanitization(tmp_path):
    from app.services.market_search import MarketSearchEngine

    engine = MarketSearchEngine(SalesStore(tmp_path / "sqli.sqlite3"))
    engine.bulk_insert_seeds([
        {"company": "MedTech Almaty", "city": "Almaty", "category": "Medical", "activity": "Centrifuges"},
        {"company": "Astana Clinic", "city": "Astana", "category": "Clinic", "activity": "Diagnostics"},
    ])

    malicious_inputs = [
        "'; DROP TABLE am_market_seeds; --",
        'MedTech" OR 1=1 --',
        'NEAR(MedTech, Clinic) * "unclosed',
        "' UNION SELECT 1,2,3,4,5,6,7,8,9,10,11,12 --",
    ]
    for payload in malicious_inputs:
        res = engine.search(query=payload, region="almaty' OR '1'='1")
        assert isinstance(res["items"], list)
        assert engine.count_indexed() == 2


def test_market_search_api_endpoint_and_csv_indexing(tmp_path):
    from app.api.platform_studio import studio_market
    from app.services.market_search import MarketSearchEngine

    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    AdsStore(base)
    m_engine = MarketSearchEngine(base)
    m_engine.bulk_insert_seeds([
        {"company": "Almaty Cardio Center", "city": "Almaty", "category": "Hospital", "activity": "Medical lab equipment"},
    ])

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[studio_store] = lambda: pstore
    app.dependency_overrides[studio_market] = lambda: m_engine
    app.dependency_overrides[cc_service] = lambda: ControlCenter(ControlStore(base), settings)
    try:
        with TestClient(app) as client:
            h = {"X-Control-Request": "1"}
            client.post("/platform/api/login", json={"password": "studio-test-secret"}, headers=h)
            res = client.get("/platform/api/studio/audiences/search-seeds?q=cardio&region=almaty", headers=h)
            assert res.status_code == 200
            data = res.json()
            assert data["count"] == 1
            assert data["items"][0]["company"] == "Almaty Cardio Center"
            assert data["query_ms"] >= 0
    finally:
        app.dependency_overrides.clear()


def test_ami_crlf_injection_sanitization():
    from calling.pbx_caller import sanitize_ami_value, validate_sip_phone, validate_transfer_extension

    assert validate_sip_phone("+77056945245") == "+77056945245"
    for bad_phone in [
        "+77056945245\r\nAction: Originate",
        "+77056945245\nSet: x=1",
        "7705@evil.sip",
        "+7705;drop",
        "abc",
    ]:
        with pytest.raises(ValueError):
            validate_sip_phone(bad_phone)

    assert validate_transfer_extension("055") == "055"
    for bad_ext in ["055\r\nAction: Command", "055;echo pwned", "055/../../etc"]:
        with pytest.raises(ValueError):
            validate_transfer_extension(bad_ext)

    cleaned = sanitize_ami_value("Hello\r\nAction: Originate\x00\nChannel: SIP/100")
    assert "\r" not in cleaned
    assert "\n" not in cleaned
    assert "\x00" not in cleaned
    assert cleaned == "Hello Action: Originate Channel: SIP/100"


def test_isolated_call_context_and_daily_cap_enforcement(tmp_path):
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    astore = AdsStore(base)
    cstore = ControlStore(base)

    pstore.save_company_profile({"company_name": "ТОО «МедТех Казахстан»"})
    pstore.save_telephony_config({"voice_name": "Aoede", "transfer_extension": "077", "daily_call_cap": 1})

    camp = astore.create_campaign(name="MedTech Q2", product_key="centrifuges", objective="call_then_offer", lang="kz")
    aud = astore.create_audience(camp["id"], name="Labs", segment="b2b", rules={"regions": ["almaty"]})
    astore.upsert_leads([{
        "campaign_id": camp["id"],
        "audience_id": aud["id"],
        "dedupe_key": "med-1",
        "company": "Almaty BioLab",
        "city": "Almaty",
        "phone_raw": "+77056945245",
        "phone_e164": "+77056945245",
        "director": "Айдар",
        "icp_score": 90,
        "whitelisted": 1,
    }])
    lead = astore.list_leads(campaign_ids=[camp["id"]])["items"][0]
    lead["campaign_status"] = "active"
    lead["product_key"] = "centrifuges"
    lead["objective"] = "call_then_offer"

    captured = {}

    def spy_pbx(**kwargs):
        captured.update(kwargs)
        return {"success": True, "stdout": "Call initiated"}

    mgr = AdsManager(astore, settings, call_pbx=spy_pbx, whitelist_fn=lambda: {"+77056945245"})
    res = mgr._execute_lead_call(lead, "kz")
    assert res["success"] is True
    assert "МедТех Казахстан" in captured["seller"]
    assert "МедТех Казахстан" in captured["greeting"]
    assert "центрифуга" in captured["greeting"].lower()
    assert captured["transfer_target"] == "077"
    assert "whatsappcalls" not in captured["custom_prompt"].lower()
    assert "аудиожазбалары" not in captured["custom_prompt"].lower()

    # Now daily_call_cap is 1 and 1 initiated call was logged today -> 2nd call must be blocked!
    with pytest.raises(ValueError, match="daily_call_cap_reached"):
        mgr._execute_lead_call(lead, "kz")
    updated_lead = astore.get_lead(lead["id"])
    assert updated_lead["status"] == "deferred_cap"





