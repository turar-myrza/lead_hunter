"""Internal authenticated API for product-brief runs."""

import secrets
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.models.universal_lead_hunter import RunRequest, RunResult, RunStatus, SourceCapability
from app.services.sales_store import SalesStore
from app.services.universal_lead_hunter import LeadHunterApplication


def require_access(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
    settings: Annotated[Settings, Depends(get_settings)],
):
    token = settings.lead_hunter_api_token
    if token is None or not token.get_secret_value():
        raise HTTPException(503, 'LeadHunter API access is not configured.')
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), token.get_secret_value().encode()):
        raise HTTPException(401, 'LeadHunter authorization required.', headers={'WWW-Authenticate': 'Bearer'})


def get_application(settings: Annotated[Settings, Depends(get_settings)]):
    return LeadHunterApplication(SalesStore(settings.lead_hunter_db), settings.lead_hunter_output)


router = APIRouter(prefix='/api/v1/lead-hunter', tags=['LeadHunter'], dependencies=[Depends(require_access)])
Application = Annotated[LeadHunterApplication, Depends(get_application)]


@router.get('/sources', response_model=list[SourceCapability])
def sources(service: Application):
    return service.capabilities()


@router.post('/runs', response_model=RunStatus, status_code=202)
def submit(request: RunRequest, tasks: BackgroundTasks, service: Application):
    try:
        run_id = service.submit(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    queued = service.status(run_id)
    tasks.add_task(service.process, run_id)
    return queued


@router.get('/runs/{run_id}', response_model=RunStatus)
def status(run_id: int, service: Application):
    run = service.status(run_id)
    if run is None:
        raise HTTPException(404, 'Run not found.')
    return run


@router.get('/runs/{run_id}/result', response_model=RunResult)
def result(run_id: int, service: Application, response: Response):
    status(run_id, service)
    value = service.result(run_id)
    if value is None:
        raise HTTPException(409, 'Run result is unavailable. Check run status.')
    response.headers['Cache-Control'] = 'no-store'
    return value


@router.get('/runs/{run_id}/artifacts/{name}', response_class=FileResponse)
def artifact(run_id: int, name: str, service: Application):
    path = service.artifact(run_id, name)
    if path is None:
        raise HTTPException(404, 'Artifact not found.')
    return FileResponse(path, filename=path.name, headers={'Cache-Control': 'no-store'})
