"""Minimal, explicitly read-only Bitrix24 REST client."""

import asyncio
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import httpx

from app.core.exceptions import (
    Bitrix24APIError,
    Bitrix24AuthorizationError,
    Bitrix24ConfigurationError,
    Bitrix24EmptyResponseError,
    Bitrix24NetworkError,
    Bitrix24ReadOnlyViolationError,
    Bitrix24UnexpectedResponseError,
)


READ_ONLY_METHODS = frozenset(
    {
        "crm.activity.list",
        "crm.item.list",
        "crm.stagehistory.list",
        "crm.status.list",
        "user.get",
    }
)

PAGE_SIZE = 50
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
RETRYABLE_ERROR_CODES = frozenset(
    {
        "QUERY_LIMIT_EXCEEDED",
        "TOO_MANY_REQUESTS",
    }
)
BATCH_READ_ONLY_METHODS = frozenset({"crm.activity.list"})

AUTHORIZATION_ERROR_CODES = frozenset(
    {
        "ACCESS_DENIED",
        "ALLOWED_ONLY_INTRANET_USER",
        "EXPIRED_TOKEN",
        "INSUFFICIENT_SCOPE",
        "INVALID_CREDENTIALS",
        "NO_AUTH_FOUND",
        "WRONG_AUTH_TYPE",
    }
)


