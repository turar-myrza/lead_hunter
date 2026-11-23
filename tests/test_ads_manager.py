from datetime import datetime, timezone
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.main import app
from app.api.ads_manager import ads_service
from app.api.control_center import service as cc_service
from app.models.ads_manager import AudienceIn, AudienceRules
from app.services.ads_manager import (
    AdsManager,
    SourceUnavailable,
    compute_icp_score,
    normalize_kz_phones,
)
from app.services.ads_store import AdsStore
from app.services.control_center import ControlCenter
from app.services.control_store import ControlStore
from app.services.sales_store import SalesStore


def test_normalize_kz_phones():
    sample1 = "87172 50-23-85 / 57-86-40 / 8 771 083 26 76"
    assert normalize_kz_phones(sample1) == ["+77710832676", "+77172502385"]

    sample2 = "87232403590. ватцап 87084403590"
    assert normalize_kz_phones(sample2) == ["+77084403590", "+77232403590"]

    sample3 = "+7 (705) 694-52-45, 7056945245"
    assert normalize_kz_phones(sample3) == ["+77056945245"]


def test_icp_score():
    rules = AudienceRules(keywords=["шприц"], min_contract_kzt=10000, max_contract_kzt=500000)
    full_lead = {
        "phone_raw": "8 705 694-52-45",
        "director": "Иванов Иван",
        "bin": "161140030623",
        "contract_sum_kzt": 150000.0,
        "category": "Шприц инъекционный",
    }
    assert compute_icp_score(full_lead, rules) == 100

    landline_only = {
        "phone_raw": "87172 50-23-85",
        "director": "",
        "bin": "123",
        "contract_sum_kzt": 100.0,
        "category": "Бинт",
    }
    assert compute_icp_score(landline_only, rules) == 10


def test_b2c_policy_validation():
    with pytest.raises(ValidationError, match="B2C"):
        AudienceIn(
            campaign_id=1,
            name="Retail test",
            segment="b2c",
            rules=AudienceRules(sources=["csv_seed"]),
        )
    valid = AudienceIn(
        campaign_id=1,
        name="Retail manual",
        segment="b2c",
        rules=AudienceRules(sources=["manual_import"], manual_rows="company,phone\nA,+77056945245"),
    )
    assert valid.rules.sources == ["manual_import"]


def _make_manager(tmp_path, clock_fn=None, calls_log=None, offers_log=None, whitelist=None, goszakup_fn=None):
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir(exist_ok=True)
    settings = Settings(
        _env_file=None,
        lead_hunter_api_token="test-password",
        lead_hunter_db=tmp_path / "state.db",
        lead_hunter_output=tmp_path / "out",
        ads_seed_dir=seed_dir,
    )
    store = AdsStore(SalesStore(settings.lead_hunter_db))

    def fake_call(**kwargs):
        if calls_log is not None:
            calls_log.append(kwargs)
        return {"success": True, "stdout": "OK", "stderr": "", "mode": "mock"}

    def fake_offer(phone, item, lang, cfg, cstore=None):
        if offers_log is not None:
            offers_log.append((phone, item["id"], lang))
        return {"status": "sent", "wamid": "wamid.MOCK123", "phone": phone, "pdf_url": item["pdf_kz"]}

    wl = set(whitelist) if whitelist is not None else {"+77056945245"}
    mgr = AdsManager(
        store,
        settings,
        clock=clock_fn or (lambda: datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)),  # 11:00 Almaty Mon
        goszakup_fn=goszakup_fn,
        call_fn=fake_call,
        offer_fn=fake_offer,
        whitelist_fn=lambda: set(wl),
    )
    return mgr, seed_dir


