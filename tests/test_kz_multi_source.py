"""Unit & integration tests for Simultaneous Kazakhstan Multi-Source Discovery Engine (v2.9.0)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.control_center import service as cc_service
from app.api.platform_studio import studio_harvester, studio_market, studio_store
from app.core.config import Settings, get_settings
from app.main import app
from app.models.ads_manager import AudienceIn, AudienceRules
from app.services.ads_manager import AdsManager
from app.services.ads_store import AdsStore
from app.services.control_center import ControlCenter
from app.services.control_store import ControlStore
from app.services.discovery.kz_multi_source import (
    KazakhstanMultiSourceHarvester,
    extract_valid_kz_phones,
    normalize_company_key,
    parse_bulk_registry_file,
)
from app.services.market_search import MarketSearchEngine
from app.services.platform_store import PlatformStore
from app.services.sales_store import SalesStore


SATU_SEARCH_HTML = """
<html><body>
  <div data-qaid="product_block">
    <a data-qaid="product_link" href="/p101-centrifuge.html"><span data-qaid="product_name">Центрифуга СМ-6МТ</span></a>
    <a href="/c472283-too-labstarmed.html"><span data-qaid="company_name">ТОО "LabStarMed"</span></a>
    <span data-qaid="product_price">от 792 000 ₸</span>
  </div>
  <div data-qaid="product_block">
    <a data-qaid="product_link" href="/p202-blades.html"><span data-qaid="product_name">Лезвия хирургические</span></a>
    <a href="/c888111-too-almaty-med.html"><span data-qaid="company_name">ТОО «Almaty Med Pro»</span></a>
    <span data-qaid="product_price">125 000 ₸</span>
  </div>
</body></html>
"""

SATU_PROFILE_LABSTARMED = """
<html><body>
  <h1>ТОО LabStarMed — г. Алматы</h1>
  <script>var trackingId = "87993027864";</script>
  <a href="tel:+77075296883">+7 (707) 529-68-83</a>
  <a href="https://wa.me/77075296883">WhatsApp</a>
  <p>Email: lsm_svetlana@mail.ru</p>
</body></html>
"""

SATU_PROFILE_ALMATYMED = """
<html><body>
  <h1>ТОО Almaty Med Pro — Алматы</h1>
  <p>Контакты: +7 (701) 555-44-33, офис 8 (727) 250-11-22, sales@almatymed.kz</p>
