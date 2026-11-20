# CRM Consulting AI Platform — CRM Analyst

Source code, tests, and runbooks are versioned together. See the
[Git workflow and repository layout](docs/version_control.md) for the shared
GitHub branch, the `crm_analyst/` directory, and instructions for future commits.

CRM Analyst uses Python and FastAPI to read Bitrix24 and answer the management question: **Which open deals require my attention?** It now presents the answer as a short Executive Report first and keeps the full calculation available only as an explicit detail view.

It does **not** create, update, move, or delete anything in Bitrix24.

## Available read-only endpoints

Open `GET /api/v1/deals/latest` to receive:

- deal ID;
- deal title;
- readable stage name;
- responsible person's name;
- last modification date and time.

Open `GET /api/v1/analytics/deals/attention` to receive the default Executive Report:

- a 2–4 sentence management conclusion;
- the five most important deals with priority and a concrete recommendation;
- up to five actions for today;
- repeated systemic signals, short statistics, and data limitations;
- an invitation to request deeper analysis.

Open `GET /api/v1/analytics/deals/attention/details` only when the full calculation is needed. It preserves the existing list of every risky deal, Risk Score, detected reasons, weights, recommendations, warnings, and full statistics.

Executive priorities are derived only from the already calculated Risk Score:

| Priority | Risk Score | Meaning |
| --- | ---: | --- |
| 🚨 КРИТИЧЕСКИЙ | 100–165 | Direct management intervention is required today |
| 🔴 ВЫСОКИЙ | 50–99 | The deal should be processed today |
| 🟡 СРЕДНИЙ | 30–49 | Process in the near term |
| 🟢 НИЗКИЙ | 1–29 | Monitor without urgent intervention |

Every highlighted item distinguishes `ФАКТ`, `РИСК`, and `ПРЕДПОЛОЖЕНИЕ`. Missing CRM records are never presented as proof that a manager did no work. The current deal-attention presenter does not generate assumptions.

The Risk Score uses these rules:

| Risk | Condition | Weight |
| --- | --- | ---: |
| No recent activity | More than 3 days | 10 |
| No next action | No incomplete future activity or task | 20 |
| Overdue task | Linked unfinished task is past its deadline | 30 |
| Long time in stage | More than 7 days | 15 |
| No activity after proposal | More than 5 days since the proposal or the latest following activity | 15 |
| Overdue planned close date | Planned close date is before today | 25 |
| No responsible person | Missing, unknown, or inactive user | 50 |

The checks are independent and their weights are added together. Deals with a zero score are counted in statistics but omitted from the attention list.

For the first working version, a proposal is identified conservatively by the
deal's current stage name containing `КП` or `Коммерческое предложение`. The
stage-entry time is used as the proposal timestamp. If a funnel uses a different
name, the response contains a warning and skips this one rule for that funnel;
the other six rules still run normally.

The application uses only these read methods:

- `crm.item.list` — current universal CRM list method, with `entityTypeId = 2` for deals;
- `crm.activity.list` — incomplete next actions and targeted proposal follow-up checks;
- `crm.stagehistory.list` — proposal-stage history;
- `crm.status.list` — stage names;
- `user.get` — brief names of responsible users.

Deal-linked unfinished tasks are read through `crm.activity.list`, following the
official Bitrix24 CRM tutorial. Activity requests are sent in controlled batches:
each subrequest contains exactly one deal, and every subrequest is fixed to the
read-only `crm.activity.list` method. This also identifies activities where the
deal is a secondary binding.

The client has an exact allowlist that blocks any other Bitrix24 method before a
network request is sent. The generic `batch` method is not exposed; internally it
accepts only validated `crm.activity.list` subrequests.

## Before you start

You need:

1. macOS with an internet connection;
2. Python 3.10 or newer;
3. the regenerated inbound-webhook base URL;
4. webhook permissions `CRM (crm)` and `Пользователи (минимальный) (user_brief)`;
5. a webhook owner whose CRM role allows reading deals but denies adding, editing, and deleting them.

`Задачи (task)` is not required for this scenario because only tasks linked to
CRM deals are checked through the read-only CRM activity API.

