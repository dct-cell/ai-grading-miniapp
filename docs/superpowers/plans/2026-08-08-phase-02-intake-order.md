# Phase 02 Intake and Order Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the first vertical slice from test-account login through PDF quote and fake payment callback to an immutable paid order with a queued V1 grading job.

**Architecture:** Auth, file storage and payment are protocols with fake and production adapters. Quote files remain temporary for 24 hours; a verified payment callback consumes the quote exactly once and creates the order, payment, first grading round and queue record in one transaction.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, local filesystem, existing app.pdf_utils validation, SHA-256, pytest

---

### Task 1: Add mini-program sessions and FakeAuth

**Files:**
- Create: server/adapters/auth.py
- Create: server/services/sessions.py
- Create: server/api/dependencies.py
- Create: server/api/miniapp_auth.py
- Modify: server/main.py
- Test: tests/server/test_miniapp_auth.py

- [ ] **Step 1: Write the failing login test**

    def test_fake_login_creates_reusable_user_and_session(client) -> None:
        first = client.post("/api/v1/auth/login", json={"code": "test-parent-1"})
        second = client.post("/api/v1/auth/login", json={"code": "test-parent-1"})
        assert first.status_code == 200
        assert first.json()["user"]["id"] == second.json()["user"]["id"]
        assert first.json()["access_token"] != second.json()["access_token"]

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_miniapp_auth.py -q

Expected: 404 for the login route.

- [ ] **Step 3: Define and implement the adapter contract**

    from dataclasses import dataclass
    from typing import Protocol

    @dataclass(frozen=True)
    class ExternalIdentity:
        openid: str
        nickname: str

    class AuthProvider(Protocol):
        def exchange_code(self, code: str) -> ExternalIdentity: ...

    class FakeAuthProvider:
        def exchange_code(self, code: str) -> ExternalIdentity:
            if not code.startswith("test-"):
                raise ValueError("invalid fake login code")
            return ExternalIdentity(openid=f"fake:{code}", nickname="测试家长")

- [ ] **Step 4: Implement opaque sessions**

Generate the raw token with secrets.token_urlsafe(32), store only sha256(raw_token).hexdigest(), set a 30-day expiry, and return the raw token once. Generate public_id as a stable prefix plus eight random lowercase hex characters.

- [ ] **Step 5: Protect a probe endpoint**

    @router.get("/me")
    def me(user: Annotated[User, Depends(current_miniapp_user)]) -> UserView:
        return UserView.model_validate(user)

Send the token as Authorization: Bearer value. A Worker shared key and an Admin cookie must receive 401 on this route.

- [ ] **Step 6: Run and commit**

    .venv/bin/python -m pytest tests/server/test_miniapp_auth.py -q
    git add server/adapters/auth.py server/services/sessions.py server/api server/main.py tests/server/test_miniapp_auth.py
    git commit -m "feat: add miniapp test login sessions"

### Task 2: Add safe local file storage

**Files:**
- Create: server/adapters/files.py
- Create: server/services/files.py
- Test: tests/server/test_file_store.py

- [ ] **Step 1: Write the failing atomic-storage test**

    def test_put_temporary_pdf_is_atomic_and_hashed(tmp_path, sample_pdf) -> None:
        store = LocalFileStore(tmp_path)
        stored = store.put_temporary("file-1", sample_pdf)
        assert stored.relative_path == "temporary/file-1.pdf"
        assert len(stored.sha256) == 64
        assert not list((tmp_path / "staging").glob("*"))

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_file_store.py -q

Expected: import failure for LocalFileStore.

