# Lead Hunter source control

This repository is the source of truth for the CRM Consulting Lead Hunter
product. The baseline is release `1.0.0` on branch
`codex/lead-hunter-initial`.

Before editing:

```bash
git pull --ff-only origin codex/lead-hunter-initial
git status --short
```

Stage only reviewed source and test files. Never commit `.env`, CRM exports,
private phone lists, logs, recordings, generated reports, or production SIP/AMI
configuration. Use the safe examples and local environment variables instead.

For the next feature release, update `version.py` from `1.0.0` to `1.0.1`, run
the tests, commit, and create the matching tag `v1.0.1`. See
`docs/versioning.md` for the release procedure.

The VPS deployment is a separate runtime and must be deployed from an explicit
tag after review. A direct edit on the VPS is not considered a source commit.