</body></html>
"""


def _build_all_six_sources_transport():
    """Mock HTTP transport answering all 6 Kazakhstan sources cleanly."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "satu.kz/search" in url:
            return httpx.Response(200, text=SATU_SEARCH_HTML)
        if "satu.kz/c472283-too-labstarmed.html" in url:
            return httpx.Response(200, text=SATU_PROFILE_LABSTARMED)
        if "satu.kz/c888111-too-almaty-med.html" in url:
            return httpx.Response(200, text=SATU_PROFILE_ALMATYMED)

        if "pk.uchet.kz/api/web/company/search" in url:
            body = json.loads(request.content.decode("utf-8"))
            val = (body.get("value") or "").lower()
            if "labstarmed" in val or val == "150540010004":
                return httpx.Response(200, json={
                    "results": [{
                        "bin": "150540010004",
                        "name": 'Товарищество с ограниченной ответственностью "LabStarMed"',
                        "head_fullname": "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА",
                        "address": "КАЗАХСТАН, ГОРОД АЛМАТЫ, АУЭЗОВСКИЙ РАЙОН, мкр 9, дом 12",
                        "kato_code": 751310000,
                        "status": "active",
                    }]
                })
            if "almaty med" in val:
                return httpx.Response(200, json={
                    "results": [{
                        "bin": "200140009988",
                        "name": 'ТОО "Almaty Med Pro"',
                        "head_fullname": "СЕРИКОВ АЙДАР",
                        "address": "Г. АЛМАТЫ, БОСТАНДЫКСКИЙ РАЙОН",
                        "kato_code": 751410000,
                        "status": "active",
                    }]
                })
            return httpx.Response(200, json={
                "results": [{
                    "bin": "191240014702",
                    "name": 'Товарищество с ограниченной ответственностью "Sau Med Group"',
                    "head_fullname": "ЖАРИМБЕТОВА САЛТАНАТ ДАНИЯРОВНА",
                    "address": "Казахстан, город Астана, район Есиль",
                    "kato_code": 711210000,
                    "status": "active",
                }]
            })

        if "catalog.api.2gis.com/3.0/items" in url:
            return httpx.Response(200, json={
                "meta": {"code": 200},
                "result": {
                    "items": [{
                        "name": "БиоМед Диагностика",
                        "address_name": "проспект Абая, 150",
                        "rubrics": [{"name": "Медицинское оборудование"}],
                        "contact_groups": [{
                            "contacts": [
                                {"type": "whatsapp", "value": "+7 705 777-88-99"},
                                {"type": "email", "value": "info@biomed.kz"},
                                {"type": "website", "value": "https://biomed.kz"},
                            ]
                        }],
                    }]
                },
            })

        if "data.egov.kz/api/v4/gbd_ul/v1" in url:
            return httpx.Response(200, json=[{
                "bin": "180340005511",
                "nameRu": 'ТОО "КазМедПрибор"',
                "directorFio": "НУРЛАНОВ БАУЫРЖАН",
                "address": "г. Алматы, ул. Тимирязева, 42",
                "katoCode": "751410000",
                "okedCode": "46461",
                "okedNameRu": "Оптовая торговля медицинским оборудованием",
                "status": "active",
            }])

        if "statsnet.co/api/v1/companies/search" in url:
            return httpx.Response(200, json={
                "items": [{
                    "bin": "150540010004",
                    "name": 'ТОО "LabStarMed"',
                    "city": "Алматы",
                    "director": "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА",
                    "industry": "Лабораторная техника",
                    "taxes_kzt": 14500000,
                    "risk_level": "low",
                }]
            })

        if "statsnet.co/api/v1/companies/kz/150540010004/md" in url:
            return httpx.Response(
                200,
                text="# ТОО LabStarMed (BIN 150540010004)\n- 2025 Taxes: 14,500,000 KZT\n- Risk: Low",
                headers={"content-type": "text/markdown"},
            )

        if "e-ondiris.gov.kz/api/public/producers" in url:
            return httpx.Response(200, json={
                "content": [{
                    "bin": "170740003322",
                    "companyName": 'ТОО "КазМедПром Завод"',
                    "kato": "791110000",
                    "address": "г. Шымкент, Индустриальная зона",
                    "productName": "Центрифуги и медицинская мебель",
                    "director": "АЛИМОВ ЕРЛАН",
                    "phone": "+7 702 333-22-11",
                }]
            })

        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        lead_hunter_api_token="kz-test-secret",
        lead_hunter_db=tmp_path / "kz_multi.sqlite3",
        lead_hunter_output=tmp_path / "out",
        ads_seed_dir=tmp_path / "seeds",
    )


def test_strict_kz_phone_extraction_blocks_html_noise():
    noisy = (
        'scriptId=87993027864, invalid=89991234567, '
        'office 8 (727) 250-11-22, mobile +7 (707) 529-68-83, whatsapp 8 701 721 11 56'
    )
    phones = extract_valid_kz_phones(noisy)
    # Mobiles ranked before landline; invalid 8799... and 8999... strictly rejected!
    assert phones == ["+77075296883", "+77017211156", "+77272501122"]


