"""Discovery/verification adapters for the shared LeadHunter workflow."""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from app.integrations.lead_hunter_public import PublicFetcher
from app.models.lead_hunter import Candidate as CRMCandidate
from app.models.sales_team import Prospect
from app.models.universal_lead_hunter import Candidate, Evidence, RunRequest, SourceCapability
from app.services.lead_hunter_discovery import DiscoveryFinding, collect_findings, verify_findings
from app.services.sales_discovery import discover_national


Discovered = TypeVar('Discovered')


@dataclass
class Discovery(Generic[Discovered]):
    payload: Discovered
    incomplete: bool = False


@dataclass
class VerifiedCandidate:
    candidate: Candidate
    legacy: CRMCandidate | Prospect | None = None


@dataclass
class Verification:
    records: list[VerifiedCandidate]
    incomplete: bool = False


class SourceAdapter(Protocol[Discovered]):
    capability: SourceCapability

    async def discover(self, request: RunRequest) -> Discovery[Discovered]: ...

    async def verify(self, request: RunRequest, discovery: Discovery[Discovered]) -> Verification: ...


class EvidenceIntake:
    capability = SourceCapability(id='evidence_intake', status='implemented',
        access='Operator-supplied evidence. Fixture mode labels every supplied record synthetic.',
        paths=['organization', 'individual'], modes=['fixture', 'research'])

    async def discover(self, request: RunRequest) -> Discovery[list[Candidate]]:
        return Discovery(request.supplied_candidates[:request.limit])

    async def verify(self, request: RunRequest, discovery: Discovery[list[Candidate]]) -> Verification:
        # Validate references again at the boundary. Operator assertions do not become verified facts.
        records = []
        for item in discovery.payload:
            candidate = Candidate.model_validate(item.model_dump())
            if request.mode == 'fixture':
                candidate.evidence = [e.model_copy(update={'basis': 'synthetic'}) for e in candidate.evidence]
            records.append(VerifiedCandidate(candidate))
        return Verification(records)


class ShymkentCRMDiscovery:
    capability = SourceCapability(id='shymkent_crm', status='implemented',
        access='Public HTTPS: GDELT DOC, Shymkent government RSS, and Otyrar RSS. Fixed CRM signals.',
        paths=['organization'], modes=['research'], context='crm_sales_operations', markets=['Шымкент', 'Shymkent'])

    def __init__(self):
        self.fetcher = PublicFetcher(max_requests=100)

    async def discover(self, request: RunRequest) -> Discovery[list[DiscoveryFinding]]:
        found = await collect_findings('Шымкент', request.limit, fetcher=self.fetcher)
        return Discovery(found.findings, incomplete=bool(found.providers_failed))

    async def verify(self, request: RunRequest, discovery: Discovery[list[DiscoveryFinding]]) -> Verification:
        findings = await verify_findings('Шымкент', discovery.payload, request.limit, fetcher=self.fetcher)
        records = []
        for index, candidate in enumerate(findings.candidates):
            sources = {s.id: s for s in candidate.sources}
            citations = [('identity', candidate.identity), *[('need', e) for e in candidate.evidence],
                         *[('business_contact', c.evidence) for c in candidate.contacts if c.type in {'email', 'phone'}]]
            evidence = [Evidence(id=f'citation-{i}', kind=kind, excerpt=citation.quote,
                        url=sources[citation.source_id].url, observed_at=sources[citation.source_id].checked_at,
                        published_on=sources[citation.source_id].published_on,
                        basis='public_page') for i, (kind, citation) in enumerate(citations)]
            shared = Candidate(id=f'shymkent-crm-{index}', label=candidate.name, path='organization', evidence=evidence)
            records.append(VerifiedCandidate(shared, candidate))
        return Verification(records, findings.verification_incomplete)


class KazakhstanSalesDiscovery:
    capability = SourceCapability(id='kazakhstan_sales', status='implemented',
        access='Public HTTPS: GDELT DOC, six regional government RSS feeds, and identified company sites. Fixed sales/CRM signals.',
        paths=['organization'], modes=['research'], context='crm_sales_operations', markets=['Казахстан', 'Kazakhstan'])

    async def discover(self, request: RunRequest) -> Discovery[list[Prospect]]:
        # This legacy service combines discovery and first-party source checking in one operation.
        from app.services.sales_team import deduplicate_prospects

        records, events = await discover_national()
        return Discovery(list(deduplicate_prospects(records))[:request.limit],
                         incomplete=any(e['status'] in {'unavailable', 'invalid_registry'} for e in events))

    async def verify(self, request: RunRequest, discovery: Discovery[list[Prospect]]) -> Verification:
        return Verification([VerifiedCandidate(Candidate(id=f'kazakhstan-sales-{index}', label=item.name,
            path='organization', evidence=[Evidence(id=f'source-{i}',
                kind='business_contact' if s.evidence_type == 'company_contact' else 'need' if i == 0 else 'identity',
                excerpt=s.fact, url=s.url, observed_on=s.observed_at, basis='public_page') for i, s in enumerate(item.sources)]), item)
            for index, item in enumerate(discovery.payload)])


# Factories create run-scoped adapters. For example, the public fetch cache is never shared across runs.
ADAPTERS = (EvidenceIntake, ShymkentCRMDiscovery, KazakhstanSalesDiscovery)
OTHER_SOURCES = (
    SourceCapability(id='configured_public_sites', status='unavailable',
        access='Legacy commands support operator-configured RSS, narrow indexes and official employer pages. No universal registry file input.', paths=['organization']),
    SourceCapability(id='approved_inbound_provider', status='requires_access',
        access='Needs an authorized first-party form or provider and a permission-event connector. Manual intake is available.', paths=['individual', 'organization']),
    SourceCapability(id='web_search', status='unavailable', access='No general web-search adapter.', paths=['organization']),
    SourceCapability(id='social_and_ads', status='unavailable',
        access='No public social, Telegram, or ad-library search. Bitrix24 Open Channels handles linked conversations separately.', paths=['organization']),
    SourceCapability(id='job_boards', status='unavailable', access='No third-party job-board adapter.', paths=['organization']),
    SourceCapability(id='business_and_procurement_registries', status='unavailable',
        access='No business or procurement registry adapter. Government RSS is a separate capability.', paths=['organization']),
)
