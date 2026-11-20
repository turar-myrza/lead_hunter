"""FastAPI endpoint for the first Bitrix24 test scenario."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    Bitrix24APIError,
    Bitrix24AuthorizationError,
    Bitrix24ConfigurationError,
    Bitrix24EmptyResponseError,
    Bitrix24NetworkError,
    Bitrix24UnexpectedResponseError,
)
from app.integrations.bitrix24 import Bitrix24Client
from app.models.deal import LatestDealsResponse
from app.services.deals import DealService


router = APIRouter(prefix="/api/v1/deals", tags=["Deals (read-only)"])


@router.get(
    "/latest",
    response_model=LatestDealsResponse,
    summary="Get the latest 10 modified Bitrix24 deals",
)
async def get_latest_deals(
    settings: Settings = Depends(get_settings),
) -> LatestDealsResponse:
    """Return the requested five fields without changing Bitrix24 data."""

    try:
        webhook_url = settings.require_webhook_url()
        async with Bitrix24Client(
            webhook_url=webhook_url,
            timeout_seconds=settings.bitrix24_timeout_seconds,
        ) as client:
            deals = await DealService(client).get_latest_deals(limit=10)
    except Bitrix24ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Bitrix24AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except Bitrix24NetworkError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (
        Bitrix24APIError,
        Bitrix24EmptyResponseError,
        Bitrix24UnexpectedResponseError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    if not deals:
        return LatestDealsResponse(
            count=0,
            message="Bitrix24 returned no deals visible to the webhook owner.",
            deals=[],
        )

    return LatestDealsResponse(
        count=len(deals),
        message="Latest deals, ordered by modification date (newest first).",
        deals=deals,
    )
