"""Bounded, private relationship evidence. Public research and CRM facts stay separate.

These records describe only the CRM objects visible to the read-only webhook.
An observed won deal/product row is deliberately not a performed-service fact.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PrivateRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class CRMReference(PrivateRecord):
    method: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    id: int = Field(gt=0)
    field: str = Field(min_length=1)
    date: datetime | None = None
    date_kind: Literal['event', 'card_modified', 'unknown'] = 'unknown'
    quote: str = Field(min_length=1, max_length=4000)
    checked_at: datetime | None = None


class RelationshipFact(PrivateRecord):
    text: str = Field(min_length=1, max_length=2000)
    reference: CRMReference
    service_key: str | None = None
    offer_key: str | None = None
    confidence: Literal['explicit', 'requires_review'] = 'requires_review'


class CRMProduct(PrivateRecord):
    id: int | None = None
    name: str
    quantity: str = ''
    reference: CRMReference


class CRMInteraction(PrivateRecord):
    id: int = Field(gt=0)
    kind: Literal['activity', 'comment']
    date: datetime | None = None
    subject: str = ''
    text: str = ''
    completed: bool | None = None
    direction: str | None = None
    author_id: int | None = None
    reference: CRMReference


class RelationshipEntity(PrivateRecord):
    entity: Literal['company', 'contact', 'lead', 'deal', 'quote']
    id: int = Field(gt=0)
    title: str
    identity: Literal['confirmed', 'possible'] = 'possible'
    company_id: int | None = None
    contact_id: int | None = None
    owner_id: int | None = None
    owner_name: str | None = None
    status_code: str | None = None
    status_name: str | None = None
    status_semantics: Literal['process', 'success', 'failure', 'unknown'] = 'unknown'
    category_id: int | None = None
    category_name: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    last_activity_at: datetime | None = None
    notes: str = ''
    custom_fields: dict[str, str] = Field(default_factory=dict)
    related_via: list[CRMReference] = Field(default_factory=list)
    references: list[CRMReference] = Field(default_factory=list)
    products: list[CRMProduct] = Field(default_factory=list)
    interactions: list[CRMInteraction] = Field(default_factory=list)


class RelationshipReview(PrivateRecord):
    status: Literal['not_reviewed', 'complete', 'partial', 'unavailable'] = 'not_reviewed'
    identity: Literal['confirmed', 'possible', 'none'] = 'none'
    relationship_state: Literal['active_customer', 'former_customer', 'lost_prospect',
                                'history_only', 'no_history', 'unknown'] = 'unknown'
    checked_at: datetime | None = None
    owner_name: str | None = None
    last_event_at: datetime | None = None
    last_interaction_at: datetime | None = None
    facts: list[RelationshipFact] = Field(default_factory=list)
    performed_services: list[RelationshipFact] = Field(default_factory=list)
    declined_offers: list[RelationshipFact] = Field(default_factory=list)
    new_requests: list[RelationshipFact] = Field(default_factory=list)
    do_not_contact: bool = False
    limitations: list[str] = Field(default_factory=list)
    entities: list[RelationshipEntity] = Field(default_factory=list)
    coverage: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode='after')
    def no_unconfirmed_customer_facts(self):
        if self.identity != 'confirmed' and self.relationship_state not in {'unknown', 'no_history'}:
            raise ValueError('Unconfirmed identity cannot establish customer relationship history')
        return self