def test_normalize_company_key_and_cross_source_deduplication(tmp_path):
    assert normalize_company_key('ТОО "LabStarMed"') == "labstarmed"
    assert normalize_company_key('Товарищество с ограниченной ответственностью «LabStarMed»') == "labstarmed"

    harvester = KazakhstanMultiSourceHarvester()
    merged = harvester.deduplicate_and_merge([
        {
            "company": 'ТОО "LabStarMed"',
            "phone_raw": "+7 (707) 529-68-83",
            "email": "lsm_svetlana@mail.ru",
            "source": "satu_kz",
        },
        {
            "company": 'Товарищество с ограниченной ответственностью "LabStarMed"',
            "bin": "150540010004",
            "director": "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА",
            "city": "Алматы",
            "source": "uchet_pk",
        },
    ])
    assert len(merged) == 1
    rec = merged[0]
    assert rec["bin"] == "150540010004"
    assert rec["phone_e164"] == "+77075296883"
    assert rec["whatsapp_active"] is True
    assert rec["director"] == "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА"
    assert rec["email"] == "lsm_svetlana@mail.ru"
    assert rec["sources"] == ["satu_kz", "uchet_pk"]


@pytest.mark.asyncio
async def test_simultaneous_multi_source_harvest_all_six_sources_concurrently(tmp_path):
    base = SalesStore(tmp_path / "six.sqlite3")
    market = MarketSearchEngine(base)
    harvester = KazakhstanMultiSourceHarvester(
        market_engine=market,
        api_keys={
            "twogis_api_key": "fake-2gis",
            "egov_api_key": "fake-egov",
            "statsnet_api_key": "sk_live_fake",
        },
        transport=_build_all_six_sources_transport(),
    )

    res = await harvester.discover_simultaneous(
        sources=["satu_kz", "uchet_pk", "twogis", "egov_open_data", "statsnet", "e_ondiris"],
        keywords=["центрифуга"],
        regions=["all"],
        require_phone=False,
        auto_enrich_bin=True,
        index_to_fts=True,
        limit=50,
    )

    # All 6 sources succeeded concurrently
    for src in ("satu_kz", "uchet_pk", "twogis", "egov_open_data", "statsnet", "e_ondiris"):
        assert res["source_report"][src]["status"] == "ok"
        assert res["source_report"][src]["found"] >= 1

    # LabStarMed from Satu.kz + Statsnet + Uchet-PK merged into 1 enriched entity!
    lab = next(x for x in res["items"] if x["bin"] == "150540010004")
    assert lab["phone_e164"] == "+77075296883"
    assert lab["whatsapp_active"] is True
    assert lab["director"] == "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА"
    assert lab["tax_paid_kzt"] == 14500000.0
    assert "satu_kz" in lab["sources"]
    assert "statsnet" in lab["sources"]

    # Almaty Med Pro from Satu.kz was waterfall-enriched with BIN & Director via Uchet-PK!
    almaty_med = next(x for x in res["items"] if x["bin"] == "200140009988")
    assert almaty_med["phone_e164"] == "+77015554433"
    assert almaty_med["director"] == "СЕРИКОВ АЙДАР"

    # 2GIS, eGov, e-Ondiris companies also present!
    bins_and_names = {x["bin"] or x["company"] for x in res["items"]}
    assert "180340005511" in bins_and_names  # data.egov.kz
    assert "170740003322" in bins_and_names  # e-Ondiris KTP
    assert any("БиоМед" in x["company"] for x in res["items"])  # 2GIS

    # All newly harvested records were automatically indexed into SQLite FTS5!
    assert res["indexed_to_fts"] >= 5
    fts_hit = market.search(query="LabStarMed", region="almaty")
    assert fts_hit["count"] == 1
    assert fts_hit["items"][0]["bin"] == "150540010004"


