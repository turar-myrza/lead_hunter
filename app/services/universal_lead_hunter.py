"""Application service shared by the LeadHunter CLI and FastAPI."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.models.lead_hunter import Candidate as CRMCandidate, ResearchBatch
from app.models.sales_team import Prospect
from app.models.universal_lead_hunter import (
    ActionCapability, RunRequest, RunResult, RunStatus, SourcePlan, StageError, StageProgress,
)
from app.models.adaptive_intelligence import AdaptiveRunView
from app.services.adaptive_intelligence import AdaptiveIntelligenceEngine
from app.services.sales_store import SalesStore
from app.services.universal_qualification import build_opportunity, qualify, understand
from app.services.universal_sources import ADAPTERS, OTHER_SOURCES, SourceAdapter

DEFAULT_OUTPUT = Path('outputs/lead_hunter/universal')
ORIGIN = 'universal_lead_hunter'
STAGES = ('understand', 'plan_sources', 'discover', 'verify', 'crm_review', 'qualify', 'engagement', 'export')
ARTIFACTS = frozenset({'lead-hunter.xlsx', 'sales-team.xlsx'})


@dataclass
class CRMReview:
    batch: ResearchBatch
    sales: dict


async def review_crm(batch, sales):
    """Use the existing Bitrix allowlist, matching, and history implementations."""
    from app.core.config import get_settings
    from app.core.exceptions import Bitrix24ConfigurationError
    from app.integrations.lead_hunter_crm import CRMReadError, LeadHunterCRMClient
    from app.models.lead_hunter_crm import CRMCheck
    from app.services.lead_hunter_duplicates import DuplicateService
    from app.services.lead_hunter_relationships import RelationshipService
    from app.services.sales_crm import check_sales_crm

    if batch.candidates:
        settings = get_settings()
        try:
            async with LeadHunterCRMClient(settings.require_webhook_url(), timeout=settings.bitrix24_timeout_seconds) as client:
                await DuplicateService(client).check(batch)
                await RelationshipService(client).enrich(batch)
        except (Bitrix24ConfigurationError, CRMReadError):
            for candidate in batch.candidates:
                candidate.crm_check = CRMCheck(state='unavailable')
    sales_results, _ = await check_sales_crm(sales) if sales else ({}, {})
    return CRMReview(batch, sales_results)


class LeadHunterApplication:
    def __init__(self, store: SalesStore, output_root=DEFAULT_OUTPUT, *,
                 adapters: tuple[Callable[[], SourceAdapter], ...] = ADAPTERS, crm_reader=review_crm):
        self.store = store
        self.output_root = Path(output_root)
        self.adapters = {factory.capability.id: factory for factory in adapters}
        self.crm_reader = crm_reader

    def capabilities(self):
        return [*(factory.capability for factory in self.adapters.values()), *OTHER_SOURCES]

    def submit(self, request: RunRequest) -> int:
        known = {capability.id for capability in self.capabilities()}
        if set(request.source_ids) - known:
            raise ValueError('Unknown source ID. Consult the source capability registry.')
        return self.store.enqueue(ORIGIN, request.model_dump(mode='json'))

    def status(self, run_id: int) -> RunStatus | None:
        run = self.store.get_run(run_id, ORIGIN)
        if run is None:
            return None
        stages = []
        for stage in STAGES:
            saved = self.store.step(run_id, 'finished', stage) or self.store.step(run_id, 'started', stage)
            stages.append(StageProgress.model_validate(saved) if saved else StageProgress(stage=stage, status='pending'))
        return RunStatus(run_id=run_id, status=run['status'], stages=stages)

    def result(self, run_id: int) -> RunResult | None:
        run = self.store.get_run(run_id, ORIGIN)
        if run is None or run['status'] not in {'completed', 'partial'}:
            return None
        saved = self.store.step(run_id, 'finished', 'result')
        return RunResult.model_validate(saved) if saved else None

    def adaptive_status(self, run_id: int) -> AdaptiveRunView | None:
        """Return the reviewable adaptive journal for API, CLI and the panel."""
        return AdaptiveIntelligenceEngine(self.store).view(run_id)

    def artifact(self, run_id: int, name: str) -> Path | None:
        result = self.result(run_id)
        if name not in ARTIFACTS or result is None or name not in result.artifacts:
            return None
        path = self.output_root / str(run_id) / name
        return path if path.is_file() and not path.is_symlink() else None

    def _plan(self, request):
        planned = []
        for capability in self.capabilities():
            for path in sorted(request.brief.buyer_paths.intersection(capability.paths)):
                preferred = not request.source_ids or capability.id in request.source_ids
                fallback = bool(request.adaptive and request.adaptive.enabled and request.adaptive.allow_alternative_sources)
                if not preferred and not fallback:
                    enabled, reason = False, 'This run selected other sources.'
                elif capability.status != 'implemented':
                    enabled, reason = False, capability.access
                elif request.mode not in capability.modes:
                    enabled, reason = False, 'Unavailable in this run mode.'
                elif capability.context != 'general' and request.research_context != capability.context:
                    enabled, reason = False, 'The legacy profile uses CRM-specific signals.'
                elif capability.markets and request.brief.market.casefold() not in {m.casefold() for m in capability.markets}:
                    enabled, reason = False, 'The market is outside this adapter’s configured coverage.'
                elif capability.id == 'evidence_intake' and not any(c.path == path for c in request.supplied_candidates):
                    enabled, reason = False, 'No evidence intake records supplied for this buyer path.'
                else:
                    enabled = True
                    reason = capability.access if preferred else 'Adaptive fallback: compatible implemented source available after a materially different attempt.'
                objective = (f'Find a use case, owning department and supplier decision path for {request.brief.name}.'
                             if path == 'organization' else
                             f'Review inbound need, user, household decision and payment roles for {request.brief.name}.')
                planned.append(SourcePlan(capability=capability, path=path, objective=objective, enabled=enabled, reason=reason))
        return planned

    async def process(self, run_id: int):
        with self.store.worker_lock(run_id) as acquired:
            if not acquired or not (run := self.store.claim_run(run_id, ORIGIN)):
                return
            try:
                await self._execute(run_id, RunRequest.model_validate_json(run['payload']))
            except Exception:
                # Unexpected detail is intentionally excluded from persisted/user-facing errors.
                self.store.finish(run_id, 'failed', 'universal_workflow_failed')
                raise

    async def _execute(self, run_id, request):
        errors_seen = []
        stage = 'understand'

        def progress(status, detail='', errors=()):
            name = 'started' if status == 'running' else 'finished'
            value = StageProgress(stage=stage, status=status, detail=detail, errors=list(errors))
            self.store.save(run_id, name, stage, value.model_dump(mode='json'))
            errors_seen.extend(errors)

        def error(code, message, source=None):
            return StageError(stage=stage, code=code, message=message, source=source)

        try:
            progress('running')
            problem, buyer_paths = understand(request)
            progress('completed', f'{len(buyer_paths)} buyer path(s) modeled.')

            stage = 'plan_sources'
            progress('running')
            plan = self._plan(request)
            enabled_ids = dict.fromkeys(entry.capability.id for entry in plan if entry.enabled)
            enabled_factories = {key: self.adapters[key] for key in enabled_ids}
            enabled = {key: factory() for key, factory in enabled_factories.items()}
            uncovered = request.brief.buyer_paths - {entry.path for entry in plan if entry.enabled}
            skipped = set(request.source_ids) - enabled.keys()
            plan_errors = [error('no_source_for_path', f'No enabled source for the {path} path.') for path in sorted(uncovered)]
            plan_errors.extend(error('source_not_enabled', 'Requested source is unavailable for this run.', key) for key in sorted(skipped))
            progress('partial' if plan_errors else 'completed', f'{len(enabled)} adapter(s) selected.', plan_errors)

            stage = 'discover'
            progress('running')
            adaptive = bool(request.adaptive and request.adaptive.enabled)
            adaptive_execution = None
            discoveries, failures = {}, []
            if adaptive:
                adaptive_execution = await AdaptiveIntelligenceEngine(self.store).execute(run_id, request, plan, enabled_factories)
                failures = [error(item.classification, item.summary, item.source_id) for item in adaptive_execution.failures]
                progress('partial' if failures else 'completed',
                         f'{len(adaptive_execution.records)} verified record(s) retained across adaptive source attempt(s).', failures)
            else:
                for key, adapter in enabled.items():
                    try:
                        discoveries[key] = await adapter.discover(request)
                    except Exception:
                        failures.append(error('discovery_failed', 'Adapter discovery failed.', key))
                        continue
                    if discoveries[key].incomplete:
                        failures.append(error('discovery_incomplete', 'Some configured upstream sources did not complete.', key))
                progress('partial' if failures else 'completed', f'{len(discoveries)} adapter response(s).', failures)

            stage = 'verify'
            progress('running')
            records, source_ids, failures = {}, {}, []
            if adaptive_execution is not None:
                for record in adaptive_execution.records.values():
                    candidate = record.candidate
                    key = adaptive_execution.source_ids.get(self._adaptive_candidate_key(candidate), 'unknown')
                    active_paths = {entry.path for entry in plan if entry.enabled and entry.capability.id == key}
                    if candidate.path not in active_paths or candidate.id in records:
                        failures.append(error('candidate_conflict', 'Candidate path or identity is inconsistent with its run.', key))
                        continue
                    if len(records) < request.limit:
                        records[candidate.id] = record
                        source_ids[candidate.id] = key
                progress('partial' if failures else 'completed',
                         f'{len(records)} candidate(s) retained with provenance after adaptive verification.', failures)
            else:
                for key, discovered in discoveries.items():
                    try:
                        verified = await enabled[key].verify(request, discovered)
                    except Exception:
                        failures.append(error('verification_failed', 'Adapter evidence verification failed.', key))
                        continue
                    if verified.incomplete:
                        failures.append(error('verification_incomplete', 'Some upstream evidence could not be read.', key))
                    active_paths = {entry.path for entry in plan if entry.enabled and entry.capability.id == key}
                    for record in verified.records:
                        candidate = record.candidate
                        if candidate.path not in active_paths or candidate.id in records:
                            failures.append(error('candidate_conflict', 'Candidate path or identity is inconsistent with its run.', key))
                            continue
                        if len(records) < request.limit:
                            records[candidate.id] = record
                            source_ids[candidate.id] = key
                progress('partial' if failures else 'completed', f'{len(records)} candidate(s) retained with provenance.', failures)

            stage = 'crm_review'
            progress('running')
            crm_batch = ResearchBatch(version='1.2', mode='expanded', search_log=['Universal LeadHunter source adapter'],
                    candidates=[r.legacy for r in records.values() if isinstance(r.legacy, CRMCandidate)])
            sales = [r.legacy for r in records.values() if isinstance(r.legacy, Prospect)]
            crm = CRMReview(crm_batch, {})
            failures = []
            if request.crm_read_only:
                if crm_batch.candidates or sales:
                    crm = await self.crm_reader(crm_batch, sales)
                eligible = len(crm_batch.candidates) + len(sales)
                organization_count = sum(r.candidate.path == 'organization' for r in records.values())
                if organization_count > eligible:
                    failures.append(error('crm_identity_not_supported', 'Generic intake has no verified business identifiers for CRM matching.'))
                complete_states = {'no_match_accessible', 'confirmed_duplicate', 'possible_match'}
                if (any(c.crm_check.state not in complete_states or not all(c.crm_check.coverage.get(k)
                        for k in ('company', 'contact', 'lead')) for c in crm.batch.candidates)
                    or any(crm.sales.get(p.name, {}).get('state') not in complete_states
                           or not all(crm.sales[p.name].get('coverage', {}).get(k)
                                      for k in ('company', 'contact', 'lead', 'deal')) for p in sales)):
                    failures.append(error('crm_read_incomplete', 'Bitrix24 read coverage is incomplete or unavailable.'))
            progress('partial' if failures else 'completed', 'Read-only organization review processed.' if request.crm_read_only
                     else 'CRM reads were omitted by run configuration.', failures)

            stage = 'qualify'
            progress('running')
            decisions, sales_rows = _legacy_decisions(crm, records)
            assessments = {key: qualify(request, record.candidate, decisions.get(key, (None,))[0])
                           for key, record in records.items()}
            progress('completed', f'{sum(item.state == "qualified" for item in assessments.values())} candidate(s) qualified.')

            stage = 'engagement'
            progress('running')
            opportunities = []
            for key, record in records.items():
                _, check, relationship, crm_owner = decisions.get(key, (None, 'not_checked', 'unknown', None))
                opportunities.append(build_opportunity(request, record.candidate, assessments[key],
                    source_id=source_ids[key], crm_check=check, relationship=relationship, crm_owner=crm_owner))
            actions = [ActionCapability(action=key) for key in ('email', 'sms', 'sip', 'crm_write')]
            progress('completed', f'{len(opportunities)} review-only engagement plan(s).')

            stage = 'export'
            progress('running')
            artifacts, failures = _export_workbooks(self.output_root / str(run_id), crm.batch, sales, sales_rows)
            result = RunResult(run_id=run_id, brief=request.brief, mode=request.mode, problem_hypothesis=problem,
                    buyer_paths=buyer_paths, source_plan=plan, opportunities=opportunities,
                    action_capabilities=actions, artifacts=artifacts)
            self.store.save(run_id, 'finished', 'result', result.model_dump(mode='json'))
            export_errors = [error('workbook_failed', f'{name} could not be generated.') for name in failures]
            progress('partial' if export_errors else 'completed', 'Structured result is available.', export_errors)
            self.store.finish(run_id, 'partial' if errors_seen else 'completed')
        except Exception:
            progress('failed', 'The run could not complete.', [error('stage_failed', 'An unexpected stage error occurred.')])
            self.store.finish(run_id, 'failed')

    @staticmethod
    def _adaptive_candidate_key(candidate):
        return f'{candidate.path}:{candidate.label.casefold().strip()}'


def _legacy_decisions(crm, records):
    from app.services.lead_hunter_routing import route
    from app.services.sales_team import qualify as qualify_sales

    decisions, sales_rows = {}, []
    local_keys = [key for key, record in records.items() if isinstance(record.legacy, CRMCandidate)]
    for key, candidate in zip(local_keys, crm.batch.candidates, strict=True):
        decision = route(candidate)
        state = ('excluded' if decision.contact_permissibility == 'prohibited'
                 else 'qualified' if decision.active_task else 'needs_review')
        reason = ' '.join(decision.reasons) or 'Review the commercial fit and supporting evidence.'
        decisions[key] = (state, reason), candidate.crm_check.state, decision.relationship_state, decision.owner_name
    for key, record in records.items():
        prospect = record.legacy
        if not isinstance(prospect, Prospect):
            continue
        snapshot = crm.sales.get(prospect.name, {})
        decision = qualify_sales(prospect, snapshot)
        state = decision['status'] if decision['status'] in {'qualified', 'excluded'} else 'needs_review'
        decisions[key] = ((state, decision['reason']), decision['crm_state'],
                          snapshot.get('relationship', 'unknown'), snapshot.get('owner'))
        sales_rows.append(decision)
    return decisions, sales_rows


def _export_workbooks(directory, batch, sales, sales_rows):
    from app.services.lead_hunter_run import project_rows, report_counts
    from app.services.lead_hunter_workbook import export_workbook
    from app.services.sales_workbook import export_sales_workbook

    artifacts, failures = [], []

    def write(name, export, *args, **kwargs):
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            export(directory / name, *args, **kwargs)
        except Exception:
            failures.append(name)
        else:
            artifacts.append(name)

    if batch.candidates:
        as_of = datetime.now(timezone.utc)
        rows = list(project_rows(batch, as_of))
        write('lead-hunter.xlsx', export_workbook, rows=rows,
              run={'id': directory.name, 'status': 'universal_review', 'city': 'Шымкент', 'counts': report_counts(rows)})
    if sales:
        ready, review, sources = [], [], []
        for p, decision in zip(sales, sales_rows):
            row = {'name': p.name, 'city': p.city, **decision}
            if decision['status'] == 'qualified':
                ready.append({**row, 'qualification': decision, 'sources': ' / '.join(decision['sources']), 'permission': 'review_only'})
            else:
                review.append(row)
            sources.extend({'company': p.name, **s.model_dump(mode='json')} for s in p.sources)
        write('sales-team.xlsx', export_sales_workbook, {'qualification': ready, 'review': review, 'source_details': sources,
              'run_id': directory.name, 'origin': ORIGIN, 'researched': len(sales), 'qualified': len(ready),
              'needs_review': len(review), 'prepared': 0})
    return artifacts, failures
