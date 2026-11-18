"""Read-only deal retrieval and presentation logic."""

import asyncio
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from pydantic import ValidationError

from app.core.exceptions import Bitrix24UnexpectedResponseError
from app.integrations.bitrix24 import Bitrix24Client
from app.models.deal import DealSummary


class DealService:
    """Build a small, readable view of the latest modified deals."""

    def __init__(self, client: Bitrix24Client) -> None:
        self._client = client

    async def get_latest_deals(self, limit: int = 10) -> List[DealSummary]:
        """Return up to ten deals, newest modification first."""

        if not 1 <= limit <= 10:
            raise ValueError("The first scenario supports a limit from 1 to 10.")

        raw_deals = (await self._client.list_latest_deals_page())[:limit]
        if not raw_deals:
            return []

        category_ids = self._collect_category_ids(raw_deals)
        user_ids = self._collect_user_ids(raw_deals)

        enrichment_results = await asyncio.gather(
            self._client.list_users(user_ids),
            *(self._client.list_deal_stages(category_id) for category_id in category_ids),
        )
        raw_users = enrichment_results[0]
        raw_stage_groups = enrichment_results[1:]

        user_names = self._build_user_name_map(raw_users)
        stage_names = self._build_stage_name_map(category_ids, raw_stage_groups)

        deals: List[DealSummary] = []
        for raw_deal in raw_deals:
            deal_id = self._required_int(raw_deal, "id")
            category_id = self._optional_int(raw_deal.get("categoryId")) or 0
            assigned_by_id = self._optional_int(raw_deal.get("assignedById"))
            stage_id = str(raw_deal.get("stageId") or "Unknown stage")
            updated_time = raw_deal.get("updatedTime")
            if not updated_time:
                raise Bitrix24UnexpectedResponseError(
                    "A deal is missing the expected updatedTime field."
                )

            responsible = "Not assigned"
            if assigned_by_id is not None:
                responsible = user_names.get(
                    assigned_by_id,
                    f"User #{assigned_by_id}",
                )

            try:
                deals.append(
                    DealSummary(
                        id=deal_id,
                        title=str(raw_deal.get("title") or "Untitled deal"),
                        stage=stage_names.get((category_id, stage_id), stage_id),
                        responsible=responsible,
                        updated_at=updated_time,
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise Bitrix24UnexpectedResponseError(
                    "A deal contains an invalid value in a required field."
                ) from exc

        return deals

    @classmethod
    def _collect_category_ids(cls, raw_deals: Sequence[Dict[str, Any]]) -> List[int]:
        return sorted(
            {
                cls._optional_int(raw_deal.get("categoryId")) or 0
                for raw_deal in raw_deals
            }
        )

    @classmethod
    def _collect_user_ids(cls, raw_deals: Sequence[Dict[str, Any]]) -> Set[int]:
        user_ids: Set[int] = set()
        for raw_deal in raw_deals:
            user_id = cls._optional_int(raw_deal.get("assignedById"))
            if user_id is not None:
                user_ids.add(user_id)
        return user_ids

    @classmethod
    def _build_user_name_map(
        cls,
        raw_users: Sequence[Dict[str, Any]],
    ) -> Dict[int, str]:
        names: Dict[int, str] = {}
        for raw_user in raw_users:
            user_id = cls._required_int(raw_user, "ID")
            name_parts = [
                str(raw_user.get(field) or "").strip()
                for field in ("NAME", "SECOND_NAME", "LAST_NAME")
            ]
            full_name = " ".join(part for part in name_parts if part)
            names[user_id] = full_name or f"User #{user_id}"
        return names

    @staticmethod
    def _build_stage_name_map(
        category_ids: Sequence[int],
        raw_stage_groups: Sequence[List[Dict[str, Any]]],
    ) -> Dict[Tuple[int, str], str]:
        names: Dict[Tuple[int, str], str] = {}
        for category_id, raw_stages in zip(category_ids, raw_stage_groups):
            for raw_stage in raw_stages:
                stage_id = str(raw_stage.get("STATUS_ID") or "").strip()
                if not stage_id:
                    continue
                stage_name = str(raw_stage.get("NAME") or stage_id).strip()
                names[(category_id, stage_id)] = stage_name or stage_id
        return names

    @staticmethod
    def _required_int(item: Dict[str, Any], field_name: str) -> int:
        value = item.get(field_name)
        if value in (None, ""):
            raise Bitrix24UnexpectedResponseError(
                f"A Bitrix24 item is missing the expected {field_name} field."
            )
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 field {field_name} is not a valid integer."
            ) from exc

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise Bitrix24UnexpectedResponseError(
                "Bitrix24 returned an invalid numeric identifier."
            ) from exc
