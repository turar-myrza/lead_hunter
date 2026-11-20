"""Safety and data-access tests for the deal-attention scenario."""

import json
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.exceptions import (
    Bitrix24AuthorizationError,
    Bitrix24ReadOnlyViolationError,
    Bitrix24UnexpectedResponseError,
)
from app.integrations.bitrix24 import READ_ONLY_METHODS, Bitrix24Client


FAKE_WEBHOOK_URL = "https://example.bitrix24.test/rest/11/not-a-real-secret"

OPEN_DEAL_SELECT = [
    "id",
    "title",
    "stageId",
    "categoryId",
    "assignedById",
    "createdTime",
    "lastActivityTime",
    "movedTime",
    "closedate",
]

ACTIVITY_SELECT = [
    "ID",
    "OWNER_ID",
    "TYPE_ID",
    "PROVIDER_ID",
    "PROVIDER_TYPE_ID",
    "SUBJECT",
    "CREATED",
    "START_TIME",
    "END_TIME",
    "DEADLINE",
    "COMPLETED",
    "DIRECTION",
    "IS_INCOMING_CHANNEL",
    "RESPONSIBLE_ID",
]

STAGE_HISTORY_SELECT = [
    "ID",
    "TYPE_ID",
    "OWNER_ID",
    "CREATED_TIME",
    "CATEGORY_ID",
    "STAGE_ID",
    "STAGE_SEMANTIC_ID",
]


def request_body(request: httpx.Request) -> Dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def batch_payload(
    results: Dict[str, Any],
    *,
    next_values: Dict[str, Any] | None = None,
    errors: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "result": {
            "result": results,
            "result_error": errors or [],
            "result_total": {},
            "result_next": next_values or [],
            "result_time": {},
        }
    }


def command_query(command: str) -> Dict[str, List[str]]:
    assert command.startswith("crm.activity.list?")
    return parse_qs(urlsplit(command).query)


@pytest.mark.asyncio
async def test_open_deals_uses_keyset_pagination_for_5050_items() -> None:
    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/crm.item.list.json")
        body = request_body(request)
        calls.append(body)
        last_id = body["filter"][">id"]
        upper = min(last_id + 50, 5050)
        items = [{"id": value, "title": f"Deal {value}"} for value in range(last_id + 1, upper + 1)]
        return httpx.Response(200, json={"result": {"items": items}})

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        deals = await client.list_open_deals()

    assert len(deals) == 5050
    assert deals[0]["id"] == 1
    assert deals[-1]["id"] == 5050
    assert len(calls) == 102
    assert [call["filter"][">id"] for call in calls[:3]] == [0, 50, 100]
    assert calls[-1]["filter"][">id"] == 5050
    for call in calls:
        assert call["entityTypeId"] == 2
        assert call["filter"]["stageSemanticId"] == "P"
        assert call["order"] == {"id": "ASC"}
        assert call["select"] == OPEN_DEAL_SELECT
        assert call["start"] == -1


@pytest.mark.asyncio
async def test_activity_batch_chunks_at_50_and_uses_query_context() -> None:
    batch_bodies: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/batch.json")
        body = request_body(request)
        batch_bodies.append(body)
        results: Dict[str, Any] = {}
        for key, command in body["cmd"].items():
            query = command_query(command)
            deal_id = int(query["filter[BINDINGS][0][OWNER_ID]"][0])
            assert query["filter[BINDINGS][0][OWNER_TYPE_ID]"] == ["2"]
            assert "filter[COMPLETED]" not in query
            assert query["order[ID]"] == ["ASC"]
            assert query["start"] == ["0"]
            assert query["select[0]"] == [ACTIVITY_SELECT[0]]
            assert len([name for name in query if name.startswith("select[")]) == len(ACTIVITY_SELECT)
            results[key] = [
                {
                    "ID": str(deal_id * 10),
                    "OWNER_ID": "999" if deal_id == 2 else str(deal_id),
                }
            ]
        return httpx.Response(200, json=batch_payload(results))

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        batch_pause_seconds=0,
    ) as client:
        activities = await client.list_deal_activities(range(51, 0, -1))

    assert len(batch_bodies) == 2
    assert [len(body["cmd"]) for body in batch_bodies] == [50, 1]
    assert activities[2] == [{"ID": "20", "OWNER_ID": "999"}]
    assert set(activities) == set(range(1, 52))


@pytest.mark.asyncio
async def test_pending_activity_batch_paginates_and_deduplicates_per_deal() -> None:
    starts: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        command = body["cmd"]["deal_1"]
        query = command_query(command)
        assert query["filter[COMPLETED]"] == ["N"]
        start = int(query["start"][0])
        starts.append(start)
        if start == 0:
            results = {"deal_1": [{"ID": str(value)} for value in range(1, 51)]}
            return httpx.Response(
                200,
                json=batch_payload(results, next_values={"deal_1": 50}),
            )
        return httpx.Response(
            200,
            json=batch_payload({"deal_1": [{"ID": "50"}, {"ID": "51"}]}),
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        batch_pause_seconds=0,
    ) as client:
        activities = await client.list_pending_deal_activities([1])

    assert starts == [0, 50]
    assert [int(item["ID"]) for item in activities[1]] == list(range(1, 52))


