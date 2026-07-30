## Change

<!-- What changed and why? -->

## Verification

<!-- List exact commands and results. -->

- [ ] Relevant tests pass.
- [ ] `git diff --check` passes.
- [ ] No secrets, environment files, runtime state, logs, caches, or backups are included.
- [ ] This branch was created from current `origin/master` in an isolated worktree.

## Release

- [ ] This PR is ready to merge (leave unchecked for draft/WIP work).
- [ ] After merge, deploy the exact `origin/master` merge SHA with `scripts/deploy_from_git.sh`.
- [ ] Verify the VPS `/health` endpoint returns HTTP 200.
