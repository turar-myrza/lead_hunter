# Universal CRM Consulting AI Platform — target architecture

Authority: CEO architectural directive, 2026-11-19. This is the full product
scope, not a claim that all stages exist. See IMPLEMENTATION_STATUS.md for evidence.

## Product and scope

One application accepts any product or service and orchestrates:

Product Input → Product Intelligence → Buyer Persona / ICP / Buying Group →
Source Strategy → Multi-Source Discovery → Evidence Verification → Decision-Maker
and Contact Enrichment → Lead Qualification and Scoring → Approved Bitrix24
Handoff → AI Sales Engagement → Follow-Up → Outcome Analytics and Learning.

The web interface is the control center. LeadHunter, Qualifier, Voice SDR,
Sales Team, CRM Analyst and other agents are capabilities of that application,
not separate products. Product intelligence must distinguish supported facts,
seller assertions, inferred use cases, alternatives, and unresolved questions.

Support both organizational and individual/family journeys. B2B can involve
users, technical evaluators, procurement, department heads, owners, approvers and
payers. B2C can involve different users, purchasers, payers, caregivers and
influencers. Do not infer health conditions or consent from product interest.
CRM services, cake trays, decompression belts and adult diapers are validation
briefs, not hard-coded workflows. Market and language are run inputs.

## Ownership and contracts

| Application stage | Responsibility and structured output |
|---|---|
| Product intelligence | ProductBrief → evidence-labelled value/use-case hypotheses and questions |
| Buyer modeling | ICP and buyer paths; role relationships; attributed decision authority |
| Source strategy | Capability-aware source plan with geography, access, budgets and gaps |
| Discovery | Candidate identities and source observations; no implied qualification |
| Verification | Identity, publication/check dates, provenance, conflicts and freshness |
| Enrichment | Organization/person identities, role evidence and permitted contact channels |
| Qualification | Separate need, offer fit, interest, contactability, permission and relationship history |
| CRM handoff | Reviewed mapping, duplicate resolution, named approval and idempotent receipt |
| Engagement | Approved opportunity/product/channel/content → bounded execution and delivery evidence |
| Follow-up | Conversation state, opt-out, owner, due date, next action and approval limits |
| Outcomes | Actual replies, meetings, completed meetings, wins/losses and revenue attribution |
| Learning | Reviewed rules supported by sufficient recorded outcomes; simulations excluded |

The orchestrator owns run IDs, stage transitions, checkpoints, errors,
cancellation, budgets, authorization and results. Services/adapters return typed
records; they do not silently initiate downstream actions. Preserve existing
ProductBrief, BuyerPath, Evidence, Candidate, Qualification, Opportunity and
InteractionEvent contracts, extending them with compatible migrations.

Persist separate product/run/opportunity identities and link evidence, contact
history, responsible owner, next action, approvals and outcomes. An agent result
or a generated proposal is not an executed action or observed business outcome.

## Control center

The UI must allow product entry, buyer-path review, source/access selection,
run launch/progress/errors, evidence inspection, qualification and role review,
relationship review, explicit CRM handoff approval, channel/content approval,
conversation follow-up and outcome reporting. It calls the same application
service as API/CLI. Do not implement a second orchestrator inside the UI.

## Sources and channels

Use an extensible capability registry with implemented/access-required/unavailable
states. Suitable adapters may cover public web, company sites, business events,
jobs, ad transparency sources, registries, procurement, partner introductions and
permissioned inbound requests. Availability must be demonstrated per adapter.
Do not claim universal search of social networks or private groups.

Bitrix24 Open Channels provides connected conversation access, not public
Telegram/Instagram/Facebook discovery. SIP is a transport for authorized calls,
not product intelligence or lead discovery. Retain separate adapters for approved
email, SMS and other channels without forcing all journeys through one channel.

## Safety and execution gates

Default to read-only research and draft engagement. Qualification is not consent.
CRM history, completed work, prior refusals and opt-outs override scoring.
Exclude prohibited industries; ambiguous cases require managerial review.
Do not expose internal commercial reconnaissance in customer-facing text.

CRM writes require a dedicated permissioned handoff adapter, item-level approval,
idempotency and audit receipts, separate from read-only clients. External AI gets
only approved minimized data. Secrets, private contacts and recordings stay out
of Git. No live messages/calls without authorization for recipients and limits.

## First validation phase — not the complete scope

1. Functional universal web control center over the existing application.
2. CRM Consulting implementation service as the first product; real discovery
   and qualification for a target of 20 potential buyers, with evidence and honest
   shortfall reporting. Never manufacture qualified opportunities to reach 20.
3. Voice-quality approval, then CEO approval of three selected qualified prospects,
   call content/channel and limits, before three live regular SIP sales calls.

Retain B2B/B2C fixtures and regression coverage through this phase. No prospect
call is authorized merely by this roadmap. Scope reduction needs explicit CEO
approval and a recorded decision; incomplete work remains in the target.
