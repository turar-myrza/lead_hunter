"""Risk analysis for open Bitrix24 deals, without any write operations."""

import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.core.exceptions import Bitrix24UnexpectedResponseError
from app.models.deal_attention import (
    DealAttentionItem,
    DealAttentionResponse,
    DealAttentionStatistics,
    RiskReason,
    RiskReasonCode,
)


NO_ACTIVITY_AFTER = timedelta(days=3)
LONG_STAGE_AFTER = timedelta(days=7)
STALE_PROPOSAL_AFTER = timedelta(days=5)

RISK_DEFINITIONS: Dict[RiskReasonCode, Tuple[str, int, str]] = {
    RiskReasonCode.NO_ACTIVITY: (
        "Нет активности более 3 дней",
        10,
        "Связаться с клиентом и зафиксировать результат контакта.",
    ),
    RiskReasonCode.NO_NEXT_ACTION: (
        "Нет следующего действия",
        20,
        "Запланировать следующее действие с конкретной датой и ответственным.",
    ),
    RiskReasonCode.OVERDUE_TASK: (
        "Есть просроченная задача",
        30,
        "Выполнить, перепланировать или закрыть просроченную задачу с результатом.",
    ),
    RiskReasonCode.LONG_IN_STAGE: (
        "Сделка находится на одной стадии более 7 дней",
        15,
        "Проверить блокер и перевести сделку на актуальную стадию.",
    ),
    RiskReasonCode.NO_REACTION_AFTER_PROPOSAL: (
        "Нет реакции после коммерческого предложения более 5 дней",
        15,
        "Сделать follow-up по коммерческому предложению и зафиксировать решение.",
    ),
    RiskReasonCode.OVERDUE_CLOSE_DATE: (
        "Плановая дата закрытия просрочена",
        25,
        "Обновить плановую дату закрытия и подтвердить реалистичный срок.",
    ),
    RiskReasonCode.NO_RESPONSIBLE: (
        "Нет ответственного",
        50,
        "Назначить активного ответственного за сделку.",
    ),
}

KP_NAME_PATTERN = re.compile(
    r"(?<!\w)кп(?!\w)|коммерческ\w*\s+предложен\w*|предложен\w*\s+отправлен\w*",
    re.IGNORECASE,
)
DEFAULT_TIMEZONE = ZoneInfo("Asia/Almaty")


