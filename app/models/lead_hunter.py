"""Evidence records for the Codex-operated Lead Hunter. No CRM credentials."""

from datetime import date, datetime, timezone
from enum import Enum
import ipaddress
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Criterion(str, Enum):
    INBOUND = 'active_inbound'
    TEAM = 'multiple_managers'
    CHANNELS = 'communication_channels'
    GROWTH = 'growth_or_processing_problems'
    CRM = 'crm_or_automation'


class CommercialSignal(str, Enum):
    PROCESSING_PROBLEM = 'processing_problem'
    AUTOMATION_PROJECT = 'automation_project'
    GROWTH = 'growth'
    INBOUND = 'inbound'
    TEAM = 'team'
    CHANNELS = 'channels'
    ORDER_COMPLEXITY = 'order_complexity'
    EXISTING_CRM = 'existing_crm'


WEIGHTS = {Criterion.INBOUND: 25, Criterion.TEAM: 20, Criterion.CHANNELS: 20,
           Criterion.GROWTH: 20, Criterion.CRM: 15}


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password:
        raise ValueError('Only public HTTP(S) URLs without credentials are accepted')
    if parsed.port not in {None, 80, 443} or '.' not in host or host.endswith(('.local', '.localhost', '.internal', '.test', '.example')):
        raise ValueError('A public business source is required')
    try:
        address = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError('Private IP sources are forbidden')
    if '/rest/' in parsed.path.lower():
        raise ValueError('CRM webhook URLs are forbidden')
    return value


class Source(Record):
    id: str = Field(min_length=1)
    url: str
    title: str = Field(min_length=1)
    checked_at: datetime
    published_on: date | None = None
    valid_until: date | None = None
    vacancy_status: str = Field(default='unknown', pattern='^(active|archived|closed|unknown|not_applicable)$')
    status_evidence: str | None = None
    last_attempt_at: datetime | None = None
    last_attempt_status: str = Field(default='success', pattern='^(success|unavailable|restricted)$')
    kind: str = Field(pattern='^(company_site|vacancy|directory|public_page)$')
    access: str = Field(pattern='^(public_page|search_excerpt)$')
    excerpt: str = Field(min_length=1, max_length=4000)
    limitation: str | None = None
    _url = field_validator('url')(public_url)

    @model_validator(mode='after')
    def check_provenance(self):
        if self.checked_at.tzinfo is None or self.checked_at > datetime.now(timezone.utc):
            raise ValueError('Check timestamp must be timezone-aware and not in the future')
        if self.access == 'search_excerpt' and not self.limitation:
            raise ValueError('Search-only sources require an explicit limitation')
        if self.published_on and self.published_on > self.checked_at.date():
            raise ValueError('Publication cannot be later than verification')
        if self.last_attempt_at and (self.last_attempt_at.tzinfo is None or self.last_attempt_at > datetime.now(timezone.utc)):
            raise ValueError('Attempt timestamp must be timezone-aware and not in the future')
        if self.vacancy_status in {'active', 'archived', 'closed'}:
            if not self.status_evidence or ' '.join(self.status_evidence.casefold().split()) not in ' '.join(self.excerpt.casefold().split()):
                raise ValueError('Vacancy status requires a verbatim source excerpt')
        return self


class Evidence(Record):
    source_id: str
    quote: str = Field(min_length=1, max_length=500)
    fact: str = Field(min_length=1)
    criterion: Criterion | None = None
    signal: CommercialSignal | None = None
    scope: str = Field(default='local', pattern='^(local|network)$')


class Contact(Record):
    type: str = Field(pattern='^(phone|email|public_page|website)$')
    value: str = Field(min_length=1)
    evidence: Evidence
    role: str = Field(default='company', pattern='^(company|sales|decision_maker|recruitment)$')
    person_name: str | None = None
    role_evidence: Evidence | None = None


class PublicResponsible(Record):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    evidence: Evidence
    scope: str = Field(default='local', pattern='^(local|network)$')


