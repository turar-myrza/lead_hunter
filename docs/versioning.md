# Lead Hunter versioning

The repository uses semantic versioning for the Lead Hunter product.

## Baseline

The imported implementation is release `1.0.0`.

## Feature increments

Each next approved feature release increments the patch version by `0.0.1`:

```text
1.0.0 -> 1.0.1 -> 1.0.2
```

Update `version.py`, run the relevant test suites, review the staged diff, then
create a matching annotated Git tag:

```bash
python -m pytest -q
git add version.py
git commit -m "feat: describe the change"
git tag -a v1.0.1 -m "Lead Hunter v1.0.1"
git push origin HEAD
git push origin v1.0.1
```

Do not tag a release containing credentials, CRM exports, private phone lists,
logs, audio, generated reports, or unreviewed production files.

## Current publishing policy

Current release: `1.2.4` (verified control-center deployment and provider compatibility).
After each completed, validated code change, increment the patch version, commit
with a short description and version, create a matching annotated tag, and push
to `codex/lead-hunter-initial` in `turar-myrza/lead_hunter`. This is the user-approved
default; an explicit task instruction not to publish overrides it. Do not push
partial edits or force-push. Do not publish secrets, runtime data, or unrelated
user files, including `tools/import_cake_leads.py`. No background uploader is installed.

The v1.2.2 snapshot reconciles local baseline history with the GitHub v1.0.0
branch. Tags v1.2.0 and v1.2.1 already exist on the separate main implementation;
that branch is not merged or changed by this release.