class DealAttentionService:
    """Calculate the seven additive risks for every visible open deal."""

    def __init__(self, client: Any, now: Optional[datetime] = None) -> None:
        self._client = client
        effective_now = now or datetime.now(DEFAULT_TIMEZONE)
        if effective_now.tzinfo is None:
            raise ValueError("DealAttentionService requires a timezone-aware 'now'.")
        self._now = effective_now

    async def get_deals_requiring_attention(self) -> DealAttentionResponse:
        """Return risky deals sorted by score, with whole-pipeline statistics."""

        raw_deals = await self._client.list_open_deals()
        if not raw_deals:
            return self._empty_response()

        deal_ids = [self._required_positive_int(deal, "id") for deal in raw_deals]
        category_ids = sorted(
            {
                self._optional_int(
                    self._value(deal, "categoryId", "CATEGORY_ID")
                )
                or 0
                for deal in raw_deals
            }
        )
        assigned_ids = sorted(
            {
                assigned_id
                for deal in raw_deals
                if (assigned_id := self._optional_int(
                    self._value(deal, "assignedById", "ASSIGNED_BY_ID")
                ))
                is not None
                and assigned_id > 0
            }
        )

        raw_users = await self._client.list_users(assigned_ids)
        stage_groups: Dict[int, List[Dict[str, Any]]] = {}
        for category_id in category_ids:
            stage_groups[category_id] = await self._client.list_deal_stages(category_id)

        stage_names, kp_stages, missing_kp_categories = self._build_stage_metadata(
            stage_groups
        )
        pending_activities = await self._client.list_pending_deal_activities(deal_ids)
        warnings: List[str] = []

        kp_deal_ids = [
            self._required_positive_int(deal, "id")
            for deal in raw_deals
            if self._stage_key(deal) in kp_stages
        ]
        kp_history: List[Dict[str, Any]] = []
        kp_activities: Dict[int, List[Dict[str, Any]]] = {}
        if kp_deal_ids:
            kp_history = await self._client.list_deal_stage_history(kp_deal_ids)
            kp_activities = await self._client.list_deal_activities(kp_deal_ids)

        if missing_kp_categories:
            joined = ", ".join(str(category_id) for category_id in missing_kp_categories)
            warnings.append(
                "Не найдены стадии коммерческого предложения в воронках "
                f"categoryId: {joined}; критерий КП для них не вычислялся."
            )

        users = self._build_user_map(raw_users)
        pending_by_deal = pending_activities
        kp_activities_by_deal = kp_activities
        history_by_deal = self._group_by_owner(kp_history)

        scored_items: List[Tuple[DealAttentionItem, float]] = []
        all_scores: List[int] = []
        reason_counts: Counter[str] = Counter()

        for deal in raw_deals:
            deal_id = self._required_positive_int(deal, "id")
            pending = pending_by_deal.get(deal_id, [])
            reasons: List[RiskReason] = []

            last_activity = self._last_activity_time(deal)
            staleness_anchor = last_activity or self._datetime_field(
                deal, "createdTime", "CREATED_TIME"
            )
            if staleness_anchor and self._older_than(
                staleness_anchor, NO_ACTIVITY_AFTER
            ):
                age = self._age_days(staleness_anchor)
                self._add_reason(
                    reasons,
                    RiskReasonCode.NO_ACTIVITY,
                    f"Последняя активность была {age:.1f} дней назад.",
                )

            if not self._has_future_next_action(pending):
                self._add_reason(
                    reasons,
                    RiskReasonCode.NO_NEXT_ACTION,
                    "Не найдено незавершённого действия с будущей датой.",
                )

            overdue_task_count = self._overdue_task_count(pending)
            if overdue_task_count:
                self._add_reason(
                    reasons,
                    RiskReasonCode.OVERDUE_TASK,
                    f"Просроченных незавершённых задач: {overdue_task_count}.",
                )

            stage_since = self._datetime_field(
                deal, "movedTime", "MOVED_TIME"
            ) or self._datetime_field(deal, "createdTime", "CREATED_TIME")
            if stage_since and self._older_than(stage_since, LONG_STAGE_AFTER):
                age = self._age_days(stage_since)
                self._add_reason(
                    reasons,
                    RiskReasonCode.LONG_IN_STAGE,
                    f"Текущая стадия не менялась {age:.1f} дней.",
                )

            stage_key = self._stage_key(deal)
            if stage_key in kp_stages:
                proposal_time = self._proposal_entry_time(
                    deal,
                    history_by_deal.get(deal_id, []),
                    stage_key,
                )
                if proposal_time:
                    proposal_followup_anchor = self._last_activity_after(
                        kp_activities_by_deal.get(deal_id, []),
                        proposal_time,
                    ) or proposal_time
                    if self._older_than(
                        proposal_followup_anchor,
                        STALE_PROPOSAL_AFTER,
                    ):
                        age = self._age_days(proposal_followup_anchor)
                        self._add_reason(
                            reasons,
                            RiskReasonCode.NO_REACTION_AFTER_PROPOSAL,
                            "После коммерческого предложения последняя активность "
                            f"была {age:.1f} дней назад.",
                        )

            close_date = self._date_field(deal, "closedate", "CLOSEDATE")
            if close_date and close_date < self._now.date():
                overdue_days = (self._now.date() - close_date).days
                self._add_reason(
                    reasons,
                    RiskReasonCode.OVERDUE_CLOSE_DATE,
                    f"Плановая дата закрытия просрочена на {overdue_days} дней.",
                )

            assigned_id = self._optional_int(
                self._value(deal, "assignedById", "ASSIGNED_BY_ID")
            )
            responsible, responsible_is_valid = self._responsible_name(
                assigned_id, users
            )
            if not responsible_is_valid:
                self._add_reason(
                    reasons,
                    RiskReasonCode.NO_RESPONSIBLE,
                    "Ответственный отсутствует, не найден или деактивирован.",
                )

            score = sum(reason.weight for reason in reasons)
            all_scores.append(score)
            reason_counts.update(reason.code.value for reason in reasons)
            if not reasons:
                continue

            reasons.sort(key=lambda reason: (-reason.weight, reason.code.value))
            recommendations = [
                RISK_DEFINITIONS[reason.code][2] for reason in reasons
            ]
            stage = stage_names.get(stage_key, stage_key[1] or "Неизвестная стадия")
            title = str(self._value(deal, "title", "TITLE") or "Сделка без названия")
            stale_seconds = (
                (self._now - self._normalize_datetime(staleness_anchor)).total_seconds()
                if staleness_anchor
                else float("inf")
            )
            scored_items.append(
                (
                    DealAttentionItem(
                        id=deal_id,
                        title=title,
                        responsible=responsible,
                        stage=stage,
                        risk_score=score,
                        reasons=reasons,
                        recommendations=recommendations,
                    ),
                    stale_seconds,
                )
            )

        scored_items.sort(
            key=lambda entry: (-entry[0].risk_score, -entry[1], entry[0].id)
        )
        items = [entry[0] for entry in scored_items]
        total = len(raw_deals)
        maximum = max(all_scores, default=0)
        return DealAttentionResponse(
            generated_at=self._now,
            deals=items,
            statistics=DealAttentionStatistics(
                total_open_deals=total,
                requiring_attention=len(items),
                without_detected_risks=total - len(items),
                average_risk_score=round(sum(all_scores) / total, 2),
                maximum_risk_score=maximum,
                by_reason={
                    code.value: reason_counts.get(code.value, 0)
                    for code in RiskReasonCode
                },
            ),
            warnings=warnings,
        )

    def _empty_response(self) -> DealAttentionResponse:
        return DealAttentionResponse(
            generated_at=self._now,
            deals=[],
            statistics=DealAttentionStatistics(
                total_open_deals=0,
                requiring_attention=0,
                without_detected_risks=0,
                average_risk_score=0,
                maximum_risk_score=0,
                by_reason={code.value: 0 for code in RiskReasonCode},
            ),
            warnings=[],
        )

    @classmethod
    def _build_stage_metadata(
        cls,
        stage_groups: Mapping[int, Sequence[Mapping[str, Any]]],
    ) -> Tuple[Dict[Tuple[int, str], str], set[Tuple[int, str]], List[int]]:
        names: Dict[Tuple[int, str], str] = {}
        kp_stages: set[Tuple[int, str]] = set()
        missing: List[int] = []
        for category_id, stages in stage_groups.items():
            found_kp = False
            for stage in stages:
                stage_id = str(cls._value(stage, "STATUS_ID", "statusId") or "")
                if not stage_id:
                    continue
                stage_name = str(cls._value(stage, "NAME", "name") or stage_id)
                key = (category_id, stage_id)
                names[key] = stage_name
                if KP_NAME_PATTERN.search(stage_name):
                    kp_stages.add(key)
                    found_kp = True
            if not found_kp:
                missing.append(category_id)
        return names, kp_stages, missing

    @classmethod
    def _build_user_map(
        cls, raw_users: Sequence[Mapping[str, Any]]
    ) -> Dict[int, Tuple[str, bool]]:
        users: Dict[int, Tuple[str, bool]] = {}
        for user in raw_users:
            user_id = cls._optional_int(cls._value(user, "ID", "id"))
            if not user_id:
                continue
            parts = [
                str(cls._value(user, *field_names) or "").strip()
                for field_names in (
                    ("NAME", "name"),
                    ("SECOND_NAME", "secondName"),
                    ("LAST_NAME", "lastName"),
                )
            ]
            name = " ".join(part for part in parts if part) or (
                f"Пользователь #{user_id}"
            )
            active_value = cls._value(user, "ACTIVE", "active")
            if active_value is None:
                is_active = True
            elif isinstance(active_value, bool):
                is_active = active_value
            else:
                is_active = str(active_value).upper() not in {"N", "0", "FALSE"}
            users[user_id] = (name, is_active)
        return users

    @classmethod
    def _group_by_owner(
        cls, records: Sequence[Mapping[str, Any]]
    ) -> Dict[int, List[Dict[str, Any]]]:
        grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            owner_id = cls._optional_int(cls._value(record, "OWNER_ID", "ownerId"))
            if owner_id:
                grouped[owner_id].append(dict(record))
        return grouped

    def _has_future_next_action(
        self,
        pending_activities: Sequence[Mapping[str, Any]],
    ) -> bool:
        for activity in pending_activities:
            if self._is_completed_activity(activity):
                continue
            scheduled = self._datetime_field(
                activity, "DEADLINE", "deadline", "START_TIME", "startTime"
            )
            if scheduled and self._normalize_datetime(scheduled) > self._now:
                return True
        return False

    def _overdue_task_count(
        self,
        pending_activities: Sequence[Mapping[str, Any]],
    ) -> int:
        overdue_ids: set[str] = set()
        for activity in pending_activities:
            if (
                not self._is_task_activity(activity)
                or self._is_completed_activity(activity)
            ):
                continue
            deadline = self._datetime_field(activity, "DEADLINE", "deadline")
            if deadline and self._normalize_datetime(deadline) < self._now:
                overdue_ids.add(
                    str(self._value(activity, "ID", "id") or id(activity))
                )
        return len(overdue_ids)

    def _proposal_entry_time(
        self,
        deal: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
        stage_key: Tuple[int, str],
    ) -> Optional[datetime]:
        entries: List[datetime] = []
        for event in history:
            category_id = self._optional_int(
                self._value(event, "CATEGORY_ID", "categoryId")
            )
            stage_id = str(self._value(event, "STAGE_ID", "stageId") or "")
            if (category_id or 0, stage_id) != stage_key:
                continue
            event_time = self._datetime_field(event, "CREATED_TIME", "createdTime")
            if event_time:
                entries.append(event_time)
        if entries:
            return max(entries, key=self._normalize_datetime)
        return self._datetime_field(deal, "movedTime", "MOVED_TIME")

    def _last_activity_after(
        self,
        activities: Sequence[Mapping[str, Any]],
        proposal_time: datetime,
    ) -> Optional[datetime]:
        after: List[datetime] = []
        proposal_normalized = self._normalize_datetime(proposal_time)
        for activity in activities:
            activity_times = [
                parsed
                for field_names in (
                    ("START_TIME", "startTime"),
                    ("CREATED", "created"),
                    ("END_TIME", "endTime"),
                )
                if (parsed := self._datetime_field(activity, *field_names))
                and self._normalize_datetime(parsed) <= self._now
            ]
            if not activity_times:
                continue
            activity_time = max(activity_times, key=self._normalize_datetime)
            if self._normalize_datetime(activity_time) > proposal_normalized:
                after.append(activity_time)
        if not after:
            return None
        return max(after, key=self._normalize_datetime)

    def _last_activity_time(self, deal: Mapping[str, Any]) -> Optional[datetime]:
        return self._datetime_field(deal, "lastActivityTime", "LAST_ACTIVITY_TIME")

    def _older_than(self, value: datetime, threshold: timedelta) -> bool:
        return self._now - self._normalize_datetime(value) > threshold

    def _age_days(self, value: datetime) -> float:
        seconds = (self._now - self._normalize_datetime(value)).total_seconds()
        return max(0.0, seconds / 86400)

    @classmethod
    def _is_task_activity(cls, activity: Mapping[str, Any]) -> bool:
        type_id = str(cls._value(activity, "TYPE_ID", "typeId") or "")
        provider = str(cls._value(activity, "PROVIDER_ID", "providerId") or "")
        return type_id == "3" or provider.upper() == "CRM_TASKS_TASK"

    @classmethod
    def _is_completed_activity(cls, activity: Mapping[str, Any]) -> bool:
        return str(cls._value(activity, "COMPLETED", "completed") or "N").upper() == "Y"

    @classmethod
    def _responsible_name(
        cls,
        assigned_id: Optional[int],
        users: Mapping[int, Tuple[str, bool]],
    ) -> Tuple[str, bool]:
        if not assigned_id or assigned_id <= 0:
            return "Не назначен", False
        user = users.get(assigned_id)
        if user is None:
            return f"Пользователь #{assigned_id} (не найден)", False
        name, active = user
        if not active:
            return f"{name} (неактивен)", False
        return name, True

    @classmethod
    def _stage_key(cls, deal: Mapping[str, Any]) -> Tuple[int, str]:
        category_id = cls._optional_int(
            cls._value(deal, "categoryId", "CATEGORY_ID")
        ) or 0
        stage_id = str(cls._value(deal, "stageId", "STAGE_ID") or "")
        return category_id, stage_id

    @staticmethod
    def _add_reason(
        reasons: List[RiskReason], code: RiskReasonCode, details: str
    ) -> None:
        label, weight, _ = RISK_DEFINITIONS[code]
        reasons.append(
            RiskReason(code=code, label=label, weight=weight, details=details)
        )

    def _datetime_field(
        self, mapping: Mapping[str, Any], *field_names: str
    ) -> Optional[datetime]:
        for field_name in field_names:
            value = self._value(mapping, field_name)
            if value not in (None, ""):
                return self._parse_datetime(value)
        return None

    def _date_field(
        self, mapping: Mapping[str, Any], *field_names: str
    ) -> Optional[date]:
        for field_name in field_names:
            value = self._value(mapping, field_name)
            if value in (None, ""):
                continue
            if isinstance(value, date) and not isinstance(value, datetime):
                return value
            parsed = self._parse_datetime(value)
            return self._normalize_datetime(parsed).date()
        return None

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return self._normalize_datetime(value)
        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=self._now.tzinfo)
        raw = str(value).strip()
        try:
            if len(raw) == 10:
                parsed = datetime.combine(date.fromisoformat(raw), time.min)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Bitrix24UnexpectedResponseError(
                "Bitrix24 returned an invalid date or datetime value."
            ) from exc
        return self._normalize_datetime(parsed)

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self._now.tzinfo)
        return value.astimezone(self._now.tzinfo)

    @staticmethod
    def _value(mapping: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in mapping:
                return mapping[name]
        return None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _required_positive_int(cls, mapping: Mapping[str, Any], field: str) -> int:
        value = cls._value(mapping, field, field.upper())
        parsed = cls._optional_int(value)
        if parsed is None or parsed <= 0:
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 returned a deal without a valid {field}."
            )
        return parsed
