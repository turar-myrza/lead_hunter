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
