"""API checks for the read-only management-attention endpoint."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

import app.api.analytics as analytics_api
from app.core.config import Settings, get_settings
from app.main import app
from app.models.deal_attention import (
    DealAttentionItem,
    DealAttentionResponse,
    DealAttentionStatistics,
    RiskReason,
    RiskReasonCode,
)


FAKE_WEBHOOK = "https://example.bitrix24.test/rest/11/not-a-real-secret"


@pytest.mark.asyncio
async def test_attention_endpoint_serializes_the_management_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_at = datetime(
        2026,
        8,
        6,
        12,
        30,
        tzinfo=timezone(timedelta(hours=5)),
    )

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["webhook_url"] == FAKE_WEBHOOK

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeAttentionService:
        def __init__(self, _: object) -> None:
            pass

        async def get_deals_requiring_attention(self) -> DealAttentionResponse:
            return DealAttentionResponse(
                generated_at=generated_at,
                deals=[
                    DealAttentionItem(
                        id=42,
                        title="Priority deal",
                        responsible="Aida K.",
                        stage="КП отправлено",
                        risk_score=20,
                        reasons=[
                            RiskReason(
                                code=RiskReasonCode.NO_NEXT_ACTION,
                                label="Нет следующего действия",
                                weight=20,
                                details="Будущего действия нет.",
                            )
                        ],
                        recommendations=["Запланировать следующее действие."],
                    )
                ],
                statistics=DealAttentionStatistics(
                    total_open_deals=2,
                    requiring_attention=1,
                    without_detected_risks=1,
                    average_risk_score=10,
                    maximum_risk_score=20,
                    by_reason={code.value: int(code is RiskReasonCode.NO_NEXT_ACTION) for code in RiskReasonCode},
                ),
                warnings=[],
            )

    monkeypatch.setattr(analytics_api, "Bitrix24Client", FakeClient)
    monkeypatch.setattr(analytics_api, "DealAttentionService", FakeAttentionService)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        bitrix24_webhook_url=SecretStr(FAKE_WEBHOOK),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/analytics/deals/attention")
            detailed_response = await client.get(
                "/api/v1/analytics/deals/attention/details"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["generated_at"] == "2026-08-06T12:30:00+05:00"
    assert 2 <= len(body["main_conclusion"]) <= 4
    assert body["attention_items"] == [
        {
            "deal_id": 42,
            "client_or_deal": "Priority deal",
            "responsible": "Aida K.",
            "stage": "КП отправлено",
            "risk_score": 20,
            "priority": "low",
            "priority_label": "🟢 НИЗКИЙ",
            "why_attention": "По данным CRM: Нет следующего действия",
            "recommended_action": "Запланировать следующее действие.",
            "evidence_level": "risk",
            "evidence_label": "РИСК",
        }
    ]
    assert body["actions_today"] == []
    assert body["systemic_issues"] == []
    assert body["statistics"]["total_open_deals"] == 2
    assert body["statistics"]["requiring_attention"] == 1
    assert body["statistics"]["low_priority"] == 1
    assert any("не доказывает" in item for item in body["data_limitations"])
    assert "подроб" in body["details_available"].lower()

    assert detailed_response.status_code == 200
    detailed_body = detailed_response.json()
    assert detailed_body["deals"][0]["id"] == 42
    assert detailed_body["deals"][0]["risk_score"] == 20
    assert detailed_body["statistics"]["by_reason"]["no_next_action"] == 1
    assert detailed_body["warnings"] == []


@pytest.mark.asyncio
async def test_attention_endpoint_reports_missing_webhook_without_network() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        bitrix24_webhook_url=None,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/analytics/deals/attention")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": "BITRIX24_WEBHOOK_URL is missing from the local .env file."
    }
