"""Tests for the management-first Executive Report presentation layer."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.deal_attention import (
    DealAttentionItem,
    DealAttentionResponse,
    DealAttentionStatistics,
    RiskReason,
    RiskReasonCode,
)
from app.models.executive_report import EvidenceLevel, ExecutivePriority
from app.services.executive_report import ExecutiveReportPresenter


ALMATY = timezone(timedelta(hours=5))
GENERATED_AT = datetime(2026, 8, 21, 12, 0, tzinfo=ALMATY)


def _reason(
    code: RiskReasonCode = RiskReasonCode.NO_NEXT_ACTION,
    *,
    label: str = "Нет следующего действия",
    details: str = "В CRM не найдено следующего действия с будущей датой.",
    weight: int = 20,
) -> RiskReason:
    return RiskReason(code=code, label=label, details=details, weight=weight)


def _deal(
    deal_id: int,
    score: int,
    *,
    reason: RiskReason | None = None,
    recommendation: str | None = None,
) -> DealAttentionItem:
    return DealAttentionItem(
        id=deal_id,
        title=f"Сделка {deal_id}",
        responsible=f"Менеджер {deal_id}",
        stage="Переговоры",
        risk_score=score,
        reasons=[reason or _reason()],
        recommendations=[
            recommendation or f"Назначить следующее действие по сделке {deal_id}."
        ],
    )


def _response(
    deals: list[DealAttentionItem],
    *,
    warnings: list[str] | None = None,
    total_open_deals: int | None = None,
    by_reason: dict[str, int] | None = None,
) -> DealAttentionResponse:
    total = len(deals) if total_open_deals is None else total_open_deals
    reason_counts = {code.value: 0 for code in RiskReasonCode}
    if by_reason is None:
        for deal in deals:
            for reason in deal.reasons:
                reason_counts[reason.code.value] += 1
    else:
        reason_counts.update(by_reason)
    scores = [deal.risk_score for deal in deals]
    return DealAttentionResponse(
        generated_at=GENERATED_AT,
        deals=deals,
        statistics=DealAttentionStatistics(
            total_open_deals=total,
            requiring_attention=len(deals),
            without_detected_risks=max(total - len(deals), 0),
            average_risk_score=(round(sum(scores) / total, 2) if total else 0),
            maximum_risk_score=max(scores, default=0),
            by_reason=reason_counts,
        ),
        warnings=warnings or [],
    )


@pytest.mark.parametrize(
    ("score", "expected_priority"),
    [
        (29, ExecutivePriority.LOW),
        (30, ExecutivePriority.MEDIUM),
        (49, ExecutivePriority.MEDIUM),
        (50, ExecutivePriority.HIGH),
        (99, ExecutivePriority.HIGH),
        (100, ExecutivePriority.CRITICAL),
    ],
)
def test_priority_boundaries(
    score: int,
    expected_priority: ExecutivePriority,
) -> None:
    report = ExecutiveReportPresenter.build(_response([_deal(1, score)]))

    assert report.attention_items[0].priority is expected_priority
    assert report.attention_items[0].priority.label


def test_attention_and_actions_are_sorted_and_limited_to_five() -> None:
    source = _response(
        [
            _deal(1, 40),
            _deal(2, 165),
            _deal(3, 70),
            _deal(4, 20),
            _deal(5, 100),
            _deal(6, 99),
            _deal(7, 50),
        ]
    )

    report = ExecutiveReportPresenter.build(source)

    assert [item.deal_id for item in report.attention_items] == [2, 5, 6, 3, 7]
    assert [item.risk_score for item in report.attention_items] == [165, 100, 99, 70, 50]
    assert len(report.attention_items) == 5
    assert len(report.actions_today) == 5
    for item in report.attention_items:
        assert any(item.client_or_deal in action for action in report.actions_today)


def test_report_is_short_and_offers_progressive_disclosure() -> None:
    report = ExecutiveReportPresenter.build(
        _response([_deal(1, 100), _deal(2, 50)], total_open_deals=10)
    )

    assert 2 <= len(report.main_conclusion) <= 4
    assert report.details_available
    assert "подроб" in report.details_available.lower()

    management_text = " ".join(report.main_conclusion).lower()
    assert "api" not in management_text
    assert "rest" not in management_text
    assert "запрос" not in management_text


def test_systemic_overload_recommends_top_five_instead_of_all_deals_today() -> None:
    source = _response(
        [_deal(deal_id, 50) for deal_id in range(1, 61)],
        total_open_deals=100,
    )

    report = ExecutiveReportPresenter.build(source)
    conclusion = " ".join(report.main_conclusion).lower()

    assert "невозможно" in conclusion
    assert "пяти верхних" in conclusion
    assert len(report.attention_items) == 5
    assert len(report.actions_today) == 5


def test_evidence_is_labeled_and_only_repeated_problem_is_systemic() -> None:
    no_next_action = _reason()
    overdue_task = _reason(
        RiskReasonCode.OVERDUE_TASK,
        label="Есть просроченная задача",
        details="В CRM найдена одна просроченная задача.",
        weight=30,
    )
    source = _response(
        [
            _deal(1, 50, reason=no_next_action),
            _deal(2, 40, reason=no_next_action),
            _deal(3, 30, reason=overdue_task),
        ],
        by_reason={
            RiskReasonCode.NO_NEXT_ACTION.value: 2,
            RiskReasonCode.OVERDUE_TASK.value: 1,
        },
    )

    report = ExecutiveReportPresenter.build(source)

    evidence_by_deal = {
        item.deal_id: item.evidence_level for item in report.attention_items
    }
    assert evidence_by_deal == {
        1: EvidenceLevel.RISK,
        2: EvidenceLevel.RISK,
        3: EvidenceLevel.FACT,
    }
    assert len(report.systemic_issues) == 1
    issue = report.systemic_issues[0]
    assert issue.affected_count == 2
    assert "следующ" in f"{issue.title} {issue.detail}".lower()
    assert issue.evidence_level is EvidenceLevel.RISK


def test_systemic_issues_are_ranked_by_weighted_management_impact() -> None:
    source = _response(
        [_deal(1, 50)],
        total_open_deals=100,
        by_reason={
            RiskReasonCode.NO_ACTIVITY.value: 100,
            RiskReasonCode.NO_NEXT_ACTION.value: 60,
            RiskReasonCode.NO_RESPONSIBLE.value: 25,
        },
    )

    report = ExecutiveReportPresenter.build(source)

    assert [issue.affected_count for issue in report.systemic_issues] == [25, 60, 100]


def test_source_warnings_are_preserved_as_data_limitations() -> None:
    warning = (
        "По части сделок история коммерческого предложения недоступна; "
        "этот критерий для них не вычислялся."
    )

    report = ExecutiveReportPresenter.build(
        _response([_deal(1, 20)], warnings=[warning])
    )

    assert report.data_limitations[0] == warning
    assert any("не доказывает" in item for item in report.data_limitations)


def test_empty_pipeline_produces_a_valid_executive_report() -> None:
    report = ExecutiveReportPresenter.build(_response([]))

    assert report.attention_items == []
    assert report.actions_today == []
    assert report.systemic_issues == []
    assert report.data_limitations == []
    assert 2 <= len(report.main_conclusion) <= 4
    assert report.statistics.total_open_deals == 0
    assert report.statistics.requiring_attention == 0
    assert report.statistics.critical_priority == 0
    assert report.statistics.high_priority == 0
    assert report.statistics.medium_priority == 0
    assert report.statistics.low_priority == 0
    assert report.statistics.without_detected_risks == 0


def test_report_does_not_turn_missing_crm_records_into_manager_accusations() -> None:
    limitation = "История коммуникаций по этой выборке недоступна."
    source = _response(
        [
            _deal(
                9,
                10,
                reason=_reason(
                    RiskReasonCode.NO_ACTIVITY,
                    label="Нет активности более 3 дней",
                    details="Последняя зарегистрированная активность была 4 дня назад.",
                    weight=10,
                ),
                recommendation="Проверить фактический контакт и зафиксировать результат.",
            )
        ],
        warnings=[limitation],
    )

    report = ExecutiveReportPresenter.build(source)
    rendered = str(report.model_dump()).lower()

    assert report.attention_items[0].evidence_level is EvidenceLevel.RISK
    assert report.data_limitations[0] == limitation
    assert any("не доказывает" in item for item in report.data_limitations)
    assert "менеджер не работал" not in rendered
    assert "менеджер не звонил" not in rendered
    assert "клиента не обрабатывали" not in rendered
    assert EvidenceLevel.ASSUMPTION.value not in rendered
