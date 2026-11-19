# Implementation status and architecture audit

Audited 2026-11-19 against code at `886a836` (v1.2.2), local branch
`feature/universal-lead-hunter`, published branch `codex/lead-hunter-initial` in
https://github.com/turar-myrza/lead_hunter. This audit changes documentation only.
The untracked `tools/import_cake_leads.py` is excluded and preserved.

## Verdict and divergence

The repository contains a real shared application workflow, but not the complete
universal sales platform. No evidence of the recent changes deleting the universal
brief or B2C path was found. The risk is presenting specialized adapters, the
legacy sales dashboard or an externally operated voice test as the full product.

| Priority | Finding / code evidence | Required correction |
|---|---|---|
| High | `app/api/sales_team_dashboard.py:236` builds a separate read-only app; `app/main.py:29` presents CRM analytics links | Build product/run/review/action control UI on the shared API; retain analytics as a module |
| High | `app/services/universal_sources.py:62,90` live adapters use fixed CRM signals and Kazakhstan/Shymkent coverage | Treat these as first-product profiles; add product-driven discovery strategies without removing them |
| High | `app/services/universal_qualification.py:28` uses purpose-text matching and role templates | Add evidence-labelled product/ICP intelligence; current behavior is a heuristic, not semantic AI analysis |
| High | `app/models/universal_lead_hunter.py:161` ActionCapability fixes implemented/authorized to false; workflow only creates plans | Add reviewed CRM handoff and execution adapters, never bypass these gates |
| High | `app/models/universal_lead_hunter.py:84` caps request size at 15 | A 20-buyer campaign needs bounded pagination/batching with cross-batch deduplication and campaign totals |
| High | Generic intake has no verified CRM identifiers (`app/services/universal_lead_hunter.py:205`) | Extend shared identities and role/contact evidence before general-purpose CRM handoff |
| Medium | Supplied interaction history and textual follow-up plan are not conversation lifecycle management | Add canonical delivery/reply/meeting/outcome events and approved follow-up execution |
| Medium | API uses in-process BackgroundTasks; interrupted runs replay stages | Provide durable worker/resumption and visible cancellation/errors before calling it unattended production orchestration |

Line references identify the audited code snapshot, not all future revisions.

## Capability map

| Target capability | Actual state / evidence |
|---|---|
| Universal product input and B2B/B2C contracts | Implemented: `app/models/universal_lead_hunter.py`, ProductBrief, BuyerPath, BuyerRole, Candidate, Opportunity |
| Product intelligence / ICP | Limited heuristic and generic role hypotheses in `universal_qualification.understand`; no full AI-driven product intelligence in this workflow |
| Application orchestration | Implemented: `LeadHunterApplication.submit/process/status/result`, eight stages, persisted records and structured errors |
| CLI and API | Same service: `app/universal_lead_hunter.py`; `app/api/universal_lead_hunter.py`; API mounted in `app/main.py` |
| Persistence | Existing SQLite SalesStore; separate universal queue origin; per-run locking and artifacts |
| Source strategy | Capability/market/path/context selection; not autonomous discovery of new source types |
| Sources | EvidenceIntake; ShymkentCRMDiscovery; KazakhstanSalesDiscovery. GDELT/RSS and company-site verification reused from legacy components |
| Missing discovery | General web search, public social/ad libraries, third-party job boards and business/procurement registry adapters unavailable in this checkout |
| Evidence / contacts / roles | Typed provenance, citation checks and legacy public verification; generalized decision-maker enrichment absent; supplied data is not independently verified by intake |
| CRM history | Existing read-only matching/history reused for legacy organizational records; generic identities remain unsupported |
| Qualification | Legacy CRM/sales decisions plus generic review states; synthetic fixtures can qualify, generic supplied research requires review |
| CRM handoff | Not implemented in universal workflow; no approved write executor or handoff receipt |
| Engagement | Review-only plans; legacy outreach generation; separately gated Open Channels drafts/test replies |
| Follow-up | Plan text and local receipt deduplication; no unified scheduled follow-up worker |
| Outcomes / learning | Legacy SalesStore metrics and `sales_team.learning_evaluation`; not connected as a full universal opportunity feedback loop |
| Web UI | Legacy local read-only dashboard and FastAPI documentation; no universal control center |
| Universal fixtures | Cake-tray organizational roles and synthetic inbound family belt path; CRM/adult-diaper brief validation in tests |
| Voice SDR | Legacy simulation/handoff code and separate VPS PBX; live CEO test was operated by Codex, not this orchestrator |

