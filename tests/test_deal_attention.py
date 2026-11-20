"""Unit tests for the read-only deal-attention risk calculation."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models.deal_attention import RiskReasonCode
from app.services.deal_attention import DealAttentionService


ALMATY = timezone(timedelta(hours=5))
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=ALMATY)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _deal(
    deal_id: int,
    *,
    title: str,
    stage_id: str = "NEW",
    category_id: int = 0,
    assigned_by_id: int | str = 7,
    created_time: datetime = NOW - timedelta(days=1),
    last_activity_time: datetime = NOW,
    moved_time: datetime = NOW,
    close_date: str | None = None,
) -> dict[str, Any]:
    return {
        "id": deal_id,
        "title": title,
        "stageId": stage_id,
        "categoryId": category_id,
        "assignedById": assigned_by_id,
        "createdTime": _iso(created_time),
        "lastActivityTime": _iso(last_activity_time),
        "movedTime": _iso(moved_time),
        "closedate": close_date or (NOW + timedelta(days=10)).date().isoformat(),
    }


class FakeAttentionClient:
    """In-memory implementation of only the read methods used by the service."""

    def __init__(
        self,
        *,
        deals: list[dict[str, Any]],
        users: list[dict[str, Any]] | None = None,
        stages: dict[int, list[dict[str, Any]]] | None = None,
        pending: dict[int, list[dict[str, Any]]] | None = None,
        history: list[dict[str, Any]] | None = None,
        activities: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.deals = deals
        self.users = users or []
        self.stages = stages or {}
        self.pending = pending or {}
        self.history = history or []
        self.activities = activities or {}
        self.calls: list[tuple[str, Any]] = []

    async def list_open_deals(self) -> list[dict[str, Any]]:
        self.calls.append(("list_open_deals", None))
        return self.deals

    async def list_users(self, user_ids: list[int]) -> list[dict[str, Any]]:
        self.calls.append(("list_users", list(user_ids)))
        return self.users

    async def list_deal_stages(self, category_id: int) -> list[dict[str, Any]]:
        self.calls.append(("list_deal_stages", category_id))
        return self.stages.get(category_id, [])

    async def list_pending_deal_activities(
        self,
        deal_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        self.calls.append(("list_pending_deal_activities", list(deal_ids)))
        return self.pending

    async def list_deal_stage_history(
        self,
        deal_ids: list[int],
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_deal_stage_history", list(deal_ids)))
        return self.history

    async def list_deal_activities(
        self,
        deal_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        self.calls.append(("list_deal_activities", list(deal_ids)))
        return self.activities


ACTIVE_USER = {
    "ID": "7",
    "NAME": "Aida",
    "LAST_NAME": "K.",
    "ACTIVE": "Y",
}


@pytest.mark.asyncio
async def test_all_seven_weights_are_additive_sorted_and_included_in_statistics() -> None:
    risky_deal = _deal(
        101,
        title="Maximum risk",
        stage_id="C1:PROPOSAL",
        category_id=1,
        assigned_by_id="",
        created_time=NOW - timedelta(days=20),
        last_activity_time=NOW - timedelta(days=4),
        moved_time=NOW - timedelta(days=8),
        close_date=(NOW - timedelta(days=1)).date().isoformat(),
    )
    healthy_deal = _deal(102, title="Healthy")
    lower_risk_deal = _deal(103, title="Needs next action")
    overdue_task = {
        "ID": "501",
        "TYPE_ID": "3",
        "PROVIDER_ID": "CRM_TASKS_TASK",
        "COMPLETED": "N",
        "DEADLINE": _iso(NOW - timedelta(days=1)),
    }
    future_action = {
        "ID": "502",
        "TYPE_ID": "2",
        "COMPLETED": "N",
        "DEADLINE": _iso(NOW + timedelta(days=1)),
    }
    client = FakeAttentionClient(
        deals=[lower_risk_deal, healthy_deal, risky_deal],
        users=[ACTIVE_USER],
        stages={
            0: [
                {"STATUS_ID": "NEW", "NAME": "Новая"},
                {"STATUS_ID": "PROPOSAL", "NAME": "КП отправлено"},
            ],
            1: [
                {"STATUS_ID": "C1:NEW", "NAME": "Новая"},
                {"STATUS_ID": "C1:PROPOSAL", "NAME": "КП отправлено"},
            ],
        },
        pending={101: [overdue_task], 102: [future_action], 103: []},
        history=[
            {
                "ID": "801",
                "OWNER_ID": "101",
                "CATEGORY_ID": "1",
                "STAGE_ID": "C1:PROPOSAL",
                "CREATED_TIME": _iso(NOW - timedelta(days=6)),
            }
        ],
        activities={
            101: [
                {
                    "ID": "701",
                    "START_TIME": _iso(NOW - timedelta(days=5, seconds=1)),
                    "DIRECTION": "2",
                }
            ]
        },
    )

    result = await DealAttentionService(client, now=NOW).get_deals_requiring_attention()

    assert [item.id for item in result.deals] == [101, 103]
    maximum = result.deals[0]
    assert maximum.risk_score == 165
    assert maximum.responsible == "Не назначен"
    assert maximum.stage == "КП отправлено"
    assert {reason.code: reason.weight for reason in maximum.reasons} == {
        RiskReasonCode.NO_ACTIVITY: 10,
        RiskReasonCode.NO_NEXT_ACTION: 20,
        RiskReasonCode.OVERDUE_TASK: 30,
        RiskReasonCode.LONG_IN_STAGE: 15,
        RiskReasonCode.NO_REACTION_AFTER_PROPOSAL: 15,
        RiskReasonCode.OVERDUE_CLOSE_DATE: 25,
        RiskReasonCode.NO_RESPONSIBLE: 50,
    }
    assert len(maximum.recommendations) == 7
    assert result.deals[1].risk_score == 20
    assert [reason.code for reason in result.deals[1].reasons] == [
        RiskReasonCode.NO_NEXT_ACTION
    ]
    assert result.statistics.model_dump() == {
        "total_open_deals": 3,
        "requiring_attention": 2,
        "without_detected_risks": 1,
        "average_risk_score": 61.67,
        "maximum_risk_score": 165,
        "by_reason": {
            "no_activity": 1,
            "no_next_action": 2,
            "overdue_task": 1,
            "long_in_stage": 1,
            "no_reaction_after_proposal": 1,
            "overdue_close_date": 1,
            "no_responsible": 1,
        },
    }
    assert result.warnings == []


@pytest.mark.asyncio
async def test_time_thresholds_are_strict_and_close_date_today_is_not_overdue() -> None:
    boundary_deal = _deal(
        201,
        title="Exactly on every boundary",
        stage_id="PROPOSAL",
        last_activity_time=NOW - timedelta(days=3),
        moved_time=NOW - timedelta(days=7),
        close_date=NOW.date().isoformat(),
    )
    client = FakeAttentionClient(
        deals=[boundary_deal],
        users=[ACTIVE_USER],
        stages={
            0: [
                {"STATUS_ID": "PROPOSAL", "NAME": "Коммерческое предложение"}
            ]
        },
        pending={
            201: [
                {
                    "ID": "601",
                    "TYPE_ID": "2",
                    "COMPLETED": "N",
                    "DEADLINE": _iso(NOW + timedelta(seconds=1)),
                }
            ]
        },
        history=[
            {
                "ID": "901",
                "OWNER_ID": "201",
                "CATEGORY_ID": "0",
                "STAGE_ID": "PROPOSAL",
                "CREATED_TIME": _iso(NOW - timedelta(days=5)),
            }
        ],
        activities={201: []},
    )

    result = await DealAttentionService(client, now=NOW).get_deals_requiring_attention()

    assert result.deals == []
    assert result.statistics.total_open_deals == 1
    assert result.statistics.requiring_attention == 0
    assert result.statistics.without_detected_risks == 1
    assert result.statistics.average_risk_score == 0
    assert all(count == 0 for count in result.statistics.by_reason.values())


@pytest.mark.asyncio
async def test_recent_activity_after_proposal_clears_stale_followup_risk() -> None:
    stale_followup = _deal(301, title="Stale follow-up", stage_id="PROPOSAL")
    recent_followup = _deal(302, title="Recent follow-up", stage_id="PROPOSAL")
    future_action_301 = {
        "ID": "1001",
        "COMPLETED": "N",
        "DEADLINE": _iso(NOW + timedelta(days=1)),
    }
    future_action_302 = {
        "ID": "1002",
        "COMPLETED": "N",
        "DEADLINE": _iso(NOW + timedelta(days=1)),
    }
    client = FakeAttentionClient(
        deals=[recent_followup, stale_followup],
        users=[ACTIVE_USER],
        stages={
            0: [
                {"STATUS_ID": "PROPOSAL", "NAME": "КП отправлено"},
            ]
        },
        pending={301: [future_action_301], 302: [future_action_302]},
        history=[
            {
                "ID": "1101",
                "OWNER_ID": "301",
                "CATEGORY_ID": "0",
                "STAGE_ID": "PROPOSAL",
                "CREATED_TIME": _iso(NOW - timedelta(days=6)),
            },
            {
                "ID": "1102",
                "OWNER_ID": "302",
                "CATEGORY_ID": "0",
                "STAGE_ID": "PROPOSAL",
                "CREATED_TIME": _iso(NOW - timedelta(days=6)),
            },
        ],
        activities={
            301: [
                {
                    "ID": "1201",
                    "START_TIME": _iso(NOW - timedelta(days=5, seconds=1)),
                    "DIRECTION": "2",
                }
            ],
            302: [
                {
                    "ID": "1202",
                    "START_TIME": _iso(NOW - timedelta(days=4)),
                    "DIRECTION": "1",
                }
            ],
        },
    )

    result = await DealAttentionService(client, now=NOW).get_deals_requiring_attention()

    assert [item.id for item in result.deals] == [301]
    assert result.deals[0].risk_score == 15
    assert [reason.code for reason in result.deals[0].reasons] == [
        RiskReasonCode.NO_REACTION_AFTER_PROPOSAL
    ]
    assert result.statistics.by_reason["no_reaction_after_proposal"] == 1


@pytest.mark.asyncio
async def test_boolean_false_user_is_treated_as_inactive_responsible() -> None:
    client = FakeAttentionClient(
        deals=[_deal(350, title="Former employee")],
        users=[
            {
                "ID": "7",
                "NAME": "Former",
                "LAST_NAME": "Employee",
                "ACTIVE": False,
            }
        ],
        stages={
            0: [
                {"STATUS_ID": "NEW", "NAME": "Новая"},
                {"STATUS_ID": "PROPOSAL", "NAME": "КП/Договор"},
            ]
        },
        pending={
            350: [
                {
                    "ID": "1250",
                    "COMPLETED": "N",
                    "DEADLINE": _iso(NOW + timedelta(days=1)),
                }
            ]
        },
    )

    result = await DealAttentionService(client, now=NOW).get_deals_requiring_attention()

    assert result.deals[0].risk_score == 50
    assert result.deals[0].responsible == "Former Employee (неактивен)"
    assert [reason.code for reason in result.deals[0].reasons] == [
        RiskReasonCode.NO_RESPONSIBLE
    ]
    assert result.warnings == []


@pytest.mark.asyncio
async def test_per_deal_query_context_keeps_secondary_links_and_deduplicates_tasks() -> None:
    deals = [_deal(401, title="First linked deal"), _deal(402, title="Second linked deal")]
    shared_task = {
        "ID": "1301",
        "OWNER_TYPE_ID": "3",
        "OWNER_ID": "999",
        "TYPE_ID": "3",
        "COMPLETED": "N",
        "DEADLINE": _iso(NOW - timedelta(hours=1)),
    }
    client = FakeAttentionClient(
        deals=deals,
        users=[ACTIVE_USER],
        stages={
            0: [
                {"STATUS_ID": "NEW", "NAME": "Новая"},
                {"STATUS_ID": "PROPOSAL", "NAME": "КП отправлено"},
            ]
        },
        # The REST client associates these rows using the deal-specific query
        # context, even when an activity's primary owner is another CRM entity.
        pending={401: [shared_task, dict(shared_task)], 402: [shared_task]},
    )

    result = await DealAttentionService(client, now=NOW).get_deals_requiring_attention()

    assert [item.id for item in result.deals] == [401, 402]
    assert [item.risk_score for item in result.deals] == [50, 50]
    for item in result.deals:
        reasons = {reason.code: reason for reason in item.reasons}
        assert set(reasons) == {
            RiskReasonCode.NO_NEXT_ACTION,
            RiskReasonCode.OVERDUE_TASK,
        }
        assert reasons[RiskReasonCode.OVERDUE_TASK].details.endswith(": 1.")


@pytest.mark.asyncio
async def test_empty_open_deal_list_short_circuits_all_other_reads() -> None:
    client = FakeAttentionClient(deals=[])

    result = await DealAttentionService(client, now=NOW).get_deals_requiring_attention()

    assert result.deals == []
    assert result.warnings == []
    assert result.statistics.model_dump() == {
        "total_open_deals": 0,
        "requiring_attention": 0,
        "without_detected_risks": 0,
        "average_risk_score": 0.0,
        "maximum_risk_score": 0,
        "by_reason": {code.value: 0 for code in RiskReasonCode},
    }
    assert client.calls == [("list_open_deals", None)]
