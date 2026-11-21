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

## Control center increment — v1.2.3 (2026-11-19)

The earlier audit above describes the v1.2.2 baseline; the following supersedes
its UI and product-intelligence gaps where explicitly noted:

- Implemented `/platform` browser control center in the existing FastAPI app,
  product CRUD/revisions, real Gemini product analysis, multiple editable audience
  segments and explicit approval, durable worker jobs, campaign progress/results,
  manager qualification review and local prepared handoffs. APIs reuse the shared
  models, SalesStore and LeadHunterApplication rather than a separate lead workflow.
- New product-driven adapter supports GDELT and approved public URLs with exact-quote
  verification. It does not inherit fixed CRM queries. Research target can be 20
  (shared bounded run limit now 50); no guarantee that 20 supported candidates exist.
- Secure cookie login, CSRF controls, login rate limits, request caps at deployment,
  audit logging and daily AI-call limits added. Private CRM data stays outside AI.
- Contact enrichment, fully general web/social discovery, actual CRM writes,
  production engagement, scheduled follow-up, and integrated business outcome
  ingestion are still incomplete. B2C discovery requires permissioned inbound access.
- CEO explicitly rejected the previous Kazakh voice quality. Voice status is
  **not approved**, and no prospect calls may run in this milestone.
- Audience approval is a CEO action; generated audience models are not automatically
  approved by implementation or deployment. A real discovery campaign waits for it.

See `docs/control_center.md` for actual commands, guards and current limitations.

## Verified deployment — v1.2.4 (2026-11-20)

- Production control center: `https://panel.whatsappcalls.kz/platform` on the
  existing VPS. Browser verified valid HTTPS, login page, unauthenticated state
  HTTP 401, authenticated state HTTP 200, Products, Target Audience, Discovery
  and Prospects rendering. Secure operator credentials are not in this repository.
- The requested apex `https://whatsappcalls.kz/platform` is NOT deployed publicly:
  apex DNS points to 185.22.67.34, whereas this VPS is 195.49.210.252. It returns
  404. A `/platform` reverse proxy on the apex website host is still required;
  changing the entire apex DNS would affect that website. Panel DNS and certificate
  already work. Existing telephony routes were preserved.
- Python dependencies installed; web and separate worker services active and
  enabled at boot, private backend port 8088, persistent SQLite under
  `/var/lib/lead-hunter`. A queued analysis survived restart and the worker
  completed it. Subsequent restart preserved authenticated session, product,
  completed analysis and both unapproved segments; heartbeat resumed.
- Real CRM Consulting product profile (ID 1, revision 1) saved through the API,
  using public seller website and CEO-provided services. No CRM history was sent
  to the model. Analysis job 2 completed via actual Gemini 3.8 Flash invocation:
  1,416 input / 1,042 output tokens recorded. Job 1 remains visibly failed: the
  provider returned 404 for 2.5 Flash and explicitly required migration. No
  synthetic replacement or hidden retry was used. Monetary billing not verified.
- Two generated audience hypotheses saved: retail/marketplace trading companies,
  and service/wholesale B2B direct-sales companies. Roles, motivations, signals,
  search queries and criteria are visible. BOTH remain pending CEO approval.
  Generated queries can attract vendors and criteria partly describe vendor
  selection, rather than prospect qualification: review/edit is required before
  discovery. Seller references do not verify buyer demand.
- Live unapproved campaign request returned 409 `audience_approval_required`;
  invalid handoff returned 409 `qualified_review_required`. Zero discovery jobs
  were launched, no prospects contacted, no production CRM records written.
- CRM connection read-only scope check succeeded, but configured credential lacks
  `crm` scope. `imopenlines` is available. Customer/history checks require a
  dedicated authorized read-only-use credential with CRM permission. Connector
  status remains unknown where Bitrix24 requires application context.
- Discovery, persistence, qualification and prepared handoff use the shared real
  backend; fixture tests verify these paths. Live discovery/qualification is NOT
  claimed verified because CEO approval is pending. No prospect records seeded.
- API capabilities report CRM writes, external messages and external calls false;
  panel has no production send/call executor. Voice quality remains not approved.
- Validation: local and VPS full pytest suites each 446 passed, one dependency
  deprecation warning; `node --check app/web/control.js` and `git diff --check`
  passed. Python 3.11 TLS mock compatibility fixed without changing runtime TLS.

### Reload fix — v1.2.5