class Candidate(Record):
    name: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    city: str = Field(pattern='^Шымкент$')
    website: str
    identity: Evidence
    geography: Evidence
    sources: list[Source] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    contacts: list[Contact] = Field(default_factory=list)
    products: list[str] = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    next_step: str | None = None
    purchase_evidence: list[Evidence] = Field(default_factory=list)
    public_responsibles: list[PublicResponsible] = Field(default_factory=list)
    crm_check: 'CRMCheck' = Field(default_factory=lambda: CRMCheck())
    relationship_review: 'RelationshipReview' = Field(default_factory=lambda: RelationshipReview())
    crm_usage_evidence: 'CRMUsageAssessment' = Field(default_factory=lambda: CRMUsageAssessment())
    commercial_proposal: 'CommercialProposal | None' = None
    _website = field_validator('website')(public_url)

    @model_validator(mode='after')
    def check_evidence(self):
        sources = {s.id: s for s in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError('Duplicate source IDs')
        refs = [self.identity, self.geography, *self.evidence, *self.purchase_evidence,
                *(c.evidence for c in self.contacts), *(c.role_evidence for c in self.contacts if c.role_evidence),
                *(p.evidence for p in self.public_responsibles)]
        refs.extend(self.crm_usage_evidence.evidence)
        if self.commercial_proposal:
            refs.extend([self.commercial_proposal.confirmed_fact, *self.commercial_proposal.new_basis])
        normalize = lambda text: ' '.join(text.casefold().split())
        for item in refs:
            source = sources.get(item.source_id)
            if not source or normalize(item.quote) not in normalize(source.excerpt):
                raise ValueError('Every evidence quote must occur in its recorded source excerpt')
            analytical = (getattr(item, 'criterion', None) or getattr(item, 'signal', None)
                          or item in self.purchase_evidence or item in self.crm_usage_evidence.evidence
                          or (self.commercial_proposal and item in [self.commercial_proposal.confirmed_fact, *self.commercial_proposal.new_basis]))
            if analytical and source.access != 'public_page':
                raise ValueError('Search-only evidence cannot earn qualification points')
        if not any(e.criterion or e.signal for e in self.evidence):
            raise ValueError('Candidate needs at least one confirmed qualification signal')
        if 'шымкент' not in normalize(self.geography.quote):
            raise ValueError('Explicit Shymkent location evidence required')
        for contact in self.contacts:
            if contact.type == 'website':
                if sources[contact.evidence.source_id].kind != 'company_site' or contact.value.rstrip('/') != sources[contact.evidence.source_id].url.rstrip('/'):
                    raise ValueError('Official website must match the opened company-site source URL')
            elif normalize(contact.value) not in normalize(contact.evidence.quote):
                raise ValueError('Contact must appear verbatim in its evidence')
            if sources[contact.evidence.source_id].access != 'public_page':
                raise ValueError('Contacts require an opened public source')
            if contact.type in {'website', 'public_page'}:
                public_url(contact.value)
            if contact.role in {'sales', 'decision_maker'} and contact.role_evidence is None:
                raise ValueError('A named business role requires source evidence')
            if contact.person_name and (not contact.role_evidence or normalize(contact.person_name) not in normalize(contact.role_evidence.quote)):
                raise ValueError('Named contact must be supported by role evidence')
        for person in self.public_responsibles:
            if normalize(person.name) not in normalize(person.evidence.quote) or normalize(person.role) not in normalize(person.evidence.quote):
                raise ValueError('Responsible name and role must appear in source evidence')
            if sources[person.evidence.source_id].access != 'public_page':
                raise ValueError('Responsible person requires an opened public business source')
        return self


class ResearchBatch(Record):
    version: str = Field(default='1.0', pattern='^(1.0|1.1|1.2)$')
    mode: str = Field(default='pilot', pattern='^(pilot|expanded)$')
    candidates: list[Candidate] = Field(max_length=30)
    search_log: list[str] = Field(min_length=1)
    skipped_sources: list[str] = Field(default_factory=list)
    crm_read_audit: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode='after')
    def pilot_limit(self):
        if self.mode == 'pilot' and len(self.candidates) > 5:
            raise ValueError('Pilot is limited to five companies; review before expanding')
        return self


from app.models.lead_hunter_crm import CRMCheck  # No network/config import.
from app.models.lead_hunter_relationships import RelationshipReview
from app.models.lead_hunter_routing import CRMUsageAssessment, CommercialProposal

Candidate.model_rebuild()
ResearchBatch.model_rebuild()
