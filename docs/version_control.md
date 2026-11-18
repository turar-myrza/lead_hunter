# Lead Hunter source version control

The source baseline dated 2026-11-18 includes the current CRM Analyst application,
Lead Hunter v1.0/v1.1/v1.2 support, Account Research & Outreach, the sales team
modules, tests, runbooks, agent skills, and versioned commercial rules.

The local project had no Git history before this baseline. Version labels in the
application describe supported formats and workflows; this upload does not
reconstruct historical v1.0 or v1.1 source snapshots.

The shared repository is `git@github.com:turar-myrza/bitrix_pbx.git`, on branch
`codex/ainur-multiprompt-interface`. This project lives in its `crm_analyst/`
directory. The PBX/AInur project remains at the repository root.

## Work from the shared repository

```bash
git clone --branch codex/ainur-multiprompt-interface git@github.com:turar-myrza/bitrix_pbx.git
cd bitrix_pbx/crm_analyst
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Run Lead Hunter and CRM Analyst commands from `crm_analyst/`. Each application
has its own tests and Python import paths; run the PBX tests separately from the
repository root.

Before editing, pull the shared branch. After validating a change, stage the
specific source files, review the staged diff, commit, and push:

```bash
git pull --ff-only origin codex/ainur-multiprompt-interface
git status --short
git add app/path_to_changed_file.py tests/path_to_changed_test.py
git diff --cached
git commit -m "fix(lead-hunter): describe the change"
git push origin HEAD:codex/ainur-multiprompt-interface
```

Replace the example paths and commit message with the actual change. A commit
captures the staged files only; editing a file does not upload it automatically.

## Local source history

The original standalone project can retain its own source commits. Import those
commits into the shared checkout with Git subtree so the source history stays
available alongside the existing PBX history:

```bash
# From the shared repository root, after the initial import:
git subtree pull --prefix=crm_analyst /absolute/path/to/crm_analyst lead-hunter
```

Run both projects' test suites before pushing the shared branch. Do not force
push or rewrite shared commits.

## Files that stay local

`.gitignore` excludes credentials, environments, caches, generated reports,
contact exports, runtime databases/logs, and case observations. Safe placeholder
configuration in `.env.example` and commercial rule definitions are included.
The unrelated `terrace-video-test/` experiment is excluded.
Case-specific scripts inside `outputs/` also stay local because some embed
prospect contacts, CRM history, or fixed research decisions. They are separate
from the application implementation; sanitizing those archived scripts requires
moving their case data out of the source first.