@pytest.mark.asyncio
async def test_activity_batch_surfaces_nested_authorization_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=batch_payload(
                {},
                errors={"deal_1": {"error": "insufficient_scope"}},
            ),
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24AuthorizationError, match="INSUFFICIENT_SCOPE"):
            await client.list_pending_deal_activities([1])


@pytest.mark.asyncio
async def test_activity_batch_retries_nested_rate_limit_error() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=batch_payload(
                    {},
                    errors={"deal_1": {"error": "QUERY_LIMIT_EXCEEDED"}},
                ),
            )
        return httpx.Response(200, json=batch_payload({"deal_1": []}))

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
        max_retries=1,
    ) as client:
        assert await client.list_pending_deal_activities([1]) == {1: []}

    assert calls == 2


@pytest.mark.asyncio
async def test_deal_stage_history_is_ordered_chunked_and_paginated() -> None:
    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        calls.append(body)
        first_owner_id = body["filter"]["@OWNER_ID"][0]
        return httpx.Response(
            200,
            json={"result": {"items": [{"ID": str(first_owner_id), "OWNER_ID": first_owner_id}]}},
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        history = await client.list_deal_stage_history(range(1, 92))

    assert len(history) == 3
    assert [len(call["filter"]["@OWNER_ID"]) for call in calls] == [45, 45, 1]
    for call in calls:
        assert call["entityTypeId"] == 2
        assert call["order"] == {"ID": "ASC"}
        assert call["select"] == STAGE_HISTORY_SELECT
        assert call["start"] == 0


@pytest.mark.asyncio
async def test_user_lookup_does_not_truncate_after_50_users() -> None:
    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        calls.append(body)
        ids = body["FILTER"]["ID"]
        return httpx.Response(
            200,
            json={"result": [{"ID": str(user_id), "NAME": f"User {user_id}"} for user_id in ids]},
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        users = await client.list_users(range(51, 0, -1))

    assert [int(user["ID"]) for user in users] == list(range(1, 52))
    assert [len(call["FILTER"]["ID"]) for call in calls] == [50, 1]
    for call in calls:
        assert call["SORT"] == "ID"
        assert call["ORDER"] == "ASC"
        assert call["start"] == 0


@pytest.mark.asyncio
async def test_empty_deal_id_sets_make_no_network_requests() -> None:
    network_was_called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_was_called
        network_was_called = True
        return httpx.Response(200, json={"result": []})

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.list_deal_activities([]) == {}
        assert await client.list_pending_deal_activities([]) == {}
        assert await client.list_deal_stage_history([]) == []
        assert await client.list_users([]) == []

    assert network_was_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_next", [True, -1, "invalid", 0])
async def test_offset_paginator_rejects_invalid_or_repeated_cursor(bad_next: Any) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"STATUS_ID": "NEW"}], "next": bad_next},
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        with pytest.raises(Bitrix24UnexpectedResponseError):
            await client.list_deal_stages(0)


@pytest.mark.asyncio
async def test_offset_paginator_rejects_decreasing_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = request_body(request)["start"]
        next_value = 50 if start == 0 else 25
        return httpx.Response(
            200,
            json={"result": [{"STATUS_ID": str(start)}], "next": next_value},
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        with pytest.raises(Bitrix24UnexpectedResponseError, match="non-advancing"):
            await client.list_deal_stages(0)


@pytest.mark.asyncio
async def test_offset_paginator_rejects_empty_page_with_next() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [], "next": 50})

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        with pytest.raises(Bitrix24UnexpectedResponseError, match="empty page"):
            await client.list_deal_stages(0)


@pytest.mark.asyncio
async def test_offset_paginator_supports_more_than_100_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = request_body(request)["start"]
        payload: Dict[str, Any] = {"result": [{"STATUS_ID": str(start)}]}
        if start < 100:
            payload["next"] = start + 1
        return httpx.Response(200, json=payload)

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        page_pause_seconds=0,
    ) as client:
        stages = await client.list_deal_stages(0)

    assert len(stages) == 101


@pytest.mark.asyncio
async def test_retryable_limit_error_is_retried_without_secret_leak() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})
        return httpx.Response(200, json={"result": {"items": []}})

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
        retry_backoff_seconds=0,
        max_retries=1,
    ) as client:
        assert await client.list_latest_deals_page() == []

    assert calls == 2


@pytest.mark.asyncio
async def test_read_only_allowlists_are_exact_and_block_batch_writes() -> None:
    assert READ_ONLY_METHODS == frozenset(
        {
            "crm.activity.list",
            "crm.item.list",
            "crm.stagehistory.list",
            "crm.status.list",
            "user.get",
        }
    )
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
            await client._call_read_only("crm.item.update", {"id": 1})  # noqa: SLF001
        with pytest.raises(Bitrix24ReadOnlyViolationError):
            await client._call_read_only_batch(  # noqa: SLF001
                {"unsafe": "crm.item.update?id=1"}
            )
        with pytest.raises(Bitrix24ReadOnlyViolationError):
            await client._call_endpoint("batch", {"cmd": {}})  # noqa: SLF001

    assert network_was_called is False


@pytest.mark.asyncio
async def test_activity_batch_rejects_wrong_nested_result_shape() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=batch_payload({"deal_1": {"items": []}}),
        )

    async with Bitrix24Client(
        FAKE_WEBHOOK_URL,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(Bitrix24UnexpectedResponseError):
            await client.list_deal_activities([1])