- [ ] **Step 3: Implement LocalFileStore**

    @dataclass(frozen=True)
    class StoredFile:
        relative_path: str
        size_bytes: int
        sha256: str

    class LocalFileStore:
        def __init__(self, root: Path):
            self.root = root

        def put_temporary(self, file_id: str, source: BinaryIO) -> StoredFile:
            staging = self.root / "staging" / f"{file_id}.part"
            target = self.root / "temporary" / f"{file_id}.pdf"
            staging.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with staging.open("wb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(staging, target)
            return StoredFile(str(target.relative_to(self.root)), size, digest.hexdigest())

- [ ] **Step 4: Reuse existing PDF validation**

After writing, call app.pdf_utils.inspect_pdf with configured page limit. On validation error, delete the new temporary object and leave no FileObject row.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_file_store.py -q
    git add server/adapters/files.py server/services/files.py tests/server/test_file_store.py
    git commit -m "feat: add atomic local PDF storage"

### Task 3: Create quote sessions

**Files:**
- Create: server/services/quotes.py
- Create: server/schemas/quotes.py
- Create: server/api/miniapp_quotes.py
- Modify: server/main.py
- Test: tests/server/test_quotes_api.py

- [ ] **Step 1: Write the failing quote test**

    def test_quote_counts_source_pages_and_uses_versioned_price(
        authenticated_client, two_page_pdf
    ) -> None:
        response = authenticated_client.post(
            "/api/v1/quotes",
            files={"source_pdf": ("answers.pdf", two_page_pdf, "application/pdf")},
            data={"grading_standard": "imo", "note": "重点检查下界证明"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["page_count"] == 2
        assert body["amount_cents"] == 2000
        assert body["expires_in_seconds"] == 86400

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_quotes_api.py -q

Expected: 404.

- [ ] **Step 3: Implement QuoteService.create**

The service must:
1. reject unknown grading standards;
2. save source PDF and optional reference PDF as separate FileObject rows;
3. use only source PDF pages for price;
4. snapshot price_rule_id and quoted_amount_cents;
5. set both files and quote to expire in 24 hours;
6. commit all rows in one transaction.

- [ ] **Step 4: Define the response schema**

    class QuoteView(BaseModel):
        id: str
        page_count: int
        cents_per_page: int
        amount_cents: int
        expires_at: datetime
        expires_in_seconds: int
        grading_standard: Literal["league_second_round", "cmo", "imo"]
        note: str

- [ ] **Step 5: Test optional reference PDF and limits**

Add tests that verify the reference PDF does not change price, encrypted PDFs fail with 400, over-limit PDFs fail with 400, and a user cannot read another user's quote.

- [ ] **Step 6: Run and commit**

    .venv/bin/python -m pytest tests/server/test_quotes_api.py -q
    git add server/services/quotes.py server/schemas/quotes.py server/api/miniapp_quotes.py server/main.py tests/server/test_quotes_api.py
    git commit -m "feat: add PDF quote sessions"

### Task 4: Add the fake payment seam and authoritative callback

**Files:**
- Create: server/adapters/payments.py
- Create: server/services/payments.py
- Create: server/api/miniapp_payments.py
- Create: server/api/callbacks.py
- Modify: server/main.py
- Test: tests/server/test_fake_payment.py

- [ ] **Step 1: Write the failing idempotency test**

    def test_duplicate_payment_callback_creates_one_order_and_one_job(
        authenticated_client, quote_id, session_factory
    ) -> None:
        prepay = authenticated_client.post(
            "/api/v1/payments/prepay", json={"quote_id": quote_id}
        ).json()
        payload = {"fake_transaction_id": prepay["prepay_id"], "status": "SUCCESS"}
        assert authenticated_client.post("/callbacks/fake/pay", json=payload).status_code == 204
        assert authenticated_client.post("/callbacks/fake/pay", json=payload).status_code == 204
        with session_factory() as session:
            assert session.scalar(select(func.count(Order.id))) == 1
            assert session.scalar(select(func.count(GradingJob.id))) == 1

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_fake_payment.py -q

Expected: routes are missing.

- [ ] **Step 3: Define the gateway**

    @dataclass(frozen=True)
    class PrepayRequest:
        merchant_order_id: str
        amount_cents: int
        description: str

    @dataclass(frozen=True)
    class PrepayResult:
        prepay_id: str
        client_payload: dict[str, str]

    class PaymentGateway(Protocol):
        def create_prepay(self, request: PrepayRequest) -> PrepayResult: ...

    class FakePaymentGateway:
        def create_prepay(self, request: PrepayRequest) -> PrepayResult:
            prepay_id = f"fake-{request.merchant_order_id}"
            return PrepayResult(prepay_id, {"fake_prepay_id": prepay_id})

- [ ] **Step 4: Implement callback transaction**

Lock the quote row. Verify it is unexpired, unconsumed, belongs to the payment intent and amount matches. In one transaction:
- insert Payment using unique external_transaction_id;
- move source/reference FileObject state from temporary to retained;
- create Order in V1_QUEUED;
- create GradingRound number 1;
- create GradingJob in QUEUED;
- set quote consumed_at.

On duplicate transaction ID, return 204 after verifying the existing payment points at the same order and amount.

Expose POST /api/v1/payments/{payment_id}/simulate-success only when the runtime environment is development, staging or test. It must require the authenticated quote owner and invoke the same verified callback service with a generated fake transaction ID. In production the route must not be registered and must return 404.

- [ ] **Step 5: Add negative tests**

Test expired quote, amount mismatch, forged callback, a quote consumed by another transaction, and front-end success without callback. None may create an order.

- [ ] **Step 6: Run and commit**

    .venv/bin/python -m pytest tests/server/test_fake_payment.py -q
    git add server/adapters/payments.py server/services/payments.py server/api server/main.py tests/server/test_fake_payment.py
    git commit -m "feat: create paid orders from verified callbacks"

### Task 5: Expose the initial order list

**Files:**
- Create: server/services/orders.py
- Create: server/schemas/orders.py
- Create: server/api/miniapp_orders.py
- Test: tests/server/test_orders_api.py

- [ ] **Step 1: Write the failing ownership test**

    def test_order_list_never_returns_another_users_order(
        alice_client, bob_order
    ) -> None:
        response = alice_client.get("/api/v1/orders")
        assert response.status_code == 200
        assert bob_order.id not in {item["id"] for item in response.json()["items"]}

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_orders_api.py -q

Expected: 404.

- [ ] **Step 3: Implement list and detail routes**

Return the three mini-program categories:
- all: every order owned by the user;
- grading: V1/V2 queued, running and worker exception;
- acceptance: V1/V2 delivered and refund pending.

Use keyset pagination ordered by created_at descending and ID descending. Never accept user_id from query parameters.

- [ ] **Step 4: Run the phase gate**

    .venv/bin/python -m pytest tests/server -q
    .venv/bin/python -m pytest -q

Expected: all tests pass.

- [ ] **Step 5: Commit**

    git add server/services/orders.py server/schemas/orders.py server/api/miniapp_orders.py tests/server/test_orders_api.py
    git commit -m "feat: expose owned miniapp orders"
