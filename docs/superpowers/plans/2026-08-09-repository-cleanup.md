# Repository Cleanup and Phase 01 Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current split main/Phase-01 workspace into one clean development baseline, preserve the new service plans, and remove obsolete root-level plans, design screenshots and brainstorming scratch files.

**Architecture:** Perform this cleanup sequentially in the primary repository because every task mutates the same Git history and worktree state. First prove the Phase 01 branch is complete and fast-forward it into main; then remove only the explicitly approved legacy artifacts, commit the authoritative plans, verify the full suite, and finally retire the merged Phase 01 worktree. Do not push, rewrite history, reset, clean broadly, or touch the remote server.

**Tech Stack:** Git, Bash/Zsh, Python, pytest, Markdown

---

> **Recommended execution mode:** Use `superpowers:executing-plans` inline and run tasks strictly in order. Do not dispatch parallel implementers for this plan. Do not run `git clean`, `git reset`, recursive deletion against the repository root, or any SSH command.

## Current verified state

- Primary repository: `/Users/tim/Desktop/批改小程序/math-competition-grader`
- Primary branch: `main` at `fb85a08`, one commit ahead of `origin/main`
- Phase branch: `phase-01-foundation` at `77822d3`
- Phase worktree: `/Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation`
- `main` is an ancestor of `phase-01-foundation`, so a fast-forward merge is currently possible.
- Phase 01 worktree was clean when this plan was written.
- Product plans under `docs/superpowers/plans/` are untracked and must be preserved.
- `.superpowers/` is untracked brainstorming output and may be removed after it is ignored.

## Keep/delete contract

Keep:

- `README.md`
- `app/`
- `server/` and `tests/server/` from Phase 01
- `.agents/`
- `archive/`
- `data/` and all ignored `data/jobs/` user files
- `docs/superpowers/specs/2026-08-09-remote-server-environment-design.md`
- all implementation plans in `docs/superpowers/plans/`, including this cleanup plan
- `.venv/` and local job data unless the user separately asks to remove them

Delete as obsolete or generated:

- `PLAN.md`
- `design-qa.md`
- `design-qa-comparison-v1.png`
- `design-qa-comparison-final.png`
- `implementation-workspace-v1.png`
- `implementation-workspace-final.jpg`
- `.superpowers/`
- root `.DS_Store`, if present
- `docs/superpowers/.DS_Store`, if present

The root `PLAN.md` is the obsolete local-Web/server-Codex deployment proposal. Do not confuse it with the authoritative files under `docs/superpowers/plans/`.

### Task 1: Revalidate the two worktrees before any mutation

**Files:**
- Read: `/Users/tim/Desktop/批改小程序/math-competition-grader/.git`
- Read: `/Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation/.git`
- Test: `/Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation/tests/`

- [ ] **Step 1: Capture branch and worktree state**

Run from the primary repository:

```bash
git status --short --branch
git branch -avv
git worktree list --porcelain
git log --oneline --decorate -10
```

Expected:

- current branch is `main`;
- `phase-01-foundation` exists in the stated worktree;
- there are no tracked modifications in the primary worktree;
- only `.superpowers/` and `docs/superpowers/plans/` are untracked.

If any additional tracked or untracked path appears, stop and report it instead of deleting, moving or staging it.

- [ ] **Step 2: Prove Phase 01 descends from current main**

```bash
git merge-base --is-ancestor main phase-01-foundation
git log --oneline main..phase-01-foundation
git diff --stat main...phase-01-foundation
```

Expected: the ancestor check exits 0 and the branch contains the seven Phase 01 commits ending with `77822d3 feat: add grading service app factory`.

- [ ] **Step 3: Verify the Phase 01 worktree is clean**

```bash
git -C /Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation status --short --branch
```

Expected: only `## phase-01-foundation`, with no modified or untracked files.

- [ ] **Step 4: Run Phase 01 tests before merge**