## Verified execution and limits

- v1.2.2 verification previously passed 438 automated tests with one dependency
  deprecation warning. This documentation-only audit did not rerun them or claim
  that mock tests establish production capability.
- Existing local `.venv` has a native dependency issue; previous tests used the
  bundled Python runtime with project dependencies. This remains a deployment gap.
- Live Open Channels read returned `imopenlines` access and enabled lines 11,
  45 and 47. Connector-level statuses require existing application context and
  remain unknown. Polling exists; no real-time listener/scheduler is installed.
- The 2026-11-19 CEO-only regular SIP test was answered through the existing
  `b24-ai-endpoint`, using configured caller ID +77009121212. Call ID
  `1795087479.2`; answer-to-hangup approximately 101.5 seconds; recording 100.18
  seconds; gateway reported 7 AI turns and kk-KZ test mode. User barge-in was logged.
  This does not verify Kazakh fluency, correct sales dialogue or prospect readiness.
  Voice-quality approval is still pending. No transcript was produced.
- The test used an isolated local gateway and dialplan, blocked application CRM
  calls and omitted CRM-sync hooks. Native carrier/Bitrix telephony bookkeeping was
  not audited; do not claim no provider-side records were created.
- This was one manually operated infrastructure test, not a universal campaign
  execution. Private recordings/config backups remain on the VPS, not in Git.
  After the call, the temporary gateway was stopped, backed-up configuration
  restored, and the previously stopped regular PBX returned to its stopped state.
  The original primary inbound container and gateway remained running.
- No 20-buyer research campaign or three-prospect call campaign was run in this audit.

## Corrected first-stage implementation plan

1. Keep the shared product input and all buyer paths. Build the web control panel
   over existing submit/status/result/source endpoints, adding review and approval
   views without another orchestration engine. Show evidence and missing capabilities.
2. Extend product/use-case/ICP and identifier contracts compatibly. Keep facts,
   hypotheses, contactability and permission separate. Add canonical opportunity,
   action-approval and outcome IDs; preserve legacy entry points through adapters.
3. Implement capability-aware product query strategies and supported sources.
   Use the existing CRM profiles for the first product. Add campaign batching and
   stable identity deduplication to research 20 candidates; report verified,
   qualified, excluded and unresolved counts separately.
4. Connect manager review to an explicit, idempotent approved Bitrix24 handoff.
   Keep current read-only client unchanged; verify relationship history and owner.
5. Wrap the proven regular SIP transport in an authorized action executor with
   recipient/content/version approval, attempt caps and receipts. First obtain
   voice-quality approval, then explicit CEO approval for three qualified prospects.
6. Connect inbound replies, follow-up, meetings and actual revenue/loss outcomes
   to the same opportunity ledger. Learning changes require evidence and review.

Acceptance for this phase is a coherent UI-led product-to-opportunity journey,
an evidenced CRM-service campaign and separately authorized calls. It is not
acceptance of all target capabilities. Preserve tests for other products and B2C.

## Decisions

- 2026-11-19: Adopt TARGET_ARCHITECTURE.md as full scope; CRM implementation is
  the first validation product only. No scope reduction approved.
- Preserve working legacy components behind the application; do not rewrite or
  discard them because they are currently specialized.
- Connected-conversation access and public social discovery remain separate.
- An externally run live SIP test is infrastructure evidence, not proof of an
  integrated Voice SDR or authorization for further calls.
- Before a feature, state its target-stage ownership, reused models/services,
  approval/data boundaries and acceptance evidence; record any proposed scope change.