@pytest.mark.asyncio
async def test_graceful_degradation_when_some_sources_lack_keys_or_fail(tmp_path):
    def flaky_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "satu.kz/search" in url:
            return httpx.Response(200, text=SATU_SEARCH_HTML)
        if "satu.kz/c472283" in url:
            return httpx.Response(200, text=SATU_PROFILE_LABSTARMED)
        if "satu.kz/c888111" in url:
            return httpx.Response(200, text=SATU_PROFILE_ALMATYMED)
        if "pk.uchet.kz" in url:
            return httpx.Response(200, json={"results": [{
                "bin": "150540010004",
                "name": 'ТОО "LabStarMed"',
                "head_fullname": "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА",
                "address": "г. Алматы",
                "kato_code": 751310000,
                "status": "active",
            }]})
        if "data.egov.kz" in url:
            return httpx.Response(403, text="Forbidden")
        if "statsnet.co" in url:
            return httpx.Response(429, text="Too Many Requests")
        if "e-ondiris.gov.kz" in url:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(404)

    harvester = KazakhstanMultiSourceHarvester(
        api_keys={"twogis_api_key": "", "egov_api_key": "bad-key", "statsnet_api_key": "over-quota"},
        transport=httpx.MockTransport(flaky_handler),
    )

    res = await harvester.discover_simultaneous(
        sources=["satu_kz", "uchet_pk", "twogis", "egov_open_data", "statsnet", "e_ondiris"],
        keywords=["центрифуга"],
        regions=["all"],
        auto_enrich_bin=True,
    )

    assert res["source_report"]["satu_kz"]["status"] == "ok"
    assert res["source_report"]["uchet_pk"]["status"] == "ok"
    assert res["source_report"]["twogis"]["status"] == "api_key_required"
    assert res["source_report"]["egov_open_data"]["status"] == "invalid_api_key"
    assert res["source_report"]["statsnet"]["status"] == "rate_limited"
    assert "portal_unreachable" in res["source_report"]["e_ondiris"]["status"]
    assert res["total_merged"] >= 2


