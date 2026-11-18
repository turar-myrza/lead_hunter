# CRM Consulting AI Lead Hunter

Version `1.0.0`.

This repository contains the CRM Consulting Lead Hunter implementation for
public B2B research, evidence-based qualification, READ ONLY Bitrix24 matching,
commercial routing, and manager-ready reports.

## Included capabilities

- Public-source discovery and evidence records.
- NEW CRM / EXISTING BITRIX24 / CRM CONSULTING CUSTOMERS / HOLD routing.
- Separate need, offer-fit, contact, and purchase-interest signals.
- Freshness checks and duplicate matching by domain, company, and normalized phone.
- READ ONLY Bitrix24 client and relationship-history review.
- CSV, Markdown, and Excel reporting.
- Account Opportunity Brief and outreach-draft support.
- Sales-team workflow with resumable local checkpoints.
- ECC-informed buyer-intelligence skill and versioned commercial rules.

No credentials, customer exports, generated reports, CRM writes, message sending,
or automatic calls belong in this repository.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python -m app.lead_hunter run --city Шымкент --limit 3 --public-only
```

The normal Lead Hunter workflow remains review-first. It does not create CRM
records or send outreach. External LLM use is optional and is not activated by
placing a key in `.env` alone.

## Project layout

```text
app/                 application and orchestration code
app/models/          typed research and CRM models
app/services/        discovery, scoring, routing, reporting, and outreach
app/integrations/    public-source and READ ONLY CRM clients
app/sales_knowledge/ role-specific research and communication skills
data/                versioned commercial rules
.agents/skills/      CRM Consulting buyer-intelligence skill
docs/                runbooks, safety rules, and version-control notes
tests/               automated regression tests
```

## Versioning

The baseline is `1.0.0`. Every approved feature or backward-compatible change
increments the patch component by `0.0.1`:

```text
1.0.0 → 1.0.1 → 1.0.2
```

The version is stored in `version.py`. Release tags use the same value with a
leading `v`, for example `v1.0.0` and `v1.0.1`.

See `docs/versioning.md` before creating a release.
