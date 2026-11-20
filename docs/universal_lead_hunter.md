# Universal LeadHunter, stage 1

The existing FastAPI application and `app.lead_hunter universal` CLI submit the same `RunRequest` to `LeadHunterApplication`. Pydantic supplies the canonical contract for JSON and the generated OpenAPI schema. The workflow owns eight stages: brief interpretation, source planning, discovery, verification, CRM review, qualification, engagement planning, and export. Each run has a numeric ID, stage progress, structured errors, and a retrievable result. The default private SQLite store is `data/sales_team/universal.sqlite3`, using the existing Sales Team store and a separate queue origin. Workbook artifacts are under `outputs/lead_hunter/universal/<run_id>/`.

A product brief contains a name, description, category, market, optional known problem, and one or both buyer paths. Brief interpretation uses explicit problem text or a purpose clause to generate a hypothesis and open questions. Organization paths model process users, procurement, technical evaluators, department heads, owners, approvers, and payers. Individual paths ask which roles belong to the user, payer, caregiver, and family decision-maker. The role templates are hypotheses. Candidate-specific roles cite evidence and remain labelled with its provenance. Product category, CRM relationship, qualification, purchase interest, contactability, and permission are separate fields.

## Launch

From the repository root with the dependencies in `requirements.txt` installed in a working Python environment:

```sh
.venv/bin/python -m app.lead_hunter universal submit tests/fixtures/universal/cake_trays.json
.venv/bin/python -m app.lead_hunter universal submit tests/fixtures/universal/decompression_belt.json
.venv/bin/python -m app.lead_hunter universal status RUN_ID
.venv/bin/python -m app.lead_hunter universal result RUN_ID
.venv/bin/python -m app.lead_hunter universal sources
```

`submit` prints a run ID before processing. Use `submit --queue-only REQUEST.json` followed by `process RUN_ID` to launch later. `process` on an interrupted queued/running run replays the read-only stages from the stored input, under a per-run lock. Completed and failed runs remain available for inspection. `partial` presents usable results and the affected stages. A failed run exposes the failing stage. The legacy LeadHunter `run`, `export`, and CRM commands, and the Sales Team `run`, `simulate`, and dashboard commands keep their existing arguments and behavior.

For the internal API, configure `LEAD_HUNTER_API_TOKEN` in the environment with a private bearer token. Start `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1`. `/docs` shows the LeadHunter operations. Pass `Authorization: Bearer <token>` on all LeadHunter requests. This stage assumes a single internal workspace. Configure `LEAD_HUNTER_DB` and `LEAD_HUNTER_OUTPUT` as a pair when using an alternate workspace. Both the CLI and API read these settings. For one-off CLI overrides use `universal --db PATH --output-root PATH ...`.

| API route under `/api/v1/lead-hunter` | Behavior |
|---|---|
| `POST /runs` | Submit the same JSON as the CLI, return `202` and a queued run ID, then process it in the API background. |
| `GET /runs/{id}` | Read run status, stage progress, and any structured errors. |
| `GET /runs/{id}/result` | Retrieve completed or partial structured output. Returns `409` while unavailable. |
| `GET /runs/{id}/artifacts/{name}` | Download a generated legacy-profile workbook. |
| `GET /sources` | List source capabilities and access requirements. |

The cake-tray fixture produces a synthetic opportunity with cited packaging, procurement, and approval roles and a manager review plan. The decompression-belt fixture separates wearer and payer, records a synthetic inquiry and channel-specific opt-in, and proposes a reviewed reply on the inbound channel. In fixture mode all supplied evidence is synthetic. In research mode operator-supplied evidence needs separate review. Consumer records should use pseudonyms and purpose-limited evidence without health records or direct contact identifiers.

## Adapters and boundaries

| Capability | Available use |
|---|---|
| `evidence_intake` | Both buyer paths. Applies structured evidence and role citation validation to supplied records. |
| `shymkent_crm` | Shymkent organizational research with `research_context: crm_sales_operations`. Reuses GDELT DOC, municipal and Otyrar RSS, public-page verification, LeadHunter routing, and its Excel exporter. |
| `kazakhstan_sales` | Kazakhstan organizational research with the same explicitly selected context. Reuses GDELT DOC, six regional government feeds, first-party site enrichment, Sales Team qualification, and its Excel exporter. |

Both live profiles use predefined CRM and growth signals. They do not run arbitrary product queries. A general product run can plan searches and use supplied evidence. The registry exposes missing general web search, social/ad libraries, third-party job boards, and business/procurement registries. An automatic inbound provider requires approved access and an implementation. Configured employer pages, narrow site indexes, and extra RSS registries are available on the legacy commands. The universal API accepts source IDs and evidence, without a caller-controlled public fetch URL or configuration-file path. For a covered organization profile, `crm_read_only: true` enables existing allowlisted Bitrix24 reads. Generic intake lacks verified identifiers for automatic CRM matching.

To add a source, implement `SourceAdapter` in `app/services/universal_sources.py`. Declare its `SourceCapability`, return `Discovery[T]` from discovery, and convert verified observations to `Verification` containing shared `Candidate` records. Keep access and source verification inside the adapter. Register a factory in `ADAPTERS` and test supported buyer paths, source failures, and evidence provenance. Reserve fixture mode for offline adapters. The orchestrator selects the adapter by capability, buyer path, market, and configured research context.

The core workflow generates an engagement plan, local history from supplied interaction evidence, and a manager next action. Email, SMS, SIP, and CRM writes remain disabled. Connected-conversation review has its own test-only reply gate. See [Open Channels](bitrix24_open_channels.md) for authorization, polling and inventory limits. Public social discovery remains a separate capability.
