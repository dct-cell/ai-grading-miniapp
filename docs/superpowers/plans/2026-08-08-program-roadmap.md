# Grading Service Program Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the local single-user grader with a native WeChat mini-program, a mainland FastAPI/MySQL service, native macOS/Linux/Windows Workers, and a separated Admin application without discarding the verified grading engine.

**Architecture:** Keep the current app/ grading implementation and its 57 tests as the migration baseline. Add a modular server in server/, an outbound-only daemon in worker/, a native client in miniapp/, and a React Admin in admin/; move one vertical business slice at a time until the old local UI is no longer needed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, MySQL 8, pytest, native WeChat Mini Program, React/Vite/TypeScript, Ubuntu 24.04 LTS, Nginx, systemd, Tencent COS, Codex CLI, XeLaTeX

---

## Locked decisions

- Temporary pre-payment PDF uploads exist only to validate, count pages, and quote. The authoritative payment callback promotes the immutable files into an order.
- Price is versioned and defaults to CNY 10 per page.
- Each healthy Worker holds at most one order. One order may run at most three Codex sessions internally.
- Workers connect outbound over HTTPS, share one high-entropy bearer key, and retain distinct worker IDs.
- V1 delivery opens a three-day window for accept, one review, or full refund. V2 opens a three-day window for accept or full refund.
- User refunds affect monthly refund count and cumulative refund amount ratio. Admin technical refunds do not.
- Successful refund immediately revokes user download access.
- Local disk is primary file storage; private Tencent COS is encrypted backup.
- MySQL is the first queue. Redis, RabbitMQ and Docker are outside MVP.

## Target repository map

    app/                         existing verified grading engine
    server/
      api/                       miniapp, worker, admin, callback routers
      adapters/                  fake/WeChat auth and payment, local/COS storage
      domain/                    state and policy code
      models/                    SQLAlchemy models
      services/                  transactional use cases
      scheduler/                 expiry, cleanup, lease and refund reconciliation
      migrations/                Alembic migrations
    worker/
      runtime/                   platform-neutral daemon and task runner
      platforms/                 macOS, Linux and Windows adapters
      packaging/                 versioned ZIP assembly
    miniapp/                     native WeChat Mini Program
    admin/                       React/Vite Admin SPA
    ops/                         Nginx, systemd, backup and restore
      remote/                    verified grader-prod bootstrap and checks
    tests/server/
    tests/worker/
    tests/integration/

## Phase order and gates

| Phase | Plan | Gate |
|---|---|---|
| 1 | 2026-08-08-phase-01-foundation.md | Server starts, MySQL migration and state tests pass |
| 2 | 2026-08-08-phase-02-intake-order.md | Login, upload, quote, fake payment and V1 queue pass end to end |
| 3 | 2026-08-08-phase-03-worker-control-plane.md | Three Workers lease three different jobs; stale writes fail |
| 4 | 2026-08-08-phase-04-grading-runtime.md | Existing Codex/XeLaTeX output crosses the stable Worker protocol |
| 5 | 2026-08-08-phase-05-aftersales-lifecycle.md | V1/V2, refund, auto-accept and cleanup policies pass |
| 6 | 2026-08-08-phase-06-miniapp.md | Test-account mini-program completes the flow on a real phone |
| 7 | 2026-08-08-phase-07-admin.md | Admin controls orders, Workers, refunds, settings and audits |
| 8 | 2026-08-08-phase-08-deployment-cutover.md | Restore drill and three-platform package checks pass |
| 9 | 2026-08-09-phase-09-remote-server-environment.md | grader-prod passes the pre-domain host gate; staging is tunnel-only and production is dormant |

Phase 09 is the real-host infrastructure track. Its read-only inventory and Tasks 1–4 may begin before application feature work; its staging deployment waits for the Phase 01 health API and Phase 08 release artifacts, and its domain cutover remains deferred until the external credentials and domain prerequisites exist.

### Task 1: Preserve the baseline

**Files:**
- Modify: .gitignore
- Test: tests/test_api.py
- Test: tests/test_codex_runner.py
- Test: tests/test_frontend.py
- Test: tests/test_internal_analysis.py
- Test: tests/test_pdf_builder.py
- Test: tests/test_pdf_utils.py

- [ ] **Step 1: Run the baseline suite**

    .venv/bin/python -m pytest -q

Expected: 57 passed.

- [ ] **Step 2: Add the visual artifact ignore**

    # Superpowers visual brainstorming sessions
    .superpowers/

- [ ] **Step 3: Commit**

    git add .gitignore docs/superpowers/plans
    git commit -m "docs: add grading service implementation roadmap"

### Task 2: Execute phase plans in order

**Files:**
- Read: docs/superpowers/plans/2026-08-08-phase-01-foundation.md
- Read: docs/superpowers/plans/2026-08-08-phase-02-intake-order.md
- Read: docs/superpowers/plans/2026-08-08-phase-03-worker-control-plane.md
- Read: docs/superpowers/plans/2026-08-08-phase-04-grading-runtime.md
- Read: docs/superpowers/plans/2026-08-08-phase-05-aftersales-lifecycle.md
- Read: docs/superpowers/plans/2026-08-08-phase-06-miniapp.md
- Read: docs/superpowers/plans/2026-08-08-phase-07-admin.md
- Read: docs/superpowers/plans/2026-08-08-phase-08-deployment-cutover.md
- Read: docs/superpowers/plans/2026-08-09-phase-09-remote-server-environment.md

- [ ] **Step 1: Complete one phase gate before opening the next plan**

At every gate run:

    .venv/bin/python -m pytest -q
    git status --short

Expected: all legacy and new tests pass; only current-phase files are modified.

- [ ] **Step 2: Preserve the three stable seams**

    class AuthProvider(Protocol):
        def exchange_code(self, code: str) -> ExternalIdentity: ...

    class PaymentGateway(Protocol):
        def create_prepay(self, request: PrepayRequest) -> PrepayResult: ...
        def refund(self, request: RefundRequest) -> RefundResult: ...

    class GradingRuntime(Protocol):
        async def run(self, workspace: Path, bundle: TaskBundle) -> RuntimeResult: ...

Expected: fake and production adapters swap through settings without changing order services or Worker payloads.

### Task 3: Run final cross-phase acceptance

**Files:**
- Create: tests/integration/test_full_order_lifecycle.py
- Create: tests/integration/test_multi_worker_concurrency.py
- Create: tests/integration/test_backup_restore.py
- Create: ops/scripts/smoke-staging.sh
- Create: ops/remote/verify-host.sh

- [ ] **Step 1: Run all automated tests**

    .venv/bin/python -m pytest -q

Expected: legacy grading tests and all new tests pass.

- [ ] **Step 2: Run staging smoke verification**

    ops/scripts/smoke-staging.sh

Expected final lines:

    PASS health
    PASS quote
    PASS fake payment
    PASS worker lease
    PASS result delivery
    PASS review and refund
    PASS backup restore
    PASS remote foundation pre-domain

- [ ] **Step 3: Commit the verified MVP**

    git add tests/integration ops/scripts/smoke-staging.sh
    git commit -m "test: verify grading service mvp end to end"