Final full browser reload exposed the legacy host's `/favicon.ico` response with
`Clear-Site-Data: "cache", "cookies", "storage"`. The control center now declares
an inline favicon so its page does not request that legacy path. Existing host
routes are unchanged. Visiting other legacy pages on the same hostname may still
clear this session; keep the control center at `/platform`.

## Continuation of main — v1.5.7

- Baseline updated to `main` commit `1b1f569` (v1.5.6 changes). Integration branch
  `codex/main-control-center` includes that history plus the existing panel.
- Main's CLI, source modules, Kazakh/Russian templates, WhatsApp sender and PBX
  bridge preserved. PDF generator only receives bounded conversion timeout and
  support for application-owned output directories.
- Documents UI/API queues the existing generator on the durable worker and
  serves authenticated downloads. No sending endpoint is added.
- Knowledge UI reads an explicitly configured snapshot of the existing knowledge
  file. Live agent knowledge loading is not proven; no external AI transfer.
- Dedicated hostname `leadhunter.crmconsulting.kz` already has working DNS and
  HTTPS on the VPS. Reuse its existing configuration; do not change WhatsApp panel.
- Local validation: 449 tests passed, one dependency deprecation warning.
  Document worker tests mock the inherited generator; deployment must additionally
  verify real DOCX/PDF generation. New source engines are preserved, not yet
  wired into the web discovery selector. CRM history scope remains unavailable.

### Deployment verification — v1.5.8, 20 November 2026

- HTTPS login and Documents UI verified at `leadhunter.crmconsulting.kz/platform`.
  Existing product and two unapproved segments preserved. No discovery launched.
- Real Kazakh synthetic offer job 4 completed on worker, using inherited templates
  and LibreOffice. Authenticated downloads: PDF 64,527 bytes, DOCX 1,845,334 bytes;
  unauthenticated PDF access returned 401. No dispatch occurred.
- Initial synthetic job 3 failed because main's f-string syntax was incompatible
  with VPS Python 3.11. Minimal quoting fix preserves generator behavior; failure
  remains in history. XML autoescaping added for user-entered document text.
- Existing knowledge snapshot hash matches the deployed PBX knowledge file. It is
  shown read-only; live voice knowledge injection is still unverified.
- WhatsApp Nginx configuration remains byte-identical to its rollback backup.
  Existing dedicated LeadHunter DNS/certificate/Nginx configuration reused.
- 19 inherited Python files compile under VPS Python 3.11. No main CLI sending,
  calling or real discovery commands were executed during this verification.
- Final checks: VPS full suite 451 passed (including real RU/KZ DOCX rendering);
  local suite 449 passed, 2 skipped because local docxtpl is not installed. One
  dependency deprecation warning in each environment. `main.py --help` works;
  it still displays inherited CLI version 1.5.5 despite main's v1.5.6 commit label.

## Simplified discovery flow — v1.5.9 (21 November 2026)

- The first real UI searches (jobs 5 and 6) returned zero candidates. Audit data
  showed the panel selected only `product_public`, attempted seller-oriented CRM
  queries, used the CRM Consulting site as a candidate source, and GDELT failed
  by timeout/HTTP 429. This is a product-flow defect, not an operator error.
- An approved audience is now sufficient to start a search. Source selection,
  technical queries and seller-reference exclusion are application-owned. CRM
  automation in Shymkent automatically selects `shymkent_crm`, uses the existing
  local-signal discovery rules, and requests CRM READ ONLY review.
- Results expose the chosen human-readable method and temporary source failures.
  No operator must interpret adapters or compose source queries. No messages,
  calls or CRM writes are introduced by this change.
# Public-web research preparation — v1.5.12

- Added an optional, disabled-by-default `grounded_public_web` discovery adapter.
  It uses the already configured Gemini provider only for public source discovery
  and retains URLs solely from provider citations. Each candidate page is then
  independently fetched and verified by the existing bounded public-page path.
- The test-mode boundary is explicit: `CONTROL_AI_WEB_RESEARCH_ENABLED=false`
  by default; no CRM history is included in the external request; CRM remains
  read-only; messages, calls and CRM writes remain unavailable.
- Public Telegram support is deliberately limited to cited `t.me/s/<channel>`
  pages and still depends on a successful public fetch and evidence verification.
- Live enablement has not been performed: it may consume Gemini search-grounding
  quota and must be explicitly approved before a live market run.
