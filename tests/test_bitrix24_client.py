"""Tests for the read-only Bitrix24 boundary and deal presentation."""

import json
from typing import Any, Dict, List

import httpx
import pytest

from app.core.exceptions import (
    Bitrix24AuthorizationError,
    Bitrix24EmptyResponseError,
    Bitrix24NetworkError,
    Bitrix24ReadOnlyViolationError,
)
from app.integrations.bitrix24 import Bitrix24Client
from app.services.deals import DealService


FAKE_WEBHOOK_URL = "https://example.bitrix24.test/rest/11/not-a-real-secret"


@pytest.mark.asyncio
async def test_latest_deals_are_sorted_requested_and_enriched() -> None:
    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append({"method": method, "body": body})

        if method == "crm.item.list.json":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "items": [
                            {
                                "id": 102,
                                "title": "Renewal",
                                "stageId": "NEW",
                                "categoryId": 0,
                                "assignedById": 7,
                                "updatedTime": "2026-08-05T11:30:00+05:00",
                            },
                            {
                                "id": 101,
                                "title": "Consulting project",
                                "stageId": "C2:PREPARATION",
                                "categoryId": 2,
                                "assignedById": 9,
                                "updatedTime": "2026-08-05T10:00:00+05:00",
                            },
                        ]
                    },
                    "total": 2,
                },
            )

        if method == "user.get.json":
            assert body == {
                "FILTER": {"ID": [7, 9]},
                "SORT": "ID",
                "ORDER": "ASC",
                "start": 0,
            }
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"ID": "7", "NAME": "Aida", "LAST_NAME": "S."},
                        {"ID": "9", "NAME": "Timur", "LAST_NAME": "K."},
                    ]
                },
            )

        if method == "crm.status.list.json":
            entity_id = body["filter"]["ENTITY_ID"]
            if entity_id == "DEAL_STAGE":
                stages = [{"STATUS_ID": "NEW", "NAME": "New"}]
            elif entity_id == "DEAL_STAGE_2":
                stages = [
                    {"STATUS_ID": "C2:PREPARATION", "NAME": "Preparing proposal"}
                ]
            else:  # pragma: no cover - makes an unexpected pipeline fail loudly
                raise AssertionError(f"Unexpected stage entity: {entity_id}")
            return httpx.Response(200, json={"result": stages})

        raise AssertionError(f"Unexpected REST method: {method}")

    transport = httpx.MockTransport(handler)
    async with Bitrix24Client(FAKE_WEBHOOK_URL, transport=transport) as client:
        deals = await DealService(client).get_latest_deals()

    assert [deal.id for deal in deals] == [102, 101]
    assert deals[0].stage == "New"
    assert deals[0].responsible == "Aida S."
    assert deals[1].stage == "Preparing proposal"
    assert deals[1].responsible == "Timur K."

    deal_request = calls[0]["body"]
    assert deal_request["entityTypeId"] == 2
    assert deal_request["order"] == {"updatedTime": "DESC", "id": "DESC"}
    assert deal_request["select"] == [
        "id",
        "title",
        "stageId",
        "categoryId",
        "assignedById",
        "updatedTime",
    ]
    assert {call["method"] for call in calls} == {
        "crm.item.list.json",
        "crm.status.list.json",
        "user.get.json",
    }


@pytest.mark.asyncio
async def test_valid_empty_deal_result_is_not_an_error() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"result": {"items": []}, "total": 0})

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        deals = await DealService(client).get_latest_deals()

    assert deals == []
    assert call_count == 1


@pytest.mark.asyncio
async def test_stage_list_follows_read_only_pagination() -> None:
    requested_starts: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requested_starts.append(body["start"])
        if body["start"] == 0:
            return httpx.Response(
                200,
                json={
                    "result": [{"STATUS_ID": "NEW", "NAME": "New"}],
                    "next": 50,
                },
            )
        return httpx.Response(
            200,
            json={"result": [{"STATUS_ID": "WON", "NAME": "Won"}]},
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        stages = await client.list_deal_stages(category_id=0)

    assert requested_starts == [0, 50]
    assert [stage["STATUS_ID"] for stage in stages] == ["NEW", "WON"]


@pytest.mark.asyncio
async def test_authorization_error_is_reported_safely() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "NO_AUTH_FOUND",
                "error_description": "Raw upstream details must not be exposed",
            },
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24AuthorizationError) as captured:
            await client.list_latest_deals_page()

    assert "NO_AUTH_FOUND" in str(captured.value)
    assert "Raw upstream" not in str(captured.value)
    assert "not-a-real-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_non_json_authorization_error_is_still_classified() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24AuthorizationError) as captured:
            await client.list_latest_deals_page()

    assert "HTTP_403" in str(captured.value)
    assert "not-a-real-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_network_error_does_not_expose_webhook_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection failed", request=request)

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24NetworkError) as captured:
            await client.list_latest_deals_page()

    assert "not-a-real-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_empty_http_response_is_reported() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24EmptyResponseError):
            await client.list_latest_deals_page()


@pytest.mark.asyncio
async def test_write_method_is_blocked_before_network_call() -> None:
    network_was_called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_was_called
        network_was_called = True
        return httpx.Response(200, json={"result": {}})

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24ReadOnlyViolationError):
            await client._call_read_only(  # noqa: SLF001 - verifies the safety boundary
                "crm.item.update",
                {"entityTypeId": 2, "id": 1},
            )

    assert network_was_called is False
