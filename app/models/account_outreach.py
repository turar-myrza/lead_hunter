"""Local, reviewable Account Research drafts and explicitly entered outcomes.

These records do not represent permission to send anything.  They contain no
CRM connection, delivery transport or automatic sales attribution.
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.models.lead_hunter import Record
from app.models.outreach_copy import ManagerOutreachCopy, validate_no_verbatim_research, validate_single_cta


def normalized(value: str) -> str:
    return ' '.join(value.casefold().split())


def past_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value > datetime.now(timezone.utc):
        raise ValueError('Timestamp must be timezone-aware and not in the future')
    return value


class AccountFact(Record):
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=500)
    fact: str = Field(min_length=12)


class AccountOffer(Record):
    product_key: str = Field(min_length=1)
    offer_key: str = Field(min_length=1)
    solution: str = Field(min_length=8)
    concrete_offer: str = Field(min_length=20)


class BusinessAddressee(Record):
    role: str = Field(min_length=3)
    name: str | None = None
    channel_type: Literal['phone', 'email', 'public_page', 'website']
    channel_value: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    role_confirmed: bool = False
    limitation: str | None = None

    @model_validator(mode='after')
    def unknown_role_is_explicit(self):
        if not self.role_confirmed and not self.limitation:
            raise ValueError('An unconfirmed addressee role needs an explicit limitation')
        if self.name and not self.role_confirmed:
            raise ValueError('Do not invent a named addressee')
        return self


class RecommendedApproach(Record):
    method: Literal['whatsapp', 'email', 'useful_material', 'call']
    reason: str = Field(min_length=20)


class DraftEmail(Record):
    subject: str = Field(min_length=8)
    body: str = Field(min_length=120)
    # Legacy fragment fields remain readable as internal provenance.
    fact_refs: list[str] = Field(default_factory=list)
    confirmed_fact_fragment: str | None = Field(default=None, min_length=12)
    hypothesis_fragment: str | None = Field(default=None, min_length=20)
    offer_fragment: str | None = Field(default=None, min_length=20)


class UsefulMaterial(Record):
    title: str = Field(min_length=8)
    path: str = Field(min_length=1)

    @field_validator('path')
    @classmethod
    def local_material(cls, value):
        if not Path(value).is_absolute() or '://' in value:
            raise ValueError('Useful material must reference an absolute local file')
        return value


class AccountOpportunityBrief(Record):
    version: Literal['1.0'] = '1.0'
    candidate_name: str = Field(min_length=1)
    category: Literal['A', 'B', 'C', 'D']
    facts: list[AccountFact] = Field(min_length=3, max_length=5)
    business_context: str = Field(min_length=20)
    hypothesis: str = Field(min_length=20)
    offer: AccountOffer
    why_now: str | None = None
    urgency_unconfirmed: bool = True
    addressee: BusinessAddressee
    recommended_approach: RecommendedApproach
    email: DraftEmail
    whatsapp_text: str | None = Field(default=None, min_length=220, max_length=650)
    call_script: str = Field(min_length=100)
    useful_material: UsefulMaterial
    next_step: str = Field(min_length=20)
    risks: list[str] = Field(min_length=1)
    cta: str = Field(min_length=10)
    target_metric: str = Field(min_length=10)
    basis_source_ids: list[str] = Field(min_length=1)
    new_basis_refs: list[AccountFact] = Field(default_factory=list)
    reviewed_at: datetime
    reviewed_by: str = Field(min_length=2)
    content_reviewed: bool = False
    status: Literal['draft'] = 'draft'
    _reviewed = field_validator('reviewed_at')(past_timestamp)

    @model_validator(mode='after')
    def traceable_personal_draft(self):
        if self.category == 'D':
            raise ValueError('No commercial drafts for EXCLUDED / HOLD')
        facts = {fact.id: fact for fact in self.facts}
        if len(facts) != len(self.facts):
            raise ValueError('Fact IDs must be unique')
        if len({(f.source_id, normalized(f.quote)) for f in self.facts}) != len(self.facts):
            raise ValueError('Do not duplicate facts to satisfy the dossier minimum')
        if set(self.email.fact_refs) - facts.keys():
            raise ValueError('Email facts must reference this brief')
        fragment = self.email.confirmed_fact_fragment
        if fragment and normalized(fragment) not in {normalized(facts[key].fact) for key in self.email.fact_refs}:
            raise ValueError('A legacy fact fragment must be a referenced, reviewed fact')
        if self.email.hypothesis_fragment and normalized(self.email.hypothesis_fragment) != normalized(self.hypothesis):
            raise ValueError('A legacy hypothesis fragment must match the internal hypothesis')
        if self.email.offer_fragment and normalized(self.email.offer_fragment) != normalized(self.offer.concrete_offer):
            raise ValueError('A legacy offer fragment must match the dossier offer')
        if self.whatsapp_text is not None:
            self.manager_copy()
            validate_single_cta(self.call_script, self.cta)
            validate_no_verbatim_research(
                (self.whatsapp_text, self.email.subject, self.email.body, self.call_script),
                (fact.quote for fact in self.facts),
            )
        if not self.urgency_unconfirmed and not self.why_now:
            raise ValueError('Confirmed timing needs an explicit reason')
        if self.why_now and normalized(self.why_now) == normalized(self.hypothesis):
            raise ValueError('A hypothesis is not proof of timing')
        if set(self.basis_source_ids) - {f.source_id for f in self.facts}:
            raise ValueError('Every basis source needs a recorded fact')
        if self.recommended_approach.method == 'email' and self.addressee.channel_type != 'email':
            raise ValueError('Email cannot be recommended without a published business email')
        if self.recommended_approach.method == 'call' and self.addressee.channel_type != 'phone':
            raise ValueError('A call needs a published business phone')
        if self.recommended_approach.method == 'whatsapp' and self.addressee.channel_type != 'phone':
            raise ValueError('WhatsApp needs a published business phone')
        return self

    def manager_copy(self) -> ManagerOutreachCopy:
        method = self.recommended_approach.method
        return ManagerOutreachCopy(whatsapp_text=self.whatsapp_text, email_subject=self.email.subject,
                                   email_text=self.email.body, cta=self.cta,
                                   recommended_channel='phone' if method == 'call' else method)


class OutcomeEvidence(Record):
    kind: Literal['local_artifact', 'manager_record', 'public_reference', 'crm_read_reference']
    reference: str = Field(min_length=3)
    description: str = Field(min_length=12)


class AccountOutcomeEvent(Record):
    event_id: str = Field(min_length=5)
    candidate_name: str = Field(min_length=1)
    event_type: Literal['prepared', 'approved', 'sent_by_manager', 'replied', 'meeting', 'proposal', 'contract', 'revenue']
    occurred_at: datetime
    entered_at: datetime
    entered_by: str = Field(min_length=2)
    evidence: OutcomeEvidence
    amount: Decimal | None = Field(default=None, gt=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern='^[A-Z]{3}$')
    _occurred = field_validator('occurred_at')(past_timestamp)
    _entered = field_validator('entered_at')(past_timestamp)

    @model_validator(mode='after')
    def explicit_outcome(self):
        if self.entered_at < self.occurred_at:
            raise ValueError('An outcome cannot be entered before it occurred')
        if self.event_type == 'revenue':
            if self.amount is None or not self.currency:
                raise ValueError('Revenue needs an explicitly entered amount and currency')
            if self.evidence.kind == 'local_artifact':
                raise ValueError('A prepared file is not evidence of revenue')
        elif self.amount is not None or self.currency is not None:
            raise ValueError('Only explicit revenue events carry recognized revenue')
        if self.event_type not in {'prepared', 'approved'} and self.evidence.kind == 'local_artifact':
            raise ValueError('A local draft does not prove contact, response or a sale')
        return self


class AccountOutcomeLedger(Record):
    version: Literal['1.0'] = '1.0'
    events: list[AccountOutcomeEvent] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_events(self):
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError('Outcome event IDs must be unique')
        return self