```bash
cd /Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation
.venv/bin/python -m pytest -q
```

Expected: exit 0 with all legacy and Phase 01 tests passing. If tests fail, stop; do not merge or clean anything.

### Task 2: Fast-forward Phase 01 into main

**Files:**
- Add through merge: `pyproject.toml`
- Add through merge: `.env.example`
- Add through merge: `alembic.ini`
- Add through merge: `server/`
- Add through merge: `tests/server/`
- Modify through merge: `.gitignore`

- [ ] **Step 1: Return to the primary repository and reconfirm status**

```bash
cd /Users/tim/Desktop/批改小程序/math-competition-grader
git switch main
git status --short --branch
```

Expected: no tracked changes. Do not stage the untracked plans yet.

- [ ] **Step 2: Fast-forward only**

```bash
git merge --ff-only phase-01-foundation
```

Expected: main advances to `77822d3` without a merge commit or conflict. If Git cannot fast-forward, stop and inspect the new commits; do not use `--no-ff`, rebase, reset or force.

- [ ] **Step 3: Verify the Phase 01 files now exist on main**

```bash
test -f pyproject.toml
test -f alembic.ini
test -f server/main.py
test -f server/config.py
test -f server/migrations/versions/0001_initial_schema.py
test -f tests/server/test_health.py
git log -1 --oneline
```

Expected: every `test -f` succeeds and HEAD is the Phase 01 app-factory commit.

- [ ] **Step 4: Run the merged suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: exit 0. If it fails only because the primary `.venv` lacks new dependencies, run `.venv/bin/python -m pip install -e '.[dev]'`, rerun pytest, and stop if any test still fails.

### Task 3: Remove only the approved obsolete artifacts

**Files:**
- Modify: `.gitignore`
- Delete: `PLAN.md`
- Delete: `design-qa.md`
- Delete: `design-qa-comparison-v1.png`
- Delete: `design-qa-comparison-final.png`
- Delete: `implementation-workspace-v1.png`
- Delete: `implementation-workspace-final.jpg`
- Delete if present: `.DS_Store`
- Delete if present: `docs/superpowers/.DS_Store`
- Delete after quarantine: `.superpowers/`

- [ ] **Step 1: Verify every tracked deletion target**

```bash
git ls-files --error-unmatch PLAN.md
git ls-files --error-unmatch design-qa.md
git ls-files --error-unmatch design-qa-comparison-v1.png
git ls-files --error-unmatch design-qa-comparison-final.png
git ls-files --error-unmatch implementation-workspace-v1.png
git ls-files --error-unmatch implementation-workspace-final.jpg
```

Expected: all six paths are tracked. Open `PLAN.md` far enough to confirm it is the obsolete “数学竞赛题批改：服务器版部署计划”, not a new Superpowers plan.

- [ ] **Step 2: Add the brainstorming ignore rule**

Append exactly this block to `.gitignore` if it is not already present:

```gitignore

# Superpowers brainstorming scratch artifacts
.superpowers/
```

Do not add `docs/superpowers/` to `.gitignore`.

- [ ] **Step 3: Remove tracked obsolete files through Git**

```bash
git rm -- PLAN.md design-qa.md design-qa-comparison-v1.png design-qa-comparison-final.png implementation-workspace-v1.png implementation-workspace-final.jpg
```

These removals remain recoverable from Git history.

- [ ] **Step 4: Quarantine generated untracked files before final deletion**

```bash
cleanup_quarantine="/tmp/math-grader-cleanup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$cleanup_quarantine"
test ! -e .superpowers || mv .superpowers "$cleanup_quarantine/superpowers"
test ! -e .DS_Store || mv .DS_Store "$cleanup_quarantine/root.DS_Store"
test ! -e docs/superpowers/.DS_Store || mv docs/superpowers/.DS_Store "$cleanup_quarantine/docs-superpowers.DS_Store"
printf '%s\n' "$cleanup_quarantine"
```