def test_csv_seed_both_layouts_filters_and_upsert_preserves_status(tmp_path):
    mgr, seed_dir = _make_manager(tmp_path, whitelist={"+77710832676"})

    csv1 = seed_dir / "contracts.csv"
    csv1.write_text(
        "\ufeffcompany_name,bin_iin,city,real_contract_phone,default_profile_phone,best_phone_to_call,email,director_name,bank_account_iik,sample_item,contract_sum_kzt,contract_pdf_url,anno_url\n"
        'ТОО РОСФАРМА,161140030623,Almaty,,87172 50-23-85 / 8 771 083 26 76,87172 50-23-85 / 8 771 083 26 76,ast@mail.ru,АСТАХОВ,,Шприц,15 000.00,,https://gz/1\n'
        'ТОО ЮГ,130940022603,Shymkent,,,87011112233,,Директор,,Бинт,500 000.00,,https://gz/2\n',
        encoding="utf-8",
    )
    csv2 = seed_dir / "disposables.csv"
    csv2.write_text(
        '\ufeffcompany_name,bin_iin,city,contact_person,phone,email,address,sample_item,contract_sum_kzt,anno_url\n'
        '"ТОО МЕД",190140000111,Astana,Иванов,87056945245,m@x.kz,"г.Астана, ул. Мира",Центрифуга,250 000.00,https://gz/3\n',
        encoding="utf-8",
    )

    c1 = mgr.store.create_campaign("Camp 1", "centrifuges", "call_then_offer", "kz")
    aud = mgr.store.create_audience(
        c1["id"],
        "Almaty шприц",
        "b2b",
        AudienceRules(regions=["almaty"], keywords=["шприц"], sources=["csv_seed"]).model_dump(),
    )
    res = mgr.run_discovery(aud["id"])
    assert res["found"] == 1
    assert res["new"] == 1
    leads = mgr.store.list_leads(campaign_ids=[c1["id"]])["items"]
    assert leads[0]["company"] == "ТОО РОСФАРМА"
    assert leads[0]["phone_e164"] == "+77710832676"
    assert leads[0]["whitelisted"] is True
    assert leads[0]["service"] == "Зертханалық центрифугалар"

    # Second discovery must preserve status='called' and not overwrite
    mgr.store.update_lead_status(leads[0]["id"], "called", "done")
    res2 = mgr.run_discovery(aud["id"])
    assert res2["new"] == 0 and res2["updated"] == 1
    lead_after = mgr.store.get_lead(leads[0]["id"])
    assert lead_after["status"] == "called"


def test_goszakup_302_records_login_required(tmp_path, monkeypatch):
    def mock_get(self, url, **kwargs):
        return httpx.Response(302, headers={"location": "https://goszakup.gov.kz/ru/user/login"})

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    mgr, _ = _make_manager(tmp_path)
    camp = mgr.store.create_campaign("B2G", "centrifuges", "call_only", "ru")
    aud = mgr.store.create_audience(
        camp["id"],
        "Goszakup test",
        "b2g",
        AudienceRules(regions=["almaty"], sources=["goszakup"]).model_dump(),
    )
    res = mgr.run_discovery(aud["id"])
    assert res["found"] == 0
    assert res["errors"]["goszakup"] == "source_unavailable:login_required"
    assert mgr.store.get_audience(aud["id"])["last_result"]["errors"]["goszakup"] == "source_unavailable:login_required"


@pytest.mark.asyncio
async def test_queue_whitelist_gate_and_almaty_window(tmp_path):
    # 20:00 Almaty = 15:00 UTC -> outside window
    fake_clock = [datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)]
    calls = []
    mgr, _ = _make_manager(tmp_path, clock_fn=lambda: fake_clock[0], calls_log=calls, whitelist={"+77056945245"})

    camp = mgr.store.create_campaign("Dial Camp", "centrifuges", "call_then_offer", "kz")
    aud = mgr.store.create_audience(
        camp["id"],
        "Manual list",
        "b2b",
        AudienceRules(
            sources=["manual_import"],
            manual_rows=(
                "company,bin,city,phone\n"
                "Whitelisted Co,111111111111,Almaty,+77056945245\n"
                "Blocked Co,222222222222,Almaty,+77019998877\n"
            ),
        ).model_dump(),
        auto_discovery=False,
    )
    mgr.run_discovery(aud["id"])
    leads = mgr.store.list_leads(campaign_ids=[camp["id"]])["items"]
    wl_lead = next(x for x in leads if x["phone_e164"] == "+77056945245")
    blk_lead = next(x for x in leads if x["phone_e164"] == "+77019998877")

    enq = mgr.store.enqueue([wl_lead["id"], blk_lead["id"]], None, 90, fake_clock[0])
    assert enq["queued"] == 1
    assert enq["skipped"] == [{"id": blk_lead["id"], "reason": "not_whitelisted"}]

    # At 20:00 Almaty -> dialer must not place calls
    did_late = await mgr.worker_tick()
    assert did_late is False
    assert len(calls) == 0

    # Next day at 11:00 Almaty Tuesday -> dialer places the call
    fake_clock[0] = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)
    did_day = await mgr.worker_tick()
    assert did_day is True
    assert len(calls) == 1 and calls[0]["phone"] == "+77056945245"
    assert mgr.store.get_lead(wl_lead["id"])["status"] == "called"


