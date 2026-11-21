# Universal control center v1.2.3

The authenticated `/platform` application runs inside `app.main:app`. It uses
ProductBrief and the existing SQLite SalesStore; additive `cc_*` tables retain
product revisions, analysis jobs, audience approvals, campaign snapshots,
qualification reviews, prepared handoffs, audit history and AI usage.

## Run

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8088
# Separate process; queued jobs persist if the web process exits:
python -m app.control_worker
```

Set `LEAD_HUNTER_API_TOKEN` to a high-entropy private operator password, and
`LEAD_HUNTER_DB` / `LEAD_HUNTER_OUTPUT` to private writable locations. Configure
`CONTROL_AI_ENABLED=true`, `CONTROL_GEMINI_KEY` and `CONTROL_AI_MODEL` for the
existing authorized Gemini service. Default model is gemini-3.8-flash. Optional
`CONTROL_AI_PROXY` uses an operator-controlled proxy. `CONTROL_AI_DAILY_CALLS`
defaults to 30 including failed/reserved calls, with 6,000 output tokens/request.
No automatic retries, provider fallback, subscriptions or key acquisition.
Usage records contain token counts when returned; they are not a monetary budget.
Provider charges/free-tier availability must be checked on the existing account.

Use HTTPS behind a reverse proxy. Only expose `/platform` and its descendants;
keep the older analytics/API routes on the internal backend. Set forwarded Host
and scheme from the trusted proxy. Limit request bodies to 128 KiB. Browser login
uses a 12-hour Secure/HttpOnly/SameSite=Strict cookie. Password remains server-side;
there is no token in browser localStorage. Mutations require a custom same-origin
header and Origin validation. Login attempts are limited in SQLite. This is one
internal operator workspace, not multi-tenant identity/role management.

## User journey

1. Create/edit a product: category, description, market, pricing, advantages,
   customer problems, public references and text material. Explicitly authorize
   external AI processing of public material only. File ingestion is not implemented.
2. Analyze: a durable queued job calls the actual AI provider and validates typed
   output. Failed calls show an error; no template fallback poses as AI output.
3. Review multiple audience hypotheses; edit buyer characteristics/roles, signals,
   queries and criteria; explicitly approve a segment. Product edits revoke approvals.
4. Launch discovery, target 20. The service snapshots the product and approved
   segment, submits a shared LeadHunterApplication run, and displays its eight stages.
5. Inspect source quotations and checked dates, inferred fit, roles and missing
   contacts. Review qualification with cited evidence and a next action.
6. Prepare an immutable-purpose local handoff draft (editable by a later review),
   never a CRM write. Prior CRM relationship review and production authorization
   are still required. Existing conservative name matching returns possible matches,
   not confirmed duplicates based on names alone.

## Real discovery boundaries

ProductDiscovery implements the shared source contract. It uses approved
product-derived queries against public GDELT DOC and reviewer-supplied public URLs,
then reuses PublicFetcher (HTTPS/DNS/robots/size/request restrictions) and visible
page parsing. AI extracts named organizations; exact identity/location/need/role
quotations must be present in fetched text. Current matching requires the selected
market text in the location quotation. This may miss transliterations. It cannot
verify a page's truth or company ownership merely from an exact quote.

Contacts are intentionally not inferred from unrelated publication footers.
Contact ownership/deliverability and general-purpose enrichment remain incomplete.
Industry labels and fit remain model hypotheses. Evidence completeness in the UI
is identity 20 + need 40 + role 20 + business contact 20; it is not buying intent
or an automatic qualification decision. Actual review decisions have rationale.

GDELT may be unavailable or yield few companies. Errors and shortfalls persist;
no archive or seeded prospects fill the target. This is not a broad search-engine,
social-network, Telegram-group, or advertising-library adapter. B2C is retained
in product analysis and the shared workflow, but discovery from this UI requires
an authorized inbound adapter before it can run for an individual segment.

## Operations and safety

One worker owns the control lock. Interrupted running jobs become explicit failures
on restart to prevent silently replaying externally billed requests; queued jobs
are retained. API job state/results and worker heartbeat are visible in Analytics.
A retry is a new explicit analysis/campaign request, not a hidden background retry.

CRM access uses existing read-only clients. No write, call or message endpoint is
provided here. Kazakh voice is not approved by the CEO; the old infrastructure
test is not a platform campaign. No private CRM history is sent to the AI provider.
Prepared follow-up plans are not scheduled messages. Real sales/meeting conversions
are not invented; outcome ingestion into the universal ledger remains future work.

Keep server environment, SQLite, private material, reports and recordings out of
Git. The worker and web service should run as an unprivileged account, with only
state/output directories writable. Preserve old inbound telephony services.

## Deployed access (20 November 2026)

Open `https://panel.whatsappcalls.kz/platform` and use the operator password
provided privately by the deployment operator. Do not put credentials in GitHub.
The browser stores a Secure, HttpOnly, SameSite=Strict session cookie. Review
**AI Product Intelligence**, then **Target Audience** for product 1. Generated
segments are hypotheses and remain unapproved. Review queries and prospect
qualification criteria before approving; approval is not outreach permission.