Expected: the command prints one explicit quarantine directory. Do not remove the quarantine until Task 6 passes.

- [ ] **Step 5: Confirm protected paths still exist**

```bash
test -f README.md
test -d app
test -d server
test -d .agents
test -d archive
test -d data
test -f docs/superpowers/specs/2026-08-09-remote-server-environment-design.md
```

Expected: every command succeeds.

- [ ] **Step 6: Commit the approved deletions and ignore rule**

```bash
git add .gitignore
git diff --cached --check
git diff --cached --stat
git commit -m "chore: remove obsolete design artifacts"
```

Expected: the commit contains only `.gitignore` and the six explicit tracked deletions.

### Task 4: Commit the authoritative plans and add a plan index

**Files:**
- Create: `docs/superpowers/plans/README.md`
- Add: `docs/superpowers/plans/2026-08-08-program-roadmap.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-01-foundation.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-02-intake-order.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-03-worker-control-plane.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-04-grading-runtime.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-05-aftersales-lifecycle.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-06-miniapp.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-07-admin.md`
- Add: `docs/superpowers/plans/2026-08-08-phase-08-deployment-cutover.md`
- Add: `docs/superpowers/plans/2026-08-09-phase-09-remote-server-environment.md`
- Add: `docs/superpowers/plans/2026-08-09-repository-cleanup.md`
- Modify: `README.md`

- [ ] **Step 1: Create the plan index**

`docs/superpowers/plans/README.md` must contain:

```markdown
# Grading Service Implementation Plans

Start with [the program roadmap](2026-08-08-program-roadmap.md), then execute Phases 01–09 in dependency order. Phase 09 is the real-host infrastructure track and its deployment/cutover tasks remain gated as described in that plan.

## Product phases

1. [Phase 01 — Server foundation](2026-08-08-phase-01-foundation.md)
2. [Phase 02 — Intake and orders](2026-08-08-phase-02-intake-order.md)
3. [Phase 03 — Worker control plane](2026-08-08-phase-03-worker-control-plane.md)
4. [Phase 04 — Grading runtime](2026-08-08-phase-04-grading-runtime.md)
5. [Phase 05 — Aftersales and lifecycle](2026-08-08-phase-05-aftersales-lifecycle.md)
6. [Phase 06 — Native mini-program](2026-08-08-phase-06-miniapp.md)
7. [Phase 07 — Admin](2026-08-08-phase-07-admin.md)
8. [Phase 08 — Deployment and cutover](2026-08-08-phase-08-deployment-cutover.md)
9. [Phase 09 — Remote server environment](2026-08-09-phase-09-remote-server-environment.md)

## Repository operations

- [Repository cleanup and Phase 01 integration](2026-08-09-repository-cleanup.md)
```

- [ ] **Step 2: Clarify the root README without rewriting the legacy documentation**

Insert immediately after the title in `README.md`:

```markdown
> **Repository transition:** `app/` remains the verified local grading baseline. The new mini-program service is being built alongside it under `server/`, `worker/`, `miniapp/`, `admin/`, and `ops/`. See the [implementation roadmap](docs/superpowers/plans/2026-08-08-program-roadmap.md). Until a phase replaces a local component, the local instructions below remain valid.
```

Do not delete or rewrite the existing local startup, grading behavior or validation documentation.

- [ ] **Step 3: Stage only the explicit documentation set**

```bash
git add README.md \
  docs/superpowers/plans/README.md \
  docs/superpowers/plans/2026-08-08-program-roadmap.md \
  docs/superpowers/plans/2026-08-08-phase-01-foundation.md \
  docs/superpowers/plans/2026-08-08-phase-02-intake-order.md \
  docs/superpowers/plans/2026-08-08-phase-03-worker-control-plane.md \
  docs/superpowers/plans/2026-08-08-phase-04-grading-runtime.md \
  docs/superpowers/plans/2026-08-08-phase-05-aftersales-lifecycle.md \
  docs/superpowers/plans/2026-08-08-phase-06-miniapp.md \
  docs/superpowers/plans/2026-08-08-phase-07-admin.md \
  docs/superpowers/plans/2026-08-08-phase-08-deployment-cutover.md \
  docs/superpowers/plans/2026-08-09-phase-09-remote-server-environment.md \
  docs/superpowers/plans/2026-08-09-repository-cleanup.md
git diff --cached --check
git diff --cached --stat
```

