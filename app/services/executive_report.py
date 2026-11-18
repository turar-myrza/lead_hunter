"""Present existing analytics as a concise executive report.

This module does not read from or write to Bitrix24.  It deterministically
transforms the already calculated deal-attention response.
"""

from collections import Counter
from math import ceil

from app.models.deal_attention import (
    DealAttentionItem,
    DealAttentionResponse,
    RiskReasonCode,
)
from app.models.executive_report import (
    EvidenceLevel,
    ExecutiveAttentionItem,
    ExecutivePriority,
    ExecutiveReport,
    ExecutiveStatistics,
    SystemIssue,
)
from app.services.deal_attention import RISK_DEFINITIONS


_SYSTEM_ISSUE_TEXT: dict[RiskReasonCode, tuple[str, str, EvidenceLevel]] = {
    RiskReasonCode.NO_ACTIVITY: (
        "В CRM давно не зафиксирована активность",
        "В {count} сделках последняя зарегистрированная активность старше трёх "
        "дней. Это сигнал проверить коммуникации, а не доказательство отсутствия "
        "работы менеджеров.",
        EvidenceLevel.RISK,
    ),
    RiskReasonCode.NO_NEXT_ACTION: (
        "Не зафиксировано следующее действие",
        "В {count} сделках CRM не содержит будущего незавершённого действия. "
        "Необходимо проверить план работы по этим сделкам.",
        EvidenceLevel.RISK,
    ),
    RiskReasonCode.OVERDUE_TASK: (
        "Повторяются просроченные задачи",
        "В {count} сделках зарегистрирована как минимум одна просроченная "
        "незавершённая задача.",
        EvidenceLevel.FACT,
    ),
    RiskReasonCode.LONG_IN_STAGE: (
        "Сделки задерживаются на текущей стадии",
        "В {count} сделках стадия по данным CRM не менялась более семи дней.",
        EvidenceLevel.FACT,
    ),
    RiskReasonCode.NO_REACTION_AFTER_PROPOSAL: (
        "Не зафиксирован своевременный follow-up после КП",
        "В {count} сделках после коммерческого предложения более пяти дней "
        "не зарегистрировано новой активности. Фактическую коммуникацию нужно "
        "подтвердить у ответственного.",
        EvidenceLevel.RISK,
    ),
    RiskReasonCode.OVERDUE_CLOSE_DATE: (
        "Просрочены плановые даты закрытия",
        "В {count} сделках плановая дата закрытия уже прошла.",
        EvidenceLevel.FACT,
    ),
    RiskReasonCode.NO_RESPONSIBLE: (
        "Сделки остаются без активного ответственного",
        "В {count} сделках ответственный отсутствует, не найден или неактивен.",
        EvidenceLevel.FACT,
    ),
}

_SYSTEM_ISSUE_WEIGHT = {
    title: RISK_DEFINITIONS[code][1]
    for code, (title, _, _) in _SYSTEM_ISSUE_TEXT.items()
}

_CRM_RECORDING_RISKS = frozenset(
    {
        RiskReasonCode.NO_ACTIVITY,
        RiskReasonCode.NO_NEXT_ACTION,
        RiskReasonCode.NO_REACTION_AFTER_PROPOSAL,
    }
)

_CRM_RECORDING_LIMITATION = (
    "Отсутствие зарегистрированной активности, следующего действия или реакции "
    "в CRM не доказывает отсутствие работы вне системы."
)


