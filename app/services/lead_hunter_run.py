"""On-demand Lead Hunter workflow. Checkpoints are private, local observations.

The first release stops at the model boundary; no runtime path can activate a
provider, send a message or perform a CRM write.
"""

import asyncio
import csv
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from html import escape
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import mkstemp
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing import Literal

from app.integrations.lead_hunter_public import public_https_url
from app.models.lead_hunter import ResearchBatch
from app.models.lead_hunter_crm import CRMCheck
from app.models.lead_hunter_relationships import RelationshipReview
from app.services.lead_hunter import safe_cell, validate_batch_identity
from app.services.lead_hunter_scoring import freshness
from app.services.lead_hunter_v12_reporting import current_contact, decisions


def utcnow():
    return datetime.now(timezone.utc)


def _crm_fresh(checked_at, now):
    return (checked_at is not None and checked_at.tzinfo is not None
            and timedelta(0) <= now - checked_at <= timedelta(hours=24))


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    name: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,48}$')
    kind: Literal['rss', 'index', 'employer']
    url: str
    path_prefix: str | None = None
    employer: str | None = Field(default=None, min_length=2, max_length=120)

    @model_validator(mode='after')
    def check_shape(self):
        from app.services.lead_hunter_discovery import _source_allowed

        normalized = public_https_url(self.url)
        if not _source_allowed(normalized):
            raise ValueError('Этот домен не разрешён для автоматического поиска.')
        if self.kind == 'index':
            if not self.path_prefix or not self.path_prefix.startswith('/') or self.path_prefix == '/':
                raise ValueError('Для индекса нужен узкий path_prefix.')
        elif self.path_prefix is not None:
            raise ValueError('path_prefix применим только к индексу.')
        if (self.kind == 'employer') != (self.employer is not None):
            raise ValueError('employer обязателен только для официальной страницы компании.')
        return self

    def adapter(self):
        from app.services.lead_hunter_discovery import EmployerSiteAdapter, PublicIndexAdapter, RssAdapter

        url = public_https_url(self.url)
        if self.kind == 'employer':
            return EmployerSiteAdapter(self.name, self.employer, (url,))
        if self.kind == 'index':
            return PublicIndexAdapter(self.name, url, self.path_prefix)
        return RssAdapter(self.name, url, (urlsplit(url).hostname,))


class RunOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    city: Literal['Шымкент'] = 'Шымкент'
    limit: int = Field(default=3, ge=1, le=3)
    direction: Literal['both', 'new-crm', 'existing-bitrix24'] = 'both'
    public_only: bool = False
    sources: tuple[SourceEntry, ...] = Field(default=(), max_length=12)

    @property
    def lanes(self):
        return {'both': ('A', 'B'), 'new-crm': ('A',), 'existing-bitrix24': ('B',)}[self.direction]