Expected: no `.superpowers/`, `.DS_Store`, job PDF, environment secret or unrelated file is staged.

- [ ] **Step 4: Commit the documentation baseline**

```bash
git commit -m "docs: add service implementation roadmap"
```

### Task 5: Verify the organized repository

**Files:**
- Test: `tests/`
- Test: `tests/server/`
- Verify: `.gitignore`
- Verify: `README.md`
- Verify: `docs/superpowers/plans/README.md`

- [ ] **Step 1: Run all tests from main**

```bash
.venv/bin/python -m pytest -q
```

Expected: exit 0 with all legacy and Phase 01 tests passing.

- [ ] **Step 2: Verify migrations and health imports**

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -c 'from server.main import create_app; print(create_app)'
```

Expected: migration reaches head and the command prints the `create_app` function without an exception. Use only the configured test/development database; never point this cleanup verification at production.

- [ ] **Step 3: Verify the intended root layout**

```bash
find . -maxdepth 1 -mindepth 1 -not -name .git -print | sort
```

Expected to retain at least:

```text
.agents
.env.example
.gitignore
.venv
README.md
alembic.ini
app
archive
data
docs
pyproject.toml
requirements.txt
server
tests
启动批改.command
```

The six deleted legacy artifacts, `PLAN.md` and `.superpowers/` must not appear.

- [ ] **Step 4: Check for accidental files and secrets**

```bash
git status --short
git ls-files | rg '(^|/)(\.DS_Store|\.env|auth\.json)$' || true
git ls-files | rg '^data/jobs/' || true
git check-ignore -v .superpowers/example.tmp
```

Expected:

- `git status --short` is empty;
- no secret/local-job path is tracked;
- `.superpowers/example.tmp` is ignored by the new rule.

### Task 6: Retire the merged Phase 01 worktree safely

**Files:**
- Remove after ancestry proof: `/Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation`
- Delete after merge: local branch `phase-01-foundation`
- Delete after all gates pass: the exact quarantine directory printed in Task 3

- [ ] **Step 1: Prove Phase 01 is fully contained in main**

```bash
git merge-base --is-ancestor phase-01-foundation main
git diff --exit-code main phase-01-foundation
```

Expected: both commands exit 0. If either fails, do not remove the worktree or branch.

- [ ] **Step 2: Remove the merged worktree and branch using Git**

```bash
git worktree remove /Users/tim/.config/superpowers/worktrees/math-competition-grader/phase-01-foundation
git branch -d phase-01-foundation
git worktree list
```

Expected: the Phase 01 worktree and local branch disappear, while main remains checked out in the primary repository.

- [ ] **Step 3: Delete only the recorded quarantine**

Resolve the exact directory printed in Task 3, confirm its basename starts with `math-grader-cleanup-`, list its contents, and then remove that exact directory. Do not use an empty variable, glob, `/tmp`, the repository root or a home directory as the target.

After deletion, state that `.superpowers` and `.DS_Store` scratch files are not recoverable except from any separate machine backup; the tracked PNG/JPG/Markdown deletions remain recoverable from Git history.

- [ ] **Step 4: Final status and handoff**

```bash
cd /Users/tim/Desktop/批改小程序/math-competition-grader
git status --short --branch
git log --oneline --decorate -12
git worktree list
```

Expected: clean `main`, no Phase 01 worktree, and no push performed. Report the new commit IDs and ask the user separately before pushing to `origin/main`.
