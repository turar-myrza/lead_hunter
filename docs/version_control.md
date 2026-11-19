# Lead Hunter source version control

Repository: `git@github.com:turar-myrza/lead_hunter.git`.
Publishing branch: `codex/lead-hunter-initial`.
Local development branch: `feature/universal-lead-hunter`.

The local baseline and initial GitHub import had independent histories.
Release 1.2.2 preserves both histories and the current application implementation.
The separate `main` implementation is not overwritten or merged.

After a completed code change, run relevant tests, inspect the diff and secrets,
increment the patch in `version.py`, commit a short description including the
version, create the corresponding annotated tag, and push the commit and tag.
This is the user's standing authorization; later task-specific restrictions win.
See [versioning](versioning.md). Do not force-push shared history.

Keep credentials, .env, environments, generated reports, customer data, databases,
and case-specific import scripts local. Never stage `tools/import_cake_leads.py`
as part of application releases. Stage specific reviewed paths, not the whole tree.

Publishing does not deploy the VPS or authorize CRM writes, messages, or calls.
