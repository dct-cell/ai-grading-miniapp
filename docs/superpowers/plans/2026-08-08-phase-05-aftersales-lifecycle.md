# Phase 05 Aftersales and Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement V1/V2 acceptance windows, one review, full user refunds, Admin technical refunds, dynamic ETA, automatic acceptance, and file expiry exactly as agreed.

**Architecture:** Pure policy functions decide eligibility and refund routing; transactional services apply state changes and create jobs/refunds; one scheduler process owns time-based transitions. PaymentGateway performs external actions behind an idempotent refund record.

**Tech Stack:** Python, FastAPI, SQLAlchemy, zoneinfo Asia/Shanghai, scheduler process, pytest/freezegun

---

### Task 1: Implement refund policy as a pure function

**Files:**
- Create: server/domain/refund_policy.py
- Test: tests/server/test_refund_policy.py

- [ ] **Step 1: Write the boundary table**

    @pytest.mark.parametrize(
        ("amount", "month_count", "paid", "refunded", "expected"),
        [
            (5000, 0, 10000, 0, "automatic"),
            (5000, 3, 10000, 0, "automatic"),
            (5000, 4, 20000, 0, "automatic"),
            (5000, 4, 10000, 0, "manual"),
            (5001, 0, 10000, 0, "manual"),
        ],
    )
    def test_refund_route(amount, month_count, paid, refunded, expected) -> None:
        facts = RefundFacts(
            order_amount_cents=amount,
            monthly_user_refund_count=month_count,
            lifetime_paid_cents=paid,
            lifetime_user_refunded_cents=refunded,
        )
        assert decide_refund_route(facts).value == expected

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_refund_policy.py -q

Expected: refund policy module is missing.

- [ ] **Step 3: Implement exact policy**

    class RefundRoute(StrEnum):
        AUTOMATIC = "automatic"
        MANUAL = "manual"

    def decide_refund_route(facts: RefundFacts) -> RefundRoute:
        projected = facts.lifetime_user_refunded_cents + facts.order_amount_cents
        projected_ratio = Decimal(projected) / Decimal(facts.lifetime_paid_cents)
        within_amount = facts.order_amount_cents <= 5000
        within_count = facts.monthly_user_refund_count < 4
        within_ratio = projected_ratio <= Decimal("0.30")
        if within_amount and (within_count or within_ratio):
            return RefundRoute.AUTOMATIC
        return RefundRoute.MANUAL

Count only user-requested refunds. Use calendar month boundaries in Asia/Shanghai. Technical refunds are excluded from monthly count, numerator and rate decision.

- [ ] **Step 4: Run and commit**

    .venv/bin/python -m pytest tests/server/test_refund_policy.py -q
    git add server/domain/refund_policy.py tests/server/test_refund_policy.py
    git commit -m "feat: encode user refund policy"

### Task 2: Add V1 accept, review and refund actions

**Files:**
- Create: server/services/aftersales.py
- Create: server/api/miniapp_aftersales.py
- Modify: server/main.py
- Test: tests/server/test_v1_aftersales.py

- [ ] **Step 1: Write mutually-exclusive action tests**

    def test_v1_review_creates_exactly_one_v2_job(authenticated_client, v1_order) -> None:
        response = authenticated_client.post(
            f"/api/v1/orders/{v1_order.id}/review",
            json={"text": "第2题下界证明判断有误"},
        )
        assert response.status_code == 202
        assert response.json()["state"] == "v2_queued"

    def test_v1_refund_ends_review_path(authenticated_client, v1_order) -> None:
        response = authenticated_client.post(
            f"/api/v1/orders/{v1_order.id}/refund",
            json={"reason": "uploaded_wrong_pdf"},
        )
        assert response.status_code in {202, 200}
        assert authenticated_client.post(
            f"/api/v1/orders/{v1_order.id}/review",
            json={"text": "再次提交"},
        ).status_code == 409

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_v1_aftersales.py -q

Expected: routes are missing.

- [ ] **Step 3: Implement V1 actions**

Lock the order and verify owner, V1_DELIVERED, deadline not expired and no previous appeal/refund. Accept moves to ACCEPTED. Review inserts Appeal, creates round 2 and a queued V2 job using the same immutable source/reference FileObjects. Refund creates one full-amount Refund and moves to REFUND_PENDING; it never accepts a replacement PDF.

- [ ] **Step 4: Add race test**

Submit review and refund concurrently for one V1 order. Exactly one transaction succeeds; the other returns 409. Assert one of Appeal or Refund exists, never both.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_v1_aftersales.py -q
    git add server/services/aftersales.py server/api/miniapp_aftersales.py server/main.py tests/server/test_v1_aftersales.py
    git commit -m "feat: add v1 acceptance review and refund"

### Task 3: Add V2 acceptance and refund

**Files:**
- Modify: server/services/aftersales.py
- Modify: server/api/miniapp_aftersales.py
- Test: tests/server/test_v2_aftersales.py

- [ ] **Step 1: Write no-third-round test**

    def test_v2_has_no_review_endpoint(authenticated_client, v2_order) -> None:
        response = authenticated_client.post(
            f"/api/v1/orders/{v2_order.id}/review",
            json={"text": "third attempt"},
        )
        assert response.status_code == 409

    def test_v2_allows_full_refund(authenticated_client, v2_order) -> None:
        response = authenticated_client.post(
            f"/api/v1/orders/{v2_order.id}/refund",
            json={"reason": "grading_disputed"},
        )
        assert response.status_code in {200, 202}
        assert response.json()["amount_cents"] == v2_order.paid_amount_cents

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_v2_aftersales.py -q

Expected: current service does not distinguish rounds.