class Bitrix24Client:
    """Call only the explicitly allowlisted read methods used by the app."""

    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        retry_backoff_seconds: float = 0.25,
        max_retries: int = 3,
        batch_pause_seconds: float = 0.5,
        page_pause_seconds: float = 0.5,
    ) -> None:
        base_url = webhook_url.strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise Bitrix24ConfigurationError(
                "The Bitrix24 webhook URL must use HTTPS."
            )

        self._base_url = base_url
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._max_retries = max(0, max_retries)
        self._batch_pause_seconds = max(0.0, batch_pause_seconds)
        self._page_pause_seconds = max(0.0, page_pause_seconds)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "CRM-Consulting-CRM-Analyst/0.1",
            },
        )

    async def __aenter__(self) -> "Bitrix24Client":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close network resources."""

        await self._http.aclose()

    async def list_latest_deals_page(self) -> List[Dict[str, Any]]:
        """Get the first page of deals ordered by modification time."""

        payload = await self._call_read_only(
            "crm.item.list",
            {
                "entityTypeId": 2,
                "select": [
                    "id",
                    "title",
                    "stageId",
                    "categoryId",
                    "assignedById",
                    "updatedTime",
                ],
                "order": {"updatedTime": "DESC", "id": "DESC"},
                "start": 0,
            },
        )
        result = self._require_mapping(payload.get("result"), "result")
        return self._require_list_of_mappings(result.get("items"), "result.items")

    async def list_open_deals(self) -> List[Dict[str, Any]]:
        """Get every open deal with Bitrix24's recommended ID pagination."""

        return await self._paginate_by_id_read_only(
            "crm.item.list",
            {
                "entityTypeId": 2,
                "select": [
                    "id",
                    "title",
                    "stageId",
                    "categoryId",
                    "assignedById",
                    "createdTime",
                    "lastActivityTime",
                    "movedTime",
                    "closedate",
                ],
                "filter": {"stageSemanticId": "P"},
            },
            result_path=("result", "items"),
            id_field="id",
        )

    async def list_deal_activities(
        self,
        deal_ids: Iterable[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Get activities bound to the supplied deals in bounded ID chunks."""

        return await self._list_deal_activities(deal_ids, pending_only=False)

    async def list_pending_deal_activities(
        self,
        deal_ids: Iterable[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Get only incomplete deal activities for next-action checks."""

        return await self._list_deal_activities(deal_ids, pending_only=True)

    async def _list_deal_activities(
        self,
        deal_ids: Iterable[int],
        *,
        pending_only: bool,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Get activities per deal through controlled read-only batch commands."""

        select = [
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
        normalized_ids = list(self._chunk_ids(deal_ids, chunk_size=50))
        if not normalized_ids:
            return {}

        requested_ids = [deal_id for chunk in normalized_ids for deal_id in chunk]
        activities_by_deal: Dict[int, Dict[int, Dict[str, Any]]] = {
            deal_id: {} for deal_id in requested_ids
        }
        pending_pages: Dict[int, int] = {deal_id: 0 for deal_id in requested_ids}
        is_first_batch = True

        while pending_pages:
            current_pages = list(pending_pages.items())[:50]
            if not is_first_batch and self._batch_pause_seconds:
                await asyncio.sleep(self._batch_pause_seconds)
            is_first_batch = False

            commands = {
                f"deal_{deal_id}": self._build_activity_list_command(
                    deal_id=deal_id,
                    start=start,
                    select=select,
                    pending_only=pending_only,
                )
                for deal_id, start in current_pages
            }
            batch_page = await self._call_read_only_batch(commands)

            for deal_id, start in current_pages:
                command_key = f"deal_{deal_id}"
                page_items = batch_page["items"][command_key]
                for activity in page_items:
                    activity_id = self._required_positive_identifier(activity, "ID")
                    activities_by_deal[deal_id][activity_id] = dict(activity)

                next_value = batch_page["next"].get(command_key)
                if next_value in (None, ""):
                    pending_pages.pop(deal_id)
                    continue
                next_start = self._parse_pagination_cursor(
                    next_value,
                    "crm.activity.list",
                )
                if next_start <= start:
                    raise Bitrix24UnexpectedResponseError(
                        "Bitrix24 returned a non-advancing activity cursor in batch."
                    )
                if not page_items:
                    raise Bitrix24UnexpectedResponseError(
                        "Bitrix24 returned an empty activity page with a batch cursor."
                    )
                pending_pages[deal_id] = next_start

        return {
            deal_id: [records[key] for key in sorted(records)]
            for deal_id, records in activities_by_deal.items()
        }

    async def list_deal_stage_history(
        self,
        deal_ids: Iterable[int],
    ) -> List[Dict[str, Any]]:
        """Get stage history for the supplied deals in bounded ID chunks."""

        select = [
            "ID",
            "TYPE_ID",
            "OWNER_ID",
            "CREATED_TIME",
            "CATEGORY_ID",
            "STAGE_ID",
            "STAGE_SEMANTIC_ID",
        ]
        history: List[Dict[str, Any]] = []
        for id_chunk in self._chunk_ids(deal_ids, chunk_size=45):
            history.extend(
                await self._paginate_read_only(
                    "crm.stagehistory.list",
                    {
                        "entityTypeId": 2,
                        "filter": {"@OWNER_ID": id_chunk},
                        "order": {"ID": "ASC"},
                        "select": select,
                    },
                    result_path=("result", "items"),
                )
            )
        return history

    async def list_users(self, user_ids: Iterable[int]) -> List[Dict[str, Any]]:
        """Get brief user records for responsible-person display names."""

        normalized_ids = sorted({int(user_id) for user_id in user_ids})
        if not normalized_ids:
            return []

        users_by_id: Dict[int, Dict[str, Any]] = {}
        for id_chunk in self._chunk_ids(normalized_ids, chunk_size=50):
            chunk_users = await self._paginate_read_only(
                "user.get",
                {
                    "FILTER": {"ID": id_chunk},
                    "SORT": "ID",
                    "ORDER": "ASC",
                },
                result_path=("result",),
            )
            for user in chunk_users:
                user_id = self._required_positive_identifier(user, "ID")
                users_by_id[user_id] = user
        return [users_by_id[key] for key in sorted(users_by_id)]

    async def list_deal_stages(self, category_id: int) -> List[Dict[str, Any]]:
        """Get readable stage names for one deal pipeline."""

        entity_id = "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"
        return await self._paginate_read_only(
            "crm.status.list",
            {
                "order": {"SORT": "ASC"},
                "filter": {"ENTITY_ID": entity_id},
            },
            result_path=("result",),
        )

    async def _paginate_read_only(
        self,
        method: str,
        parameters: Mapping[str, Any],
        *,
        result_path: Sequence[str],
    ) -> List[Dict[str, Any]]:
        """Collect a read-only endpoint while validating shape and cursors."""

        if not result_path:
            raise ValueError("A paginated Bitrix24 result path cannot be empty.")

        items: List[Dict[str, Any]] = []
        start = 0
        seen_starts = set()

        while True:
            if start in seen_starts:
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned a repeated pagination cursor for '{method}'."
                )
            seen_starts.add(start)

            page_parameters = dict(parameters)
            page_parameters["start"] = start
            payload = await self._call_read_only(method, page_parameters)
            page_items = self._read_result_path(payload, tuple(result_path))
            items.extend(page_items)

            next_start = payload.get("next")
            if next_start in (None, ""):
                return items
            if not page_items:
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned an empty page with a continuation cursor "
                    f"for '{method}'."
                )

            parsed_next = self._parse_pagination_cursor(next_start, method)
            if parsed_next in seen_starts:
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned a repeated pagination cursor for '{method}'."
                )
            if parsed_next < start:
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned a non-advancing pagination cursor for '{method}'."
                )
            start = parsed_next
            if self._page_pause_seconds:
                await asyncio.sleep(self._page_pause_seconds)

    async def _paginate_by_id_read_only(
        self,
        method: str,
        parameters: Mapping[str, Any],
        *,
        result_path: Sequence[str],
        id_field: str,
    ) -> List[Dict[str, Any]]:
        """Read a large list using stable ID keyset pagination and no totals."""

        if not result_path:
            raise ValueError("A paginated Bitrix24 result path cannot be empty.")

        items: List[Dict[str, Any]] = []
        last_id = 0
        while True:
            page_parameters = dict(parameters)
            page_filter = dict(parameters.get("filter", {}))
            page_filter[f">{id_field}"] = last_id
            page_parameters["filter"] = page_filter
            page_parameters["order"] = {id_field: "ASC"}
            page_parameters["start"] = -1

            payload = await self._call_read_only(method, page_parameters)
            page_items = self._read_result_path(payload, tuple(result_path))
            if not page_items:
                return items

            page_ids = [
                self._required_positive_identifier(item, id_field)
                for item in page_items
            ]
            if any(item_id <= last_id for item_id in page_ids):
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned a non-advancing ID page for '{method}'."
                )
            if page_ids != sorted(set(page_ids)):
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned an unordered or duplicate ID page for '{method}'."
                )

            items.extend(page_items)
            next_last_id = page_ids[-1]
            if next_last_id <= last_id:
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 returned a repeated ID page for '{method}'."
                )
            last_id = next_last_id

            if len(page_items) < PAGE_SIZE:
                return items
            if self._page_pause_seconds:
                await asyncio.sleep(self._page_pause_seconds)

    def _read_result_path(
        self,
        payload: Mapping[str, Any],
        result_path: Tuple[str, ...],
    ) -> List[Dict[str, Any]]:
        value: Any = payload
        traversed: List[str] = []

        for field_name in result_path:
            parent_name = ".".join(traversed) or "response"
            mapping = self._require_mapping(value, parent_name)
            traversed.append(field_name)
            if field_name not in mapping:
                missing_name = ".".join(traversed)
                raise Bitrix24UnexpectedResponseError(
                    f"Bitrix24 response does not contain '{missing_name}'."
                )
            value = mapping[field_name]

        return self._require_list_of_mappings(value, ".".join(result_path))

    @staticmethod
    def _parse_pagination_cursor(value: Any, method: str) -> int:
        if isinstance(value, bool):
            parsed = -1
        elif isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
        else:
            parsed = -1

        if parsed < 0:
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 returned an invalid pagination cursor for '{method}'."
            )
        return parsed

    @staticmethod
    def _chunk_ids(
        values: Iterable[int],
        *,
        chunk_size: int,
    ) -> Iterable[List[int]]:
        """Yield sorted unique positive IDs without producing empty requests."""

        normalized_ids = sorted({int(value) for value in values if int(value) > 0})
        for position in range(0, len(normalized_ids), chunk_size):
            yield normalized_ids[position : position + chunk_size]

    @staticmethod
    def _build_activity_list_command(
        *,
        deal_id: int,
        start: int,
        select: Sequence[str],
        pending_only: bool,
    ) -> str:
        """Build one fixed, read-only activity command for a batch."""

        query_items: List[Tuple[str, str]] = [
            ("filter[BINDINGS][0][OWNER_TYPE_ID]", "2"),
            ("filter[BINDINGS][0][OWNER_ID]", str(deal_id)),
            ("order[ID]", "ASC"),
            ("start", str(start)),
        ]
        if pending_only:
            query_items.append(("filter[COMPLETED]", "N"))
        query_items.extend(
            (f"select[{index}]", field_name)
            for index, field_name in enumerate(select)
        )
        return "crm.activity.list?" + "&".join(
            f"{key}={value}" for key, value in query_items
        )

    async def _call_read_only_batch(
        self,
        commands: Mapping[str, str],
    ) -> Dict[str, Any]:
        """Execute only validated read commands through Bitrix24 batch."""

        if not 1 <= len(commands) <= 50:
            raise Bitrix24ReadOnlyViolationError(
                "A read-only Bitrix24 batch must contain from 1 to 50 commands."
            )
        for command in commands.values():
            method_name = command.partition("?")[0]
            if method_name not in BATCH_READ_ONLY_METHODS:
                raise Bitrix24ReadOnlyViolationError(
                    f"Bitrix24 method '{method_name}' is not allowed in a batch."
                )

        for attempt in range(self._max_retries + 1):
            payload = await self._call_endpoint(
                "batch",
                {"halt": 0, "cmd": dict(commands)},
                validated_batch=True,
            )
            batch_result = self._require_mapping(payload.get("result"), "result")
            raw_results = self._mapping_or_empty(
                batch_result.get("result"),
                "result.result",
            )
            raw_errors = self._mapping_or_empty(
                batch_result.get("result_error"),
                "result.result_error",
            )
            raw_next = self._mapping_or_empty(
                batch_result.get("result_next"),
                "result.result_next",
            )

            error_codes: List[str] = []
            for command_key in commands:
                if command_key not in raw_errors:
                    continue
                error = self._require_mapping(
                    raw_errors[command_key],
                    f"result.result_error.{command_key}",
                )
                error_codes.append(
                    str(error.get("error") or "UNKNOWN_ERROR").upper()
                )

            authorization_code = next(
                (code for code in error_codes if code in AUTHORIZATION_ERROR_CODES),
                None,
            )
            if authorization_code:
                raise Bitrix24AuthorizationError(
                    "Bitrix24 rejected a batched read command "
                    f"({authorization_code})."
                )

            fatal_code = next(
                (code for code in error_codes if code not in RETRYABLE_ERROR_CODES),
                None,
            )
            if fatal_code:
                raise Bitrix24APIError(
                    f"Bitrix24 returned a batched API error ({fatal_code})."
                )

            if error_codes:
                if attempt < self._max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise Bitrix24APIError(
                    f"Bitrix24 returned a batched API error ({error_codes[0]})."
                )

            page_items: Dict[str, List[Dict[str, Any]]] = {}
            for command_key in commands:
                if command_key not in raw_results:
                    raise Bitrix24UnexpectedResponseError(
                        f"Bitrix24 batch omitted result '{command_key}'."
                    )
                page_items[command_key] = self._require_list_of_mappings(
                    raw_results[command_key],
                    f"result.result.{command_key}",
                )

            return {"items": page_items, "next": raw_next}

        raise Bitrix24APIError("Bitrix24 batch retry limit was exhausted.")

    async def _call_read_only(
        self,
        method: str,
        parameters: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if method not in READ_ONLY_METHODS:
            raise Bitrix24ReadOnlyViolationError(
                f"Bitrix24 method '{method}' is not in the read-only allowlist."
            )

        return await self._call_endpoint(method, parameters)

    async def _call_endpoint(
        self,
        method: str,
        parameters: Mapping[str, Any],
        *,
        validated_batch: bool = False,
    ) -> Dict[str, Any]:
        """Call an endpoint after its public safety boundary has validated it."""

        if method not in READ_ONLY_METHODS and not (
            method == "batch" and validated_batch
        ):
            raise Bitrix24ReadOnlyViolationError(
                f"Bitrix24 endpoint '{method}' is outside the safe boundary."
            )

        method_url = f"{self._base_url}/{method}.json"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.post(method_url, json=dict(parameters))
            except httpx.TimeoutException as exc:
                raise Bitrix24NetworkError(
                    "Bitrix24 did not respond before the network timeout."
                ) from exc
            except httpx.RequestError as exc:
                raise Bitrix24NetworkError(
                    "A network connection to Bitrix24 could not be established."
                ) from exc

            if response.status_code >= 400:
                error_code = self._extract_http_error_code(response)
                if (
                    response.status_code in {401, 403}
                    or error_code in AUTHORIZATION_ERROR_CODES
                ):
                    safe_code = error_code or f"HTTP_{response.status_code}"
                    raise Bitrix24AuthorizationError(
                        "Bitrix24 rejected the webhook credentials or permissions "
                        f"({safe_code})."
                    )
                if (
                    attempt < self._max_retries
                    and (
                        response.status_code in RETRYABLE_HTTP_STATUSES
                        or error_code in RETRYABLE_ERROR_CODES
                    )
                ):
                    await self._wait_before_retry(attempt)
                    continue
                safe_code = error_code or f"HTTP_{response.status_code}"
                raise Bitrix24APIError(
                    f"Bitrix24 returned an API error ({safe_code})."
                )

            response_payload = self._decode_optional_json(response)
            if response_payload is None:
                raise Bitrix24EmptyResponseError(
                    "Bitrix24 returned an empty response body."
                )

            error_code = self._extract_error_code(response_payload)
            if error_code in AUTHORIZATION_ERROR_CODES:
                raise Bitrix24AuthorizationError(
                    "Bitrix24 rejected the webhook credentials or permissions "
                    f"({error_code})."
                )

            if error_code:
                if (
                    attempt < self._max_retries
                    and error_code in RETRYABLE_ERROR_CODES
                ):
                    await self._wait_before_retry(attempt)
                    continue
                raise Bitrix24APIError(
                    f"Bitrix24 returned an API error ({error_code})."
                )

            if "result" not in response_payload:
                raise Bitrix24UnexpectedResponseError(
                    "Bitrix24 response does not contain the expected result field."
                )
            return response_payload

        raise Bitrix24APIError("Bitrix24 retry limit was exhausted.")

    async def _wait_before_retry(self, attempt: int) -> None:
        await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))

    @staticmethod
    def _required_positive_identifier(
        mapping: Mapping[str, Any],
        field_name: str,
    ) -> int:
        value = mapping.get(field_name)
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 field '{field_name}' is not a valid identifier."
            ) from exc
        if parsed <= 0:
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 field '{field_name}' is not a positive identifier."
            )
        return parsed

    @classmethod
    def _mapping_or_empty(cls, value: Any, field_name: str) -> Dict[str, Any]:
        if value in (None, []):
            return {}
        return cls._require_mapping(value, field_name)

    @staticmethod
    def _decode_optional_json(response: httpx.Response) -> Optional[Dict[str, Any]]:
        if not response.content:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise Bitrix24UnexpectedResponseError(
                "Bitrix24 returned a response that is not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise Bitrix24UnexpectedResponseError(
                "Bitrix24 returned an unexpected JSON structure."
            )
        return payload

    @staticmethod
    def _extract_error_code(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        if payload is None or not payload.get("error"):
            return None
        return str(payload["error"]).strip().upper()

    @classmethod
    def _extract_http_error_code(cls, response: httpx.Response) -> Optional[str]:
        """Read an HTTP error code without requiring a JSON error page."""

        if not response.content:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return cls._extract_error_code(payload)

    @staticmethod
    def _require_mapping(value: Any, field_name: str) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 field '{field_name}' has an unexpected structure."
            )
        return value

    @staticmethod
    def _require_list_of_mappings(
        value: Any,
        field_name: str,
    ) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise Bitrix24UnexpectedResponseError(
                f"Bitrix24 field '{field_name}' must be a list of objects."
            )
        return value
