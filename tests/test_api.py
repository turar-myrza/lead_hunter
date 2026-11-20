"""Small API-level checks that never contact Bitrix24."""

from datetime import datetime

import httpx
import pytest
from pydantic import SecretStr

import app.api.deals as deals_api
from app.core.config import Settings, get_settings
from app.main import app
from app.models.deal import DealSummary


@pytest.mark.asyncio
async def test_missing_local_webhook_has_clear_error() -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/deals/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": "BITRIX24_WEBHOOK_URL is missing from the local .env file."
    }


@pytest.mark.asyncio
async def test_successful_endpoint_serializes_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeDealService:
        def __init__(self, _: object) -> None:
            pass

        async def get_latest_deals(self, limit: int) -> list[DealSummary]:
            assert limit == 10
            return [
                DealSummary(
                    id=42,
                    title="Example deal",
                    stage="New",
                    responsible="Example User",
                    updated_at=datetime.fromisoformat("2026-08-05T11:30:00+05:00"),
                )
            ]

    monkeypatch.setattr(deals_api, "Bitrix24Client", FakeClient)
    monkeypatch.setattr(deals_api, "DealService", FakeDealService)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        bitrix24_webhook_url=SecretStr(
            "https://example.bitrix24.test/rest/11/not-a-real-secret"
        ),
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/deals/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "count": 1,
        "message": "Latest deals, ordered by modification date (newest first).",
        "deals": [
            {
                "id": 42,
                "title": "Example deal",
                "stage": "New",
                "responsible": "Example User",
                "updated_at": "2026-08-05T11:30:00+05:00",
            }
        ],
    }