The apex `whatsappcalls.kz` currently resolves to another website host. Serving
`/platform` there requires a path proxy on that host; the working panel address
needs no DNS change. Services: `lead-hunter-web`, `lead-hunter-worker`; persistent
state: `/var/lib/lead-hunter`; protected settings: `/etc/lead-hunter/control.env`.
Use `systemctl status` to check services without printing environment values.
Current model: Gemini 3.8 Flash, verified by a completed real analysis. Configured
Bitrix24 credential has Open Channels but not CRM scope; relationship-history
checks remain blocked until CRM permission is supplied. No live discovery or
outreach was performed in this deployment verification.

## Main-branch continuation (v1.5.7)

Integration branch `codex/main-control-center` starts from main `1b1f569` (v1.5.6
commit), then merges the previously implemented control center. The main CLI,
procurement, Telegram, calling and WhatsApp dispatch modules are preserved.
This is not a replacement workflow or a new project.

Dedicated URL: `https://leadhunter.crmconsulting.kz/platform` (root redirects).
DNS A record: `leadhunter` -> `195.49.210.252`. No change to the WhatsApp panel.

Documents uses `documents.offer_generator.generate_commercial_offer` in the
existing durable worker. Select a product, enter recipient/items/language and
review the existing template's seller branding. The templates are inherited
from main, not automatically branded as CRM Consulting. Download DOCX/PDF after
completion; no dispatch is triggered. LibreOffice Writer and docxtpl are required.
Files stay in `LEAD_HUNTER_OUTPUT/offers/<job_id>` with authenticated downloads.
The existing CLI WhatsApp/template sender remains available but not authorized
or invoked by this panel. Existing call bridge remains unchanged.

Knowledge Base displays the operator-configured `CONTROL_KNOWLEDGE_PATH` file
as a read-only deployment snapshot. It neither changes the PBX file nor transfers
its contents to an external model. Live voice-agent knowledge loading is not
verified. To refresh, the operator copies the existing PBX knowledge file into
the protected application data directory and keeps it readable only by the service.

The existing main source engines remain CLI components. They are not silently
advertised as new authenticated UI discovery adapters. Product-derived discovery
still requires CEO audience approval. Broader adapter wiring is a subsequent
integration task, not claimed complete by this deployment.

## Simple discovery flow (v1.5.9)

The operator does not select GDELT, RSS, search phrases, or source URLs. The
only business decisions are the product profile, market and approval of the
generated target audience. After approval, **Find up to 20 companies** chooses
the source strategy itself. For CRM/Bitrix24/automation services in Shymkent it
uses the existing `shymkent_crm` adapter with the `crm_sales_operations` profile
and requests a Bitrix24 READ ONLY check. Seller reference URLs, such as the
CRM Consulting site, are never treated as buyer sources.

The UI shows the human-readable method and any temporarily unavailable public
source after a run. Zero candidates means no evidence-backed organizations were
retained; it never means that companies were invented. Messaging, calls and
production CRM writes remain outside this workflow.

## Public web research test mode

The normal operator flow remains **product → approve audience → find companies**.
The operator does not configure queries, URLs, Telegram channels, or adapters.

When `CONTROL_AI_WEB_RESEARCH_ENABLED=true` is explicitly enabled alongside the
already configured public-material Gemini analysis, the application adds the
`grounded_public_web` adapter to an organizational run. It asks the configured
provider to discover cited public web pages only. Those citations are discovery
hints, not evidence: the application fetches each page again through
`PublicFetcher`, honoring robots.txt, request limits, blocks and CAPTCHA
responses, then extracts evidence from the page itself.

Public Telegram is limited to cited `https://t.me/s/<public-channel>` pages.
Private groups, invite links, account pages, login-only content, retained
sessions and access-control workarounds are excluded. No CRM data is sent to
the provider. This mode still prepares review-only opportunities: it cannot
create Bitrix24 records, send messages, or place calls.