class RunStore:
    def __init__(self, directory):
        self.path = Path(directory)

    @classmethod
    def create(cls, directory, options):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        store = cls(directory)
        store.save('run.json', {'version': 1, 'id': uuid4().hex, 'started_at': utcnow().isoformat(),
                                'options': options.model_dump(mode='json')})
        return store

    def load(self, name, default=None):
        try:
            with (self.path / name).open(encoding='utf-8') as stream:
                return json.load(stream)
        except FileNotFoundError:
            return default

    def write(self, name, value, encoding='utf-8'):
        fd, path = mkstemp(prefix='.checkpoint-', dir=self.path)
        try:
            with os.fdopen(fd, 'w', encoding=encoding, newline='') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.path / name)
        finally:
            Path(path).unlink(missing_ok=True)

    def save(self, name, value):
        self.write(name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))

    def event(self, stage, status, **details):
        # Callers pass only counters and controlled status/error types. No raw
        # HTTP exceptions, URLs from CRM, prompts, credentials or transcripts.
        event = {'at': utcnow().isoformat(), 'stage': stage, 'status': status, **details}
        fd = os.open(self.path / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        print(f'{stage}: {status}', flush=True)

    @contextmanager
    def locked(self):
        fd = os.open(self.path / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Этот запуск уже выполняется.') from None
            yield
        finally:
            os.close(fd)


def qualification(candidate, decision, as_of):
    """A parallel research queue, never a score override or permission to act."""
    review = candidate.relationship_review
    if (decision.active_task or decision.contact_permissibility == 'prohibited'
            or candidate.crm_check.matches or review.identity != 'none'
            or decision.category == 'C'):
        return []
    sources = {s.id: s for s in candidate.sources}
    strong = []
    for evidence in candidate.evidence:
        if evidence.signal not in {'growth', 'processing_problem', 'automation_project'} or evidence.scope != 'local':
            continue
        src = sources[evidence.source_id]
        day = as_of.astimezone(src.checked_at.tzinfo).date()
        if (src.published_on and 0 <= (day - src.published_on).days <= 30
                and (not src.valid_until or src.valid_until >= day) and freshness(src, as_of)[0]):
            strong.append(f'{evidence.fact} [{src.id}]')
    if not strong:
        return []
    steps = ['Уточнить действующую CRM и владельца процесса.' if decision.crm_status != 'bitrix24'
             else 'Уточнить, какая часть процесса Битрикс24 ещё не автоматизирована.']
    if decision.contact_level not in {'company_channel', 'responsible_person'}:
        steps.append('Найти подтверждённый публичный деловой канал.')
    if candidate.crm_check.state != 'no_match_accessible' or not _crm_fresh(candidate.crm_check.checked_at, as_of):
        steps.append('Завершить READ ONLY сверку идентичности и истории.')
    steps.append('Уточнить объём текущей задачи и нужен ли внешний подрядчик. Обращение не разрешено.')
    return ['Свежие сигналы: ' + '; '.join(strong), *steps]


def _business_contacts(candidate, as_of):
    return [contact for contact in candidate.contacts
            if contact.role != 'recruitment' and current_contact(candidate, contact, as_of)]


def _business_channels(contacts):
    channels = {contact.type for contact in contacts if contact.type in {'email', 'phone'}}
    if any(contact.type == 'phone'
           and re.search(r'whats\s*app|ватсап|вацап|wa\.me/', contact.evidence.quote, re.I)
           for contact in contacts):
        channels.add('whatsapp')
    return channels


def draft_candidate(candidate, runtime, *, cta, as_of):
    """Prepare local drafts with a caller-supplied runtime. No provider is activated here."""
    from app.integrations.lead_hunter_ai import DraftRuntimeError, PublicDraftInput
    from app.services.lead_hunter_routing import route

    decision = route(candidate, as_of)
    if decision.category not in {'A', 'B'} or decision.contact_permissibility != 'allow_draft':
        raise DraftRuntimeError('Current routing does not allow an A/B outreach draft')
    proposal = candidate.commercial_proposal
    citation = proposal.confirmed_fact
    # An evaluated proposal already has a current, local public citation.
    # A synthetic reference keeps source URLs, IDs and CRM metadata out of the payload.
    try:
        payload = PublicDraftInput(
            company=candidate.name, city=candidate.city, sector=candidate.sector,
            category=decision.category, facts=[{'fact_id': 'fact1', 'source_id': 'public1', 'quote': citation.quote}],
            hypothesis=proposal.hypothesis, offer=proposal.concrete_offer, cta=cta,
            allowed_channels=sorted(_business_channels(_business_contacts(candidate, as_of))),
        )
    except ValidationError:
        raise DraftRuntimeError('The proposal needs a safe public citation and a verified business channel') from None
    draft = runtime.draft(payload)
    if draft.fact_refs != ['fact1'] or draft.recommended_channel not in payload.allowed_channels:
        raise DraftRuntimeError('The draft must use the approved public fact and business channel')
    try:
        updated = type(proposal).model_validate({**proposal.model_dump(), 'outreach_copy': draft.manager_copy(cta)})
    except ValidationError:
        raise DraftRuntimeError('The draft did not satisfy the customer-facing copy rules') from None
    return candidate.model_copy(update={'commercial_proposal': updated})


def project_rows(batch, as_of, *, archive=False, lanes=None):
    for c, d in decisions(batch, as_of):
        r = c.relationship_review
        proposal = c.commercial_proposal
        steps = [] if archive else qualification(c, d, as_of)
        label = {'A': 'NEW CRM', 'B': 'EXISTING BITRIX24', 'C': 'CRM CONSULTING CUSTOMERS'}
        direction = label.get(d.category) or label.get((lanes or {}).get(c.name), 'Требуется проверка')
        can_draft = not archive and d.category in {'A', 'B'} and d.contact_permissibility == 'allow_draft'
        basis_ids = {proposal.confirmed_fact.source_id, *(fact.source_id for fact in proposal.new_basis)} if proposal else set()
        contacts = _business_contacts(c, as_of)
        copy = proposal.outreach_copy if can_draft and proposal else None
        yield dict(name=c.name, sector=c.sector, category=d.category, direction=direction,
                   need_score=d.need_score, offer_score=d.offer_prospect_score, contact_score=d.contact_score,
                   crm_status=d.crm_status, relationship_state=r.relationship_state,
                   priority=d.commercial_priority, contact_permissibility='archive_only' if archive else d.contact_permissibility,
                   active_task=d.active_task and not archive, qualified=bool(steps), purchase_interest=d.purchase_interest,
                   signals=[f'{e.fact} [{e.source_id}]' for e in c.evidence],
                   sources=[f'{s.id}: {s.url}; публикация {s.published_on or "неизвестна"}; '
                            f'проверено {s.checked_at.isoformat()}; статус {s.vacancy_status}; '
                            f'последняя попытка {s.last_attempt_status}; срок {s.valid_until or "не указан"}' for s in c.sources],
                   evidence_urls=[s.url for s in c.sources],
                   contact=[x.value for x in contacts],
                   history=[f'Сопоставление: {c.crm_check.state}; история: {r.status}; идентичность: {r.identity}; '
                            f'ответственный: {r.owner_name or "не установлен"}.',
                            f'Последнее подтверждённое общение: {r.last_interaction_at or "не установлено"}.',
                            *(f'{item.text} [{item.reference.entity} #{item.reference.id}]' for item in r.facts),
                            *r.limitations],
                   offer=(proposal.concrete_offer if proposal and (can_draft or d.active_task and not archive)
                          else 'Гипотеза для проверки: ' + c.hypothesis),
                   outreach_hypothesis=proposal.hypothesis if proposal else c.hypothesis,
                   outreach_product=proposal.product_key if proposal else None,
                   outreach_sources=[s.url for s in c.sources if s.id in basis_ids],
                   outreach_copy=copy,
                   outreach_channel_verified=copy.recommended_channel in _business_channels(contacts) if copy else None,
                   next_step=('Исторический снимок; перед рекомендацией нужен новый запуск с проверкой источников и CRM.'
                              if archive else steps[1:] if len(steps) > 1 else d.next_step),
                   reasons=[*d.reasons, *d.need_rationale, *d.offer_rationale, *steps])


def report_counts(rows, findings=0):
    groups = Counter(row['category'] for row in rows)
    return {'found_sources': findings, 'researched': len(rows), **{x: groups[x] for x in 'ABCD'},
            'excluded': sum(r['contact_permissibility'] == 'prohibited' for r in rows),
            'needs_review': sum(not r['active_task'] and r['contact_permissibility'] != 'prohibited' for r in rows),
            'recommended': sum(r['active_task'] for r in rows), 'qualify': sum(r['qualified'] for r in rows)}


def _report_files(store, batch, rows, summary):
    from app.services.lead_hunter_workbook import export_workbook

    export_workbook(store.path / 'report.xlsx', run=summary, rows=rows)
    store.save('evidence.json', batch.model_dump(mode='json'))
    keys = ('name', 'direction', 'category', 'need_score', 'offer_score', 'priority', 'qualified', 'next_step', 'reasons', 'sources')

    def plain(value):
        return ' / '.join(value) if isinstance(value, list) else '' if value is None else str(value)

    csv_stream = StringIO(newline='')
    writer = csv.writer(csv_stream, delimiter=';')
    writer.writerow(keys)
    writer.writerows([safe_cell(plain(row[key])) for key in keys] for row in rows)
    store.write('review.csv', csv_stream.getvalue(), encoding='utf-8-sig')
    lines = ['# Lead Hunter — журнал исследования', '', f'Статус: `{summary["status"]}`. '
             f'Оценка: {summary["evaluated_as_of"]}. Вызовы LLM: 0. Только READ ONLY.', '',
             ', '.join(f'{k}: {v}' for k, v in summary['counts'].items()), '',
             '| Компания | Категория | NEED / OFFER | Квалифицировать | Причина / следующий шаг |',
             '|---|---|---|---|---|']
    for row in rows:
        cells = (row['name'], row['category'], f'{plain(row["need_score"]) or "—"} / {plain(row["offer_score"]) or "—"}',
                 'Да' if row['qualified'] else 'Нет', [*row['reasons'], plain(row['next_step'])])
        lines.append('| ' + ' | '.join(escape(plain(v)).replace('|', '\\|').replace('\n', ' ') for v in cells) + ' |')
    lines += ['', 'Отчёт не даёт разрешения на отправку. Гипотеза не является подтверждённой потребностью.']
    store.write('report.md', '\n'.join(lines) + '\n')
    store.save('summary.json', summary)


def _alternate(findings, lanes):
    groups = [[f for f in findings if f.lane == lane] for lane in lanes]
    return [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]


async def _verify_batch(store, options, result, verify, fetcher, result_type):
    stage = 'verification'
    frozen = store.load('public_batch.json')
    if frozen is not None:
        batch = ResearchBatch.model_validate(frozen['batch'])
        store.event(stage, 'reused', candidates=len(batch.candidates))
        return batch, frozen['lanes'], False

    batch = ResearchBatch(version='1.2', candidates=[], search_log=result.search_log or ['Нет результатов поиска.'],
                          skipped_sources=list(result.skipped_sources))
    lanes, pending = {}, False
    store.event(stage, 'started')
    for i, finding in enumerate(_alternate(result.findings, options.lanes)):
        if len(batch.candidates) >= options.limit:
            break
        key = f'verified-{i:04}.json'
        raw = store.load(key)
        if raw is None:
            verified = await verify(options.city, [finding], 1, fetcher=fetcher)
            if verified.verification_incomplete:
                pending = True
                store.save(f'pending-{i:04}.json', verified.model_dump(mode='json'))
                store.event(stage, 'incomplete', index=i)
            else:
                store.save(key, verified.model_dump(mode='json'))
                store.event(stage, 'checkpoint', index=i, candidates=len(verified.candidates))
        else:
            verified = result_type.model_validate(raw)
            store.event(stage, 'reused', index=i, candidates=len(verified.candidates))
        batch.skipped_sources.extend(verified.skipped_sources)
        batch.search_log.extend(verified.search_log)
        for candidate in verified.candidates[:1]:
            # Public inputs cannot seed a CRM decision or a ready-made offer.
            candidate = candidate.model_copy(update={'commercial_proposal': None, 'crm_check': CRMCheck(),
                                                     'relationship_review': RelationshipReview()})
            batch.candidates.append(candidate)
            try:
                validate_batch_identity(batch)
            except ValueError:
                batch.candidates.pop()
                store.event(stage, 'duplicate_public_identity', index=i)
            else:
                lanes[candidate.name] = finding.lane
    partial = pending and len(batch.candidates) < options.limit
    if not partial and (result.findings or result.providers_succeeded):
        store.save('public_batch.json', {'batch': batch.model_dump(mode='json'), 'lanes': lanes})
    store.event(stage, 'incomplete' if partial else 'finished', candidates=len(batch.candidates),
                public_attempts_this_invocation=getattr(fetcher, 'requests', 0))
    return batch, lanes, partial


async def execute(store, *, check, history, collect=None, verify=None, fetcher=None):
    """Run to the LLM boundary; commits complete verification units before continuing."""
    from app.services.lead_hunter_discovery import DEFAULT_ADAPTERS, DiscoveryResult, collect_findings, verify_findings
    from app.integrations.lead_hunter_public import PublicFetcher

    collect, verify = collect or collect_findings, verify or verify_findings
    fetcher = fetcher if fetcher is not None else PublicFetcher(max_requests=100)
    with store.locked():
        manifest = store.load('run.json')
        if not manifest or manifest.get('version') != 1:
            raise ValueError('Каталог не является запуском Lead Hunter v1.')
        options = RunOptions.model_validate(manifest['options'])
        stage = 'discovery'
        try:
            cached = store.load('discovery.json')
            if cached is None:
                store.event(stage, 'started', lanes=list(options.lanes))
                adapters = (*DEFAULT_ADAPTERS, *(entry.adapter() for entry in options.sources)) if options.sources else None
                result = await collect(options.city, options.limit, lanes=options.lanes, fetcher=fetcher, adapters=adapters)
                # Do not freeze a provider outage as an empty successful search.
                if result.findings or result.providers_succeeded:
                    store.save('discovery.json', result.model_dump(mode='json'))
                else:
                    store.save('discovery_failed.json', result.model_dump(mode='json'))
                store.event(stage, 'finished', findings=len(result.findings),
                            successful_providers=len(result.providers_succeeded), failed_providers=len(result.providers_failed))
            else:
                result = DiscoveryResult.model_validate(cached)
                store.event(stage, 'reused', findings=len(result.findings))

            stage = 'verification'
            batch, lanes, public_partial = await _verify_batch(store, options, result, verify, fetcher, DiscoveryResult)

            stage = 'crm_identity'
            crm_partial = False
            if options.public_only or not batch.candidates or public_partial:
                reason = 'public_incomplete' if public_partial else 'public_only' if options.public_only else 'no_candidates'
                store.event(stage, 'skipped', reason=reason)
                store.event('crm_history', 'skipped', reason='identity_not_requested')
            else:
                raw = store.load('identity.json')
                if raw is None:
                    store.event(stage, 'started')
                    batch = await check(batch)
                    crm_partial = any(c.crm_check.state in {'not_checked', 'unavailable', 'incomplete'}
                                      or not all(c.crm_check.coverage.get(k) for k in ('company', 'contact', 'lead'))
                                      for c in batch.candidates)
                    if not crm_partial:
                        store.save('identity.json', batch.model_dump(mode='json'))
                    store.event(stage, 'incomplete' if crm_partial else 'finished', attempts=sum(batch.crm_read_audit.values()))
                else:
                    batch = ResearchBatch.model_validate(raw)
                    store.event(stage, 'reused')
                stage = 'crm_history'
                if crm_partial:
                    store.event(stage, 'skipped', reason='identity_incomplete')
                else:
                    store.event(stage, 'started')
                    for i, candidate in enumerate(batch.candidates):
                        key = f'history-{i:04}.json'
                        raw = store.load(key)
                        if raw is None:
                            one = batch.model_copy(update={'candidates': [candidate], 'crm_read_audit': {}})
                            one = await history(one)
                            review = one.candidates[0].relationship_review
                            available = review.status == 'complete' or review.coverage.get('collection_complete', False)
                            if available:
                                store.save(key, one.model_dump(mode='json'))
                            crm_partial |= not available
                            store.event(stage, 'checkpoint' if available else 'incomplete', index=i,
                                        attempts=sum(one.crm_read_audit.values()))
                        else:
                            one = ResearchBatch.model_validate(raw)
                            store.event(stage, 'reused', index=i)
                        batch.candidates[i] = one.candidates[0]
                        batch.crm_read_audit = dict(Counter(batch.crm_read_audit) + Counter(one.crm_read_audit))

            stage = 'classification'
            as_of = utcnow()
            crm_stale = (not options.public_only and not crm_partial and not public_partial and any(
                not _crm_fresh(c.crm_check.checked_at, as_of) or not _crm_fresh(c.relationship_review.checked_at, as_of)
                for c in batch.candidates))
            rows = list(project_rows(batch, as_of, lanes=lanes))
            store.event(stage, 'finished', candidates=len(rows), qualified=sum(r['qualified'] for r in rows))
            store.event('llm', 'deferred', reason='provider_not_activated', requests=0)
            status = ('discovery_unavailable' if not result.providers_succeeded and not result.findings
                      else 'partial_public' if public_partial
                      else 'no_verified_candidates' if not rows else 'public_only' if options.public_only
                      else 'partial_crm' if crm_partial else 'stale_crm_snapshot' if crm_stale else 'awaiting_llm')
            summary = dict(id=manifest['id'], city=options.city, status=status, started_at=manifest['started_at'],
                           completed_at=utcnow().isoformat(), evaluated_as_of=as_of.isoformat(),
                           counts=report_counts(rows, len(result.findings)), llm_calls=0, external_ai_cost_usd='0',
                           public_requests_this_invocation=getattr(fetcher, 'requests', 0),
                           crm_read_attempts_in_snapshot=sum(batch.crm_read_audit.values()),
                           providers_succeeded=result.providers_succeeded, providers_failed=result.providers_failed)
            stage = 'export'
            _report_files(store, batch, rows, summary)
            store.event(stage, 'finished', report='report.xlsx')
            return summary
        except (Exception, KeyboardInterrupt, asyncio.CancelledError) as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
            store.event(stage, 'interrupted' if interrupted else 'failed', error_type=type(exc).__name__)
            raise
