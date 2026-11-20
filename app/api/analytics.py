"""Read-only CRM Analyst endpoints."""

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
from app.models.deal_attention import DealAttentionResponse
from app.models.executive_report import ExecutiveReport
from app.services.deal_attention import DealAttentionService
from app.services.executive_report import ExecutiveReportPresenter


router = APIRouter(prefix="/api/v1/analytics", tags=["CRM Analytics (read-only)"])


@router.get(
    "/deals/attention",
    response_model=ExecutiveReport,
    summary="Get a concise executive report for open deals",
)
async def get_deals_requiring_attention(
    settings: Settings = Depends(get_settings),
) -> ExecutiveReport:
    """Return the management-first view without changing Bitrix24 data."""

    analysis = await _load_deal_attention_analysis(settings)
    return ExecutiveReportPresenter.build(analysis)


@router.get(
    "/deals/attention/details",
    response_model=DealAttentionResponse,
    summary="Get the full deal-attention analysis",
)
async def get_deals_requiring_attention_details(
    settings: Settings = Depends(get_settings),
) -> DealAttentionResponse:
    """Return the existing detailed view only when explicitly requested."""

    return await _load_deal_attention_analysis(settings)


async def _load_deal_attention_analysis(settings: Settings) -> DealAttentionResponse:
    """Run the existing read-only analysis for either presentation level."""

    try:
        webhook_url = settings.require_webhook_url()
        async with Bitrix24Client(
            webhook_url=webhook_url,
            timeout_seconds=settings.bitrix24_timeout_seconds,
        ) as client:
            return await DealAttentionService(client).get_deals_requiring_attention()
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