class ExecutiveReportPresenter:
    """Build a management-first view without changing analytical logic."""

    DEFAULT_MAX_ITEMS = 5
    MAX_ITEMS = 10

    @classmethod
    def build(
        cls,
        response: DealAttentionResponse,
        *,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> ExecutiveReport:
        """Convert deal-attention analytics into a short executive report."""

        if not 1 <= max_items <= cls.MAX_ITEMS:
            raise ValueError(
                f"max_items должен быть в диапазоне от 1 до {cls.MAX_ITEMS}."
            )

        sorted_deals = sorted(
            response.deals,
            key=lambda deal: (-deal.risk_score, deal.id),
        )
        priority_counts = Counter(
            cls._priority_for_score(deal.risk_score) for deal in sorted_deals
        )
        visible_deals = sorted_deals[:max_items]
        attention_items = [
            cls._attention_item(deal) for deal in visible_deals
        ]
        actions_today = cls._actions_today(visible_deals)
        data_limitations = cls._data_limitations(response)
        statistics = ExecutiveStatistics(
            total_open_deals=response.statistics.total_open_deals,
            requiring_attention=response.statistics.requiring_attention,
            critical_priority=priority_counts[ExecutivePriority.CRITICAL],
            high_priority=priority_counts[ExecutivePriority.HIGH],
            medium_priority=priority_counts[ExecutivePriority.MEDIUM],
            low_priority=priority_counts[ExecutivePriority.LOW],
            without_detected_risks=response.statistics.without_detected_risks,
        )

        return ExecutiveReport(
            generated_at=response.generated_at,
            main_conclusion=cls._main_conclusion(statistics, bool(data_limitations)),
            attention_items=attention_items,
            actions_today=actions_today,
            systemic_issues=cls._systemic_issues(response),
            statistics=statistics,
            data_limitations=data_limitations,
            details_available=(
                "Могу показать подробный список сделок, разобрать причины "
                "высокого приоритета или сгруппировать результаты по менеджерам."
            ),
        )

    @staticmethod
    def _priority_for_score(score: int) -> ExecutivePriority:
        if score >= 100:
            return ExecutivePriority.CRITICAL
        if score >= 50:
            return ExecutivePriority.HIGH
        if score >= 30:
            return ExecutivePriority.MEDIUM
        return ExecutivePriority.LOW

    @classmethod
    def _attention_item(cls, deal: DealAttentionItem) -> ExecutiveAttentionItem:
        reason_labels = [reason.label for reason in deal.reasons]
        visible_reasons = reason_labels[:3]
        why_attention = "По данным CRM: " + "; ".join(visible_reasons)
        if len(reason_labels) > len(visible_reasons):
            why_attention += f"; ещё причин: {len(reason_labels) - len(visible_reasons)}"

        recommended_action = (
            deal.recommendations[0]
            if deal.recommendations
            else "Проверить карточку сделки и определить следующее конкретное действие."
        )
        priority = cls._priority_for_score(deal.risk_score)
        evidence_level = (
            EvidenceLevel.RISK
            if any(reason.code in _CRM_RECORDING_RISKS for reason in deal.reasons)
            else EvidenceLevel.FACT
        )
        return ExecutiveAttentionItem(
            deal_id=deal.id,
            client_or_deal=deal.title,
            responsible=deal.responsible,
            stage=deal.stage,
            risk_score=deal.risk_score,
            priority=priority,
            priority_label=priority.label,
            why_attention=why_attention,
            recommended_action=recommended_action,
            evidence_level=evidence_level,
            evidence_label=evidence_level.label,
        )

    @classmethod
    def _actions_today(cls, deals: list[DealAttentionItem]) -> list[str]:
        actions: list[str] = []
        for deal in deals:
            if len(actions) == 5:
                break
            if cls._priority_for_score(deal.risk_score) not in {
                ExecutivePriority.CRITICAL,
                ExecutivePriority.HIGH,
            }:
                continue
            recommendation = (
                deal.recommendations[0]
                if deal.recommendations
                else "проверить карточку и определить следующее действие"
            )
            actions.append(
                f"По сделке «{deal.title}» (№{deal.id}) — "
                f"{cls._lowercase_first(recommendation)}"
            )
        return actions

    @staticmethod
    def _lowercase_first(value: str) -> str:
        if not value:
            return value
        return value[0].lower() + value[1:]

    @staticmethod
    def _main_conclusion(
        statistics: ExecutiveStatistics,
        has_limitations: bool,
    ) -> list[str]:
        conclusions = [
            "По данным CRM внимания "
            f"{'требует' if statistics.requiring_attention == 1 else 'требуют'} "
            f"{statistics.requiring_attention} из "
            f"{statistics.total_open_deals} открытых сделок."
        ]
        urgent_count = statistics.critical_priority + statistics.high_priority
        urgent_share = (
            urgent_count / statistics.total_open_deals
            if statistics.total_open_deals
            else 0
        )
        is_systemic_overload = urgent_count > 50 and urgent_share >= 0.5
        if is_systemic_overload:
            conclusions.append(
                f"Критический приоритет: {statistics.critical_priority}; "
                f"высокий: {statistics.high_priority}. Такой объём невозможно "
                "качественно обработать сделка за сделкой за один день."
            )
        elif statistics.critical_priority:
            conclusions.append(
                f"Критический приоритет: {statistics.critical_priority}. "
                "Руководителю необходимо вмешаться сегодня."
            )
        elif statistics.high_priority:
            conclusions.append(
                f"Высокий приоритет: {statistics.high_priority}. "
                "Эти сделки необходимо отработать сегодня."
            )
        else:
            conclusions.append(
                "Критических и высокоприоритетных сделок не обнаружено; "
                "срочное вмешательство сегодня не требуется."
            )

        if is_systemic_overload:
            conclusions.append(
                "Сегодня следует начать с пяти верхних сделок и отдельно "
                "организовать проверку актуальности накопленной открытой воронки."
            )
        elif statistics.requiring_attention:
            conclusions.append(
                f"Сделок, требующих действия сегодня: {urgent_count}. Остальные "
                "можно обработать в порядке приоритета."
            )
        else:
            conclusions.append(
                "По текущим критериям дополнительных действий по открытым сделкам "
                "не требуется."
            )

        if has_limitations:
            conclusions.append(
                "Достоверность части выводов ограничена полнотой данных CRM; "
                "ограничения приведены отдельно."
            )
        return conclusions

    @classmethod
    def _systemic_issues(
        cls,
        response: DealAttentionResponse,
    ) -> list[SystemIssue]:
        threshold = cls._systemic_threshold(
            response.statistics.total_open_deals
        )
        issues: list[SystemIssue] = []
        for code in RiskReasonCode:
            count = response.statistics.by_reason.get(code.value, 0)
            if count < threshold:
                continue
            title, detail_template, evidence_level = _SYSTEM_ISSUE_TEXT[code]
            issues.append(
                SystemIssue(
                    title=title,
                    detail=detail_template.format(count=count),
                    evidence_level=evidence_level,
                    evidence_label=evidence_level.label,
                    affected_count=count,
                )
            )
        issues.sort(
            key=lambda issue: (
                -issue.affected_count * _SYSTEM_ISSUE_WEIGHT[issue.title],
                -issue.affected_count,
                issue.title,
            )
        )
        return issues[:3]

    @staticmethod
    def _systemic_threshold(total_open_deals: int) -> int:
        """Require repetition and scale the threshold with the analysed volume."""

        return max(2, min(25, ceil(total_open_deals * 0.10)))

    @staticmethod
    def _data_limitations(response: DealAttentionResponse) -> list[str]:
        limitations = list(dict.fromkeys(response.warnings))
        has_recording_risk = any(
            reason.code in _CRM_RECORDING_RISKS
            for deal in response.deals
            for reason in deal.reasons
        )
        if has_recording_risk and _CRM_RECORDING_LIMITATION not in limitations:
            limitations.append(_CRM_RECORDING_LIMITATION)
        return limitations[:5]
