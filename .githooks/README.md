# Local Git hooks

`post-commit` pushes the just-created commit to `origin` automatically. It is
enabled for this checkout with `git config core.hooksPath .githooks`.

To make one local-only commit, use `GIT_AUTO_PUSH=0 git commit ...`.

The hook does not create commits. Codex still commits only completed, reviewed
changes and never stages unrelated user files.