If `python3 --version` shows an `xcode-select` error, install a current Python version from [python.org](https://www.python.org/downloads/macos/) and reopen Terminal.

## Start on this computer

The project already includes a local `.venv` environment with all required packages installed.

### 1. Open Terminal

Press `Command + Space`, type `Terminal`, and press Enter.

### 2. Go to the project folder

```bash
cd "/Users/dauren/Documents/CRM Consulting AI Platform/crm_analyst"
```

### 3. Activate the prepared Python environment

```bash
source .venv/bin/activate
```

After activation, the beginning of the Terminal line normally contains `(.venv)`.

### 4. Check the installed packages

```bash
python -m pip check
```

The expected result is `No broken requirements found.`

If `.venv` is deleted or you move the project to another computer, install Python first and recreate it with:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

## Add the webhook securely

The local `.env` file already exists and is excluded by `.gitignore`.

Open it in TextEdit:

```bash
open -a TextEdit .env
```

Paste the **regenerated base URL** after the equals sign:

```dotenv
BITRIX24_WEBHOOK_URL=https://your-portal.bitrix24.example/rest/USER_ID/SECRET_TOKEN/
BITRIX24_TIMEOUT_SECONDS=10
```

Save the file. Do not add a REST method such as `profile.json` to this value. Do not paste the real URL into README, source code, screenshots, messages, or `.env.example`.

## Start the application

Make sure `(.venv)` is visible in Terminal, then run:

```bash
uvicorn app.main:app --reload
```

Keep that Terminal window open. In a browser, open:

- [http://127.0.0.1:8000/api/v1/deals/latest](http://127.0.0.1:8000/api/v1/deals/latest) — latest 10 deals;
- [http://127.0.0.1:8000/api/v1/analytics/deals/attention](http://127.0.0.1:8000/api/v1/analytics/deals/attention) — concise Executive Report;
- [http://127.0.0.1:8000/api/v1/analytics/deals/attention/details](http://127.0.0.1:8000/api/v1/analytics/deals/attention/details) — full analytical details on explicit request;
- [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) — interactive FastAPI documentation.

Stop the application with `Control + C`.

## Example response

The names below are examples, not real CRM data:

```json
{
  "count": 1,
  "message": "Latest deals, ordered by modification date (newest first).",
  "deals": [
    {
      "id": 102,
      "title": "Example renewal",
      "stage": "New",
      "responsible": "Example User",
      "updated_at": "2026-08-05T11:30:00+05:00"
    }
  ]
}
```

A valid Bitrix24 response with no visible deals returns `count: 0`, a clear message, and an empty `deals` list.

The Executive Report is returned as structured JSON so any interface can render the same management-first sections. Example values below are fictional:

```json
{
  "main_conclusion": [
    "По данным CRM внимания требуют 8 из 42 открытых сделок.",
    "Высокий приоритет: 3. Эти сделки необходимо отработать сегодня."
  ],
  "attention_items": [
    {
      "deal_id": 102,
      "client_or_deal": "Example renewal",
      "responsible": "Example User",
      "stage": "КП отправлено",
      "risk_score": 65,
      "priority": "high",
      "priority_label": "🔴 ВЫСОКИЙ",
      "why_attention": "По данным CRM: Есть просроченная задача; Нет следующего действия",
      "recommended_action": "Связаться с клиентом и определить следующее действие.",
      "evidence_level": "risk",
      "evidence_label": "РИСК"
    }
  ],
  "actions_today": [
    "По сделке «Example renewal» (№102) — связаться с клиентом и определить следующее действие."
  ]
}
```

The actual response also includes `systemic_issues`, concise `statistics`, `data_limitations`, `generated_at`, and `details_available`.

The attention endpoint can take roughly 1–3 minutes on a portal with about 5,000
open deals. It deliberately follows every Bitrix24 page and pauses between large
requests to respect API limits. It never changes a deal, activity, task, user, or
stage.

## Run the safety tests

With `(.venv)` active:

```bash
pytest
```

The tests use fake responses. They never call your real Bitrix24 portal. They verify:

- the current deal-list request and sorting;
- stage and responsible-person enrichment;
- valid empty results;
- every Risk Score criterion and its exact time boundary;
- cumulative scoring and descending sorting;
- Executive Report priority boundaries, five-item limit, concrete daily actions, and progressive disclosure;
- evidence labels and protection against turning missing CRM records into accusations;
- pagination and safe ID chunking for large portals;
- safe per-deal batch mapping, pagination, and duplicate handling;
- network failures;
- authorization failures;
- empty HTTP responses;
- blocking of a write method before any request is sent.

## Common errors

- `BITRIX24_WEBHOOK_URL is missing` — open `.env` and paste the regenerated base URL.
- `NO_AUTH_FOUND` or `INVALID_CREDENTIALS` — the webhook URL is invalid, expired, or was regenerated again.
- `INSUFFICIENT_SCOPE` — check `Настройка прав` and keep `CRM (crm)` and `Пользователи (минимальный) (user_brief)`.
- A warning about missing proposal stages — rename the relevant funnel stage so that it contains `КП` or `Коммерческое предложение`; until then, the proposal follow-up rule is skipped only for that funnel.
- Network timeout — check the internet connection and try again later.
- Empty deal list — the portal has no deals visible to the webhook owner, or the owner's CRM role is too restrictive.

## Official Bitrix24 references

- [`crm.item.list`](https://apidocs.bitrix24.com/api-reference/crm/universal/crm-item-list.html)
- [`crm.status.list`](https://apidocs.bitrix24.com/api-reference/crm/status/crm-status-list.html)
- [`crm.activity.list`](https://apidocs.bitrix24.com/api-reference/crm/timeline/activities/activity-base/crm-activity-list.html)
- [`crm.stagehistory.list`](https://apidocs.bitrix24.com/api-reference/crm/crm-stage-history-list.html)
- [`user.get`](https://apidocs.bitrix24.com/api-reference/user/user-get.html)
- [User scope versions](https://apidocs.bitrix24.com/api-reference/user/user-scope.html)
- [Official task-list-from-deals scenario](https://apidocs.bitrix24.com/tutorials/crm/how-to-get-lists/get-activity-list-by-deals.html)
- [Read-only batch transport](https://apidocs.bitrix24.com/settings/how-to-call-rest-api/batch.html)
- [Large-data pagination](https://apidocs.bitrix24.com/settings/performance/huge-data.html)

## AI Lead Hunter v1.1 (Codex session)

Lead Hunter extends this same platform without changing CRM Analyst's routes,
services or Bitrix24 client. Codex public web tools perform research; local
Python validates evidence and separately evaluates potential need (0–100),
contact availability (0–100), and public purchase interest. It is not an
unattended search service. The first five companies must be reviewed before
expanding to 30. See [agent runbook](docs/lead_hunter.md).

Only the explicit `check-crm` command loads the existing webhook and uses a
**separate READ ONLY client** to scan accessible companies, contacts and leads,
then enrich matches with responsible users, statuses and limited activity/deal
history. No CRM methods that create/update/delete, messages, calls or generic
batch are allowed. Similar names alone never establish a confirmed duplicate.
Failed or partial checks never become “no duplicates”.

```bash
.venv/bin/python -m app.lead_hunter plan
.venv/bin/python -m app.lead_hunter schema
.venv/bin/python -m app.lead_hunter check-crm outputs/lead_hunter/v1.1/2026-11-04/research.json --output outputs/lead_hunter/v1.1/2026-11-04/research_checked.json
.venv/bin/python -m app.lead_hunter export outputs/lead_hunter/v1.1/2026-11-04/research_checked.json --output outputs/lead_hunter/v1.1/2026-11-04/result
```

`review.csv` contains the entire reviewed pilot. `companies.csv` contains only
contactable candidates with a complete available CRM check no older than 24
hours and no confirmed/possible matches. Both are local UTF-8 BOM CSVs with
manual field mapping; no import is performed. Results containing internal CRM
ownership/history are written with mode 0600 and the v1.1 output directory is
excluded from Git. Do not publish these reports automatically.

Potential need uses commercial signal weights, not just a count: explicit
processing problem 30, concrete automation project 25, growth 15, steady inbound
10, team 5, channels 5, order complexity 5, existing CRM 5. Age, vacancy status,
verification freshness and local/network scope affect points. A: ≥65 plus a
problem/project; B: ≥35; C: <35. Contactability does not inflate need, and an
existing CRM or a vacancy does not imply intent to buy. Every assigned point has
a source and rationale. Original v1.0 batches and exports remain supported.

Run `.venv/bin/python -m pytest -q` with the project requirements. Check that
`import pydantic_core` succeeds as well as `pip check`: on macOS a synced `.venv`
may retain package metadata after iCloud has removed a compiled extension locally.

## Lead Hunter v1.2 and Account Research & Outreach v1.0

The v1.2 flow classifies accounts as NEW CRM, EXISTING BITRIX24, CRM CONSULTING
CUSTOMERS or EXCLUDED / HOLD before scoring a commercial offer. Relationship
history and contact restrictions take precedence over scores. Identity checking
and the explicit `review-history` command use the isolated READ ONLY client;
all classification, draft exports and outcome accounting are local operations.

The approved Outreach pilot is limited to three individually researched briefs,
letters, call scripts and useful materials. No sender, dialer or CRM writer is
implemented. A prepared draft is never counted as a sent message or a sale.
See [v1.2 / Outreach workflow and safety rules](docs/account_outreach.md) for
categories, score profiles, history limitations and commands. The v1.0/v1.1
formats remain supported; the rules in the linked document govern v1.2.

## Standalone Lead Hunter workflow (stages 1–2)

`.venv/bin/python -m app.lead_hunter run --city Шымкент --limit 3` runs public
discovery, evidence checks, READ ONLY CRM verification and local reporting, with
resume checkpoints. It stops before the LLM stage. Use `--public-only` for a
search smoke test without CRM. See the [runbook](docs/lead_hunter_run.md) for resume,
report statuses, and the standalone `.xlsx` export.

## Autonomous Sales Team

The sales worker, queue, dashboard and eight role skills are described in
[the sales team runbook](docs/sales_team.md). The portfolio workflow uses the
same READ ONLY CRM client. See [voice transport verification](docs/sales_voice.md)
for the test dialogue, Robovoice handoff and outbound enablement requirements.

## Universal LeadHunter

Submit a product brief through `python -m app.lead_hunter universal` or the internal
FastAPI endpoints. Both use the same persisted workflow for organization and
individual buyer paths. See the [universal LeadHunter runbook](docs/universal_lead_hunter.md)
for fixture commands, source coverage, API configuration, and extension points.

Product release: **1.2.5**. See [publishing policy](docs/versioning.md).

## Platform scope and implementation status

The product is a universal product-driven sales intelligence and execution platform.
CRM implementation is the first validation product, not the platform boundary.
See [target architecture](TARGET_ARCHITECTURE.md) for the full agreed scope and
[implementation status and audit](IMPLEMENTATION_STATUS.md) for verified capabilities,
gaps, decisions and the corrected first-stage plan.

Control center: `/platform`. See [deployment and operations](docs/control_center.md).