- [ ] **Step 3: Implement round-aware action availability**

Return available_actions in order detail:
- V1 delivered: accept, review, refund;
- V2 delivered: accept, refund;
- accepted/refunded/expired: none.

- [ ] **Step 4: Run and commit**

    .venv/bin/python -m pytest tests/server/test_v2_aftersales.py -q
    git add server/services/aftersales.py server/api/miniapp_aftersales.py tests/server/test_v2_aftersales.py
    git commit -m "feat: add v2 acceptance and refund"

### Task 4: Execute automatic and manual refunds safely

**Files:**
- Create: server/services/refunds.py
- Modify: server/adapters/payments.py
- Create: server/api/admin_refunds.py
- Test: tests/server/test_refund_execution.py

- [ ] **Step 1: Write idempotency tests**

    def test_retry_uses_same_external_refund_id(refund_service, pending_refund, gateway) -> None:
        gateway.fail_once()
        refund_service.execute(pending_refund.id)
        refund_service.execute(pending_refund.id)
        assert gateway.external_ids == [pending_refund.external_refund_id] * 2
        assert refund_service.get(pending_refund.id).state == "refunded"

    def test_technical_refund_does_not_change_user_metrics(refund_service, running_order) -> None:
        before = refund_service.user_metrics(running_order.user_id)
        refund_service.create_technical_refund(running_order.id, admin_id="admin-1")
        assert refund_service.user_metrics(running_order.user_id) == before

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_refund_execution.py -q

Expected: RefundService is missing.

- [ ] **Step 3: Implement the gateway refund seam**

    @dataclass(frozen=True)
    class RefundRequest:
        external_refund_id: str
        external_transaction_id: str
        amount_cents: int
        reason: str

    class PaymentGateway(Protocol):
        def refund(self, request: RefundRequest) -> RefundResult: ...

Persist Refund before calling the gateway. Automatic route executes immediately; manual route waits for an Admin decision. A successful gateway result sets REFUNDED, revokes downloads and moves the order to REFUNDED. Failure sets refund_failed and remains retryable; it must not mark the order refunded.

- [ ] **Step 4: Implement Admin approve/reject**

Approve executes the same idempotent refund method. Reject records reviewer and reason, moves order to ACCEPTED, preserves downloads until normal expiry, and writes AuditLog. Technical refund bypasses user policy and uses source admin_technical.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_refund_execution.py -q
    git add server/services/refunds.py server/adapters/payments.py server/api/admin_refunds.py tests/server/test_refund_execution.py
    git commit -m "feat: execute idempotent full refunds"

### Task 5: Add scheduler-owned time transitions

**Files:**
- Create: server/scheduler/__init__.py
- Create: server/scheduler/main.py
- Create: server/scheduler/tasks.py
- Test: tests/server/test_scheduler_tasks.py

- [ ] **Step 1: Write deadline tests**

    def test_delivery_auto_accepts_after_three_days(tasks, clock, delivered_order) -> None:
        clock.move_to(delivered_order.acceptance_deadline + timedelta(seconds=1))
        tasks.auto_accept_expired_orders()
        assert tasks.get_order(delivered_order.id).state == "accepted"

    def test_unpaid_quote_files_delete_after_24_hours(tasks, expired_quote) -> None:
        tasks.delete_expired_quotes()
        assert tasks.get_file(expired_quote.source_file_id).state == "deleted"

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_scheduler_tasks.py -q

Expected: SchedulerTasks is missing.

- [ ] **Step 3: Implement idempotent tasks**

Implement auto_accept_expired_orders, release_unacknowledged_leases, mark_expired_running_leases, delete_expired_quotes, delete_expired_order_files, retry_failed_refund_queries and verify_backup_freshness. Each task selects bounded batches, locks rows, can run repeatedly, and records the last successful run.

- [ ] **Step 4: Implement one scheduler process**

    async def scheduler_loop(tasks: SchedulerTasks) -> None:
        while True:
            tasks.run_due()
            await asyncio.sleep(20)

Use a MySQL advisory lock named grader-scheduler so accidental duplicate processes cannot both own scheduling.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_scheduler_tasks.py -q
    git add server/scheduler tests/server/test_scheduler_tasks.py
    git commit -m "feat: add lifecycle scheduler"

### Task 6: Compute ETA across all healthy Workers

**Files:**
- Create: server/domain/eta.py
- Modify: server/services/orders.py
- Test: tests/server/test_eta.py

- [ ] **Step 1: Write multi-Worker scheduling test**

    def test_eta_assigns_queue_to_earliest_available_worker() -> None:
        finish = estimate_finish_times(
            now=datetime(2026, 8, 8, tzinfo=timezone.utc),
            worker_available_minutes=[0, 20],
            queued=[("a", 3), ("b", 1), ("c", 4)],
            minutes_per_page=10,
        )
        assert finish["a"] == 30
        assert finish["b"] == 30
        assert finish["c"] == 70

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_eta.py -q

Expected: estimator is missing.

- [ ] **Step 3: Implement min-heap simulation**

Initialize one heap entry per ready Worker using current-job remaining time. Iterate unified FIFO jobs, pop earliest available Worker, add pages times configured minutes_per_page, record finish, and push back. Return a range by adding a configurable 20 percent uncertainty margin.

- [ ] **Step 4: Add degraded-capacity tests**

Verify an offline Worker is excluded, worker_exception orders show no countdown, and ETA recalculates when Worker count changes.

- [ ] **Step 5: Run the phase gate and commit**

    .venv/bin/python -m pytest tests/server -q
    .venv/bin/python -m pytest -q
    git add server/domain/eta.py server/services/orders.py tests/server/test_eta.py
    git commit -m "feat: estimate completion across workers"