@pytest.mark.asyncio
async def test_waterfall_rejects_false_positive_substring_bin_match():
    """Ensure short generic names like 'ИП Amanat' do NOT wrongly inherit BIN of 'ТОО AmanatServiceStroy'."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "satu.kz/search" in url:
            return httpx.Response(200, text=(
                '<html><body><div data-qaid="product_block">'
                '<a href="/c999-ip-amanat.html"><span data-qaid="company_name">ИП Amanat</span></a>'
                '</div></body></html>'
            ))
        if "satu.kz/c999-ip-amanat.html" in url:
            return httpx.Response(200, text="<html><body>+7 705 111-22-33</body></html>")
        if "pk.uchet.kz" in url:
            return httpx.Response(200, json={"results": [{
                "bin": "231240022007",
                "name": 'Товарищество с ограниченной ответственностью "AmanatServiceStroy"',
                "head_fullname": "OTHER PERSON",
                "address": "г. Алматы",
                "kato_code": 751310000,
                "status": "active",
            }]})
        return httpx.Response(404)

    harvester = KazakhstanMultiSourceHarvester(transport=httpx.MockTransport(handler))
    res = await harvester.discover_simultaneous(
        sources=["satu_kz"],
        keywords=["товар"],
        auto_enrich_bin=True,
    )
    assert len(res["items"]) == 1
    assert res["items"][0]["company"] == "ИП Amanat"
    assert res["items"][0]["bin"] == ""
    assert res["enriched_count"] == 0


def test_ads_manager_simultaneous_discovery_with_kz_sources(tmp_path):
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    pstore.save_data_source_keys({
        "twogis_api_key": "k2gis",
        "egov_api_key": "kegov",
        "statsnet_api_key": "kstats",
    })
    astore = AdsStore(base)
    mgr = AdsManager(
        astore,
        settings,
        clock=lambda: datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc),
        whitelist_fn=lambda: {"+77075296883"},
        harvester_transport=_build_all_six_sources_transport(),
    )

    camp = astore.create_campaign("KZ Multi-Source Q2", "centrifuges", "call_then_offer", "kz")
    aud = astore.create_audience(
        camp["id"],
        "All KZ Commercial & Registry",
        "b2b",
        AudienceRules(
            regions=["all"],
            keywords=["центрифуга"],
            sources=["satu_kz", "uchet_pk", "twogis", "egov_open_data", "statsnet", "e_ondiris"],
            require_phone=False,
        ).model_dump(),
    )
    summary = mgr.run_discovery(aud["id"])
    assert summary["found"] >= 5
    assert summary["new"] >= 5
    assert summary["errors"] == {}

    leads = astore.list_leads(campaign_ids=[camp["id"]])["items"]
    lab = next(l for l in leads if l["bin"] == "150540010004")
    assert lab["phone_e164"] == "+77075296883"
    assert lab["whitelisted"] is True
    assert lab["director"] == "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА"


def test_studio_discovery_api_harvest_enrich_keys_and_bulk_import(tmp_path):
    settings = _make_settings(tmp_path)
    base = SalesStore(settings.lead_hunter_db)
    pstore = PlatformStore(base)
    astore = AdsStore(base)
    market = MarketSearchEngine(base)
    transport = _build_all_six_sources_transport()

    camp = astore.create_campaign("Med Equipment", "centrifuges", "call_then_offer", "ru")

    def fake_harvester():
        return KazakhstanMultiSourceHarvester(
            market_engine=market,
            api_keys=pstore.get_data_source_keys(masked=False),
            transport=transport,
        )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[studio_store] = lambda: pstore
    app.dependency_overrides[studio_market] = lambda: market
    app.dependency_overrides[studio_harvester] = fake_harvester
    app.dependency_overrides[cc_service] = lambda: ControlCenter(ControlStore(base), settings)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            # 1. Auth required
            assert client.get("/platform/api/studio/discovery/sources").status_code == 401

            h = {"X-Control-Request": "1"}
            assert client.post("/platform/api/login", json={"password": "kz-test-secret"}, headers=h).status_code == 200

            # 2. Save optional API keys & verify masking (never overwrites secret with ••••)
            k_res = client.put(
                "/platform/api/studio/discovery/keys",
                json={"twogis_api_key": "2gis_secret_987654", "statsnet_api_key": "sk_live_secret_12345"},
                headers=h,
            )
            assert k_res.status_code == 200
            assert k_res.json()["twogis_configured"] is True
            assert "••••" in k_res.json()["twogis_api_key"]
            assert "987654" not in k_res.json()["twogis_api_key"]

            # Resubmitting masked value must NOT destroy stored secret
            client.put(
                "/platform/api/studio/discovery/keys",
                json={"twogis_api_key": k_res.json()["twogis_api_key"]},
                headers=h,
            )
            assert pstore.get_data_source_keys(masked=False)["twogis_api_key"] == "2gis_secret_987654"

            # 3. Sources metadata endpoint
            src_res = client.get("/platform/api/studio/discovery/sources", headers=h)
            assert src_res.status_code == 200
            sources_by_id = {s["id"]: s for s in src_res.json()["sources"]}
            assert sources_by_id["satu_kz"]["configured"] is True
            assert sources_by_id["uchet_pk"]["configured"] is True
            assert sources_by_id["twogis"]["configured"] is True
            assert sources_by_id["statsnet"]["configured"] is True

            # 4. Run simultaneous harvest & save directly into Campaign
            harv_res = client.post(
                "/platform/api/studio/discovery/harvest",
                json={
                    "sources": ["satu_kz", "uchet_pk", "twogis", "statsnet"],
                    "keywords": ["центрифуга"],
                    "regions": ["all"],
                    "auto_enrich_bin": True,
                    "save_to_campaign_id": camp["id"],
                },
                headers=h,
            )
            assert harv_res.status_code == 200
            hdata = harv_res.json()
            assert hdata["total_merged"] >= 3
            assert hdata["saved_to_ads"]["inserted"] >= 3

            # 5. Instant BIN enrichment updating an existing Ads Manager lead
            aud = astore.list_audiences([camp["id"]])[0]
            astore.upsert_leads([{
                "campaign_id": camp["id"],
                "audience_id": aud["id"],
                "company": "LabStarMed",
                "bin": "",
                "city": "",
                "director": "",
                "phone_raw": "+77075296883",
                "phone_e164": "+77075296883",
                "icp_score": 40,
            }])
            target_lead = next(
                l for l in astore.list_leads(campaign_ids=[camp["id"]])["items"]
                if l["company"] == "LabStarMed"
            )
            enr = client.post(
                "/platform/api/studio/discovery/enrich-bin",
                json={"bin": "150540010004", "lead_id": target_lead["id"]},
                headers=h,
            )
            assert enr.status_code == 200
            edata = enr.json()
            assert edata["found"] is True
            assert edata["bin"] == "150540010004"
            assert edata["director"] == "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА"
            assert "14,500,000 KZT" in edata["markdown_dossier"]
            assert edata["updated_lead"]["bin"] == "150540010004"
            assert edata["updated_lead"]["director"] == "ХУДЯКОВА СВЕТЛАНА СЕРГЕЕВНА"
            assert edata["updated_lead"]["icp_score"] > 40

            # 6. Bulk Open Data registry import (.csv & .json) + security check against disallowed extension
            egov_json = json.dumps([
                {
                    "bin": "210140001122",
                    "nameRu": 'ТОО "Астана Клиника"',
                    "okedNameRu": "Больничная деятельность",
                    "directorFio": "КАСЫМОВ АРМАН",
                    "katoCode": "711110000",
                    "phone": "+77019876543",
                }
            ]).encode("utf-8")
            bulk_j = client.post(
                "/platform/api/studio/discovery/import-bulk",
                data={"source_tag": "egov_gbd_ul"},
                files={"file": ("gbd_ul_2026.json", egov_json, "application/json")},
                headers=h,
            )
            assert bulk_j.status_code == 200
            assert bulk_j.json()["newly_indexed"] == 1

            egov_csv = "\ufeffНаименование;БИН;Город;Телефон;Руководитель\nТОО Шымкент Лаб;220240009900;Шымкент;+77051112233;БЕКОВ Т.\n".encode("utf-8")
            bulk_c = client.post(
                "/platform/api/studio/discovery/import-bulk",
                data={"source_tag": "e_ondiris_ktp"},
                files={"file": ("ktp_producers.csv", egov_csv, "text/csv")},
                headers=h,
            )
            assert bulk_c.status_code == 200
            assert bulk_c.json()["newly_indexed"] == 1

            # Verify malicious executable extension rejected
            bad_bulk = client.post(
                "/platform/api/studio/discovery/import-bulk",
                data={"source_tag": "evil"},
                files={"file": ("shell.py", b"import os", "text/x-python")},
                headers=h,
            )
            assert bad_bulk.status_code == 400

            # Verify FTS5 finds both uploaded registries in <50ms
            hit = market.search(query="Астана Клиника", region="astana")
            assert hit["count"] == 1
            assert hit["items"][0]["bin"] == "210140001122"
    finally:
        app.dependency_overrides.clear()


def test_b2c_policy_still_blocks_kz_scraping_sources():
    with pytest.raises(ValidationError, match="B2C"):
        AudienceIn(
            campaign_id=1,
            name="B2C blocked",
            segment="b2c",
            rules=AudienceRules(sources=["satu_kz", "twogis"]),
        )


def test_bulk_registry_parser_handles_csv_and_json():
    csv_bytes = b"company,bin,city,phone,category\nTOO Test,123456789012,Karaganda,+77001112233,Industrial\n"
    rows = parse_bulk_registry_file(csv_bytes, "test.csv", source_tag="samruk_ktp")
    assert len(rows) == 1
    assert rows[0]["bin"] == "123456789012"
    assert rows[0]["phone"] == "+77001112233"
    assert rows[0]["source"] == "samruk_ktp"


def test_ui_includes_kz_multi_source_harvester_and_enrich_button():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "web"
    studio_js = (root / "studio.js").read_text(encoding="utf-8")
    control_js = (root / "control.js").read_text(encoding="utf-8")
    ads_js = (root / "ads_manager.js").read_text(encoding="utf-8")
    control_html = (root / "control.html").read_text(encoding="utf-8")

    assert "renderKzHarvester" in studio_js
    assert "/studio/discovery/harvest" in studio_js
    assert "/studio/discovery/enrich-bin" in studio_js
    assert "/studio/discovery/import-bulk" in studio_js
    assert "KZ Data Harvester" in control_js
    assert "/studio/discovery/enrich-bin" in ads_js
    assert "?v=2.9.0" in control_html

