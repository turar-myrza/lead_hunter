"""Authenticated operator endpoints. Polling never dispatches a reply."""

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints

from app.api.universal_lead_hunter import Application, require_access
from app.core.config import Settings, get_settings
from app.integrations.bitrix24_open_channels import OpenChannelsError
from app.services.lead_hunter_open_channels import ConversationService, client_from_settings, inventory


router = APIRouter(prefix='/api/v1/lead-hunter', tags=['LeadHunter Open Channels'], dependencies=[Depends(require_access)])
Config = Annotated[Settings, Depends(get_settings)]
ProspectId = Annotated[str, Path(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]
DraftId = Annotated[str, Path(pattern=r'^[a-f0-9]{32}$')]
conversation = '/runs/{run_id}/prospects/{prospect_id}/open-channel'


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    crm_type: Literal['lead', 'deal', 'contact', 'company']
    crm_id: PositiveInt
    chat_id: PositiveInt


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]


class ReviewApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: Literal[True]


@asynccontextmanager
async def operation(settings):
    try:
        async with client_from_settings(settings) as client:
            yield client
    except OpenChannelsError as exc:
        statuses = {'credential_not_configured': 503, 'invalid_configuration': 503, 'insufficient_scope': 403,
                    'authorization_denied': 403, 'test_send_disabled': 403, 'prospect_not_found': 404,
                    'draft_not_found': 404, 'conversation_not_linked': 404, 'conversation_not_available': 409,
                    'conversation_already_linked': 409, 'session_changed': 409, 'draft_already_attempted': 409,
                    'no_incoming_message': 409, 'fixture_cannot_use_live_channels': 403, 'engagement_blocked': 403}
        raise HTTPException(statuses.get(exc.code, 502), {'code': exc.code}) from None


@router.get('/open-channels/inventory')
async def get_inventory(settings: Config):
    async with operation(settings) as client:
        return await inventory(client)


@router.post(conversation)
async def link(run_id: PositiveInt, prospect_id: ProspectId, request: LinkRequest, app: Application, settings: Config):
    async with operation(settings) as client:
        return await ConversationService(app, client, run_id, prospect_id).link(request.crm_type, request.crm_id, request.chat_id)


@router.get(conversation + '/history')
async def history(run_id: PositiveInt, prospect_id: ProspectId, app: Application, settings: Config, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    async with operation(settings) as client:
        return await ConversationService(app, client, run_id, prospect_id).history()


@router.post(conversation + '/poll')
async def poll(run_id: PositiveInt, prospect_id: ProspectId, app: Application, settings: Config, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    async with operation(settings) as client:
        return await ConversationService(app, client, run_id, prospect_id).poll()


@router.get(conversation + '/receipts')
async def receipts(run_id: PositiveInt, prospect_id: ProspectId, app: Application, settings: Config, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    async with operation(settings) as client:
        return ConversationService(app, client, run_id, prospect_id).receipts()


@router.post(conversation + '/drafts')
async def draft(run_id: PositiveInt, prospect_id: ProspectId, request: DraftRequest, app: Application, settings: Config):
    async with operation(settings) as client:
        return await ConversationService(app, client, run_id, prospect_id).draft(request.text)


@router.post(conversation + '/drafts/{draft_id}/send')
async def send(run_id: PositiveInt, prospect_id: ProspectId, draft_id: DraftId, approval: ReviewApproval,
               app: Application, settings: Config):
    async with operation(settings) as client:
        return await ConversationService(app, client, run_id, prospect_id).send(draft_id)
