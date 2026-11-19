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
existing authorized Gemini service. Default model is gemini-2.5-flash. Optional
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