def test_ads_api_cascade_duplicate_and_export(tmp_path):
    calls = []
    offers = []
    mgr, _ = _make_manager(tmp_path, calls_log=calls, offers_log=offers, whitelist={"+77056945245"})
    ctrl = ControlCenter(ControlStore(mgr.store.base), mgr.settings)

    app.dependency_overrides[get_settings] = lambda: mgr.settings
    app.dependency_overrides[cc_service] = lambda: ctrl
    app.dependency_overrides[ads_service] = lambda: mgr
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.get("/platform/api/ads/campaigns").status_code == 401
            client.headers["X-Control-Request"] = "1"
            assert client.post("/platform/api/login", json={"password": "test-password"}).status_code == 200

            c1 = client.post(
                "/platform/api/ads/campaigns",
                json={"name": "C1", "product_key": "centrifuges", "objective": "call_then_offer", "lang": "kz"},
            ).json()
            c2 = client.post(
                "/platform/api/ads/campaigns",
                json={"name": "C2", "product_key": "tires", "objective": "offer_then_call", "lang": "ru"},
            ).json()

            a1 = client.post(
                "/platform/api/ads/audiences",
                json={
                    "campaign_id": c1["id"],
                    "name": "A1",
                    "segment": "b2b",
                    "rules": {
                        "sources": ["manual_import"],
                        "manual_rows": "company,bin,city,phone\nTest KZ,000000000001,Almaty,+77056945245\nBlocked KZ,000000000002,Almaty,+77012223344",
                    },
                },
            ).json()
            mgr.run_discovery(a1["id"])

            # Duplicate C1 -> copies audience A1, not leads
            dup = client.post(f"/platform/api/ads/campaigns/{c1['id']}/duplicate").json()
            assert dup["status"] == "paused"
            assert dup["audiences_count"] == 1
            assert dup["leads_count"] == 0

            # Cascade filtering
            all_c1 = client.get(f"/platform/api/ads/leads?campaign_ids={c1['id']}").json()
            all_c2 = client.get(f"/platform/api/ads/leads?campaign_ids={c2['id']}").json()
            assert all_c1["total"] == 2
            assert all_c2["total"] == 0

            wl_id = next(x["id"] for x in all_c1["items"] if x["phone_e164"] == "+77056945245")
            blk_id = next(x["id"] for x in all_c1["items"] if x["phone_e164"] == "+77012223344")

            # Blocked non-whitelisted call -> 409
            assert client.post(f"/platform/api/ads/leads/{blk_id}/call").status_code == 409
            # Allowed whitelisted call -> 200
            assert client.post(f"/platform/api/ads/leads/{wl_id}/call").status_code == 200
            assert len(calls) == 1
            # Offer -> 200
            assert client.post(f"/platform/api/ads/leads/{wl_id}/offer").status_code == 200
            assert len(offers) == 1
            # CSV export has BOM
            csv_res = client.get(f"/platform/api/ads/leads/export.csv?campaign_ids={c1['id']}")
            assert csv_res.status_code == 200
            assert csv_res.content.startswith(b"\xef\xbb\xbf")
            assert "Test KZ" in csv_res.text
    finally:
        app.dependency_overrides.clear()
