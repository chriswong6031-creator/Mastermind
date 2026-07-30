# GitHub and VPS delivery workflow

`origin/master` is the only release source. The VPS remains the canonical writer
for live paper-portfolio state; GitHub owns code, configuration, tests, and
reviewable documentation.

## Start every session in isolation

From any clean administrative checkout:

```bash
git fetch origin --prune
git worktree add ../Mastermind-<task>-<session> \
  -b codex/<task>-<session> origin/master
cd ../Mastermind-<task>-<session>
```

Use a unique task/session suffix. Never point two sessions at the same worktree
or branch. Before editing, confirm:

```bash
git status --short --branch
git merge-base --is-ancestor origin/master HEAD
```

If a session opens in the legacy shared checkout and it is dirty, do not clean,
stash, reset, or overwrite it. Treat those changes as user-owned. Create a fresh
worktree and move only the task's deliberate edits there.

## Complete a change

1. Run the smallest relevant test set while iterating, then the repository CI
   gate before handoff.
2. Review `git diff --check`, `git status`, and the staged diff. Never stage
   `.env*`, credentials, logs, caches, runtime `data/`, or backup archives.
3. Commit a scoped change, push the branch, and open a PR:

   ```bash
   git push -u origin HEAD
   gh pr create --fill
   ```

4. Wait for required checks. If checks fail or the work is incomplete, mark the
   PR draft and stop; do not merge or deploy.
5. Merge through GitHub:

   ```bash
   gh pr checks --watch
   gh pr merge --squash --delete-branch
   ```

6. Resolve the merge commit from GitHub and deploy that exact commit:

   ```bash
   git fetch origin master
   merge_sha="$(git rev-parse origin/master)"
   ./scripts/deploy_from_git.sh "$merge_sha"
   ```

The deploy wrapper archives `origin/master` into a temporary clean release
directory. Local uncommitted files cannot enter the release. The VPS deploy
creates a rolling snapshot, restarts `mastermind.service`, checks
`http://127.0.0.1:8001/health`, and rolls back on failure.

## Recovery and concurrency rules

- Never resolve concurrent work by copying one shared directory over another.
  Rebase or merge the latest `origin/master` into the task branch and resolve
  conflicts in the branch worktree.
- Never bypass a failed check merely to get a deployment out.
- Never deploy a draft PR, a branch head, or a commit that is not the current
  `origin/master`.
- If a deployment fails, keep the PR/merge history intact, inspect the deploy
  log, and fix forward in a new PR. The deploy script attempts an automatic
  rollback before returning failure.
- GitHub credentials belong in macOS Keychain through `gh auth login`. VPS SSH
  credentials remain machine-local with mode `0600`; neither belongs in git or
  agent memory.

## Bootstrap note

The repository was initialized from the committed local history on 2026-07-30.
Pre-existing uncommitted application work was preserved separately in a draft PR
and must pass its failing acceptance tests before it is eligible to merge.
