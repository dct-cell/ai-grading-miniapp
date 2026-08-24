from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.config import Environment, ServerSettings
from server.main import create_app
from server.models import Order
from server.models.base import Base


SHARED_KEY = "worker-shared-key-" + "w" * 32
#: Retained so tests can assert the retired Phase 05 credential authenticates
#: nothing, and that it never appears in a response body.
ADMIN_SHARED_KEY = "admin-shared-key-" + "a" * 32
ADMIN_PASSWORD = "correct horse battery staple"
ADMIN_ORIGIN = "http://localhost:5173"
RESULT_JSON = b'{"score": 21, "problems": []}'


def result_json_bytes_for_job(job: dict) -> bytes:
    """Build a valid public result for the frozen lease configuration."""
    standard = job["grading_standard"]
    if standard == "imo":
        maximum = 7
        resolved_scope = None
    elif standard == "cmo":
        maximum = 21
        resolved_scope = None
    else:
        maximum = 40
        resolved_scope = job.get("league_scope")
        if resolved_scope in {None, "auto"}:
            resolved_scope = "problem_set"

    common = {
        "service_tier": job["service_tier"],
        "grading_standard": standard,
        "resolved_league_scope": resolved_scope,
        "title": "数学竞赛题批改",
        "total_score": 0,
        "max_score": maximum,
    }
    if job["service_tier"] == "summary_report":
        result = {
            **common,
            "problems": [{
                "label": "第 1 题",
                "score": 0,
                "max_score": maximum,
                "verdict": "测试判断。",
                "issues": [{
                    "title": "演示模式",
                    "reason": "测试结果不代表真实数学评分。",
                    "deduction": maximum,
                }],
            }],
        }
    else:
        result = {
            **common,
            "overall_summary": "测试报告。",
            "problems": [{
                "label": "第 1 题",
                "score": 0,
                "max_score": maximum,
                "summary": "测试判断。",
            }],
            "pages": [{
                "page": page,
                "problem": "第 1 题",
                "score": 0,
                "max_score": maximum,
                "page_summary": "测试页。",
                "findings": [],
            } for page in range(1, job["page_count"] + 1)],
        }
    return json.dumps(result, ensure_ascii=False).encode("utf-8")


def make_pdf_bytes(pages: int = 1) -> bytes:
    buffer = BytesIO()
    document = canvas.Canvas(buffer, pagesize=(595, 842))
    for page_number in range(1, pages + 1):
        document.drawString(72, 780, f"Solution page {page_number}")
        document.showPage()
    document.save()
    return buffer.getvalue()


def make_encrypted_pdf_bytes() -> bytes:
    source = PdfReader(BytesIO(make_pdf_bytes()))
    writer = PdfWriter()
    for page in source.pages:
        writer.add_page(page)
    writer.encrypt("secret")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    values: dict[str, object] = {
        "environment": Environment.TEST,
        "database_url": f"sqlite+pysqlite:///{tmp_path}/phase02.sqlite3",
        "data_dir": tmp_path / "data",
        "session_secret": "s" * 32,
        "worker_shared_key": SHARED_KEY,
        "admin_shared_key": ADMIN_SHARED_KEY,
        "admin_origin": ADMIN_ORIGIN,
        "summary_report_enabled": True,
    }
    values.update(overrides)
    if values.get("environment") is Environment.PRODUCTION:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_path = tmp_path / "wechat-merchant-private.pem"
        public_path = tmp_path / "wechat-platform-public.pem"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        values.update(
            {
                "wechat_app_id": "wx-test-app-id",
                "wechat_app_secret": "x" * 32,
                "wechat_pay_merchant_id": "1900000001",
                "wechat_pay_certificate_serial": "TEST-MERCHANT-SERIAL",
                "wechat_pay_private_key_path": private_path,
                "wechat_pay_public_key_id": "TEST-WECHAT-PUBLIC-ID",
                "wechat_pay_public_key_path": public_path,
                "wechat_pay_api_v3_key": "v" * 32,
            }
        )
    return ServerSettings(**values)


def build_client(settings: ServerSettings) -> TestClient:
    app = create_app(settings)
    Base.metadata.create_all(app.state.session_factory.kw["bind"])
    return TestClient(app)


def login(client: TestClient, code: str = "test-parent-1") -> dict:
    response = client.post("/api/v1/auth/login", json={"code": code})
    assert response.status_code == 200
    return response.json()


def authenticate(client: TestClient, code: str = "test-parent-1") -> dict:
    body = login(client, code)
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    return body["user"]


def create_quote(
    client: TestClient,
    *,
    pages: int = 2,
    service_tier: str = "annotated_review",
    grading_standard: str = "imo",
    note: str = "",
    reference_pages: int | None = None,
) -> dict:
    files = {"source_pdf": ("answers.pdf", make_pdf_bytes(pages), "application/pdf")}
    if reference_pages is not None:
        files["reference_pdf"] = (
            "reference.pdf",
            make_pdf_bytes(reference_pages),
            "application/pdf",
        )
    response = client.post(
        "/api/v1/quotes",
        files=files,
        data={
            "service_tier": service_tier,
            "grading_standard": grading_standard,
            "note": note,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def worker_headers(worker_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SHARED_KEY}",
        "X-Worker-ID": worker_id,
    }


def register_worker(
    client: TestClient,
    *,
    installation_id: str = "install-default",
    device_name: str = "test-worker",
    platform: str = "darwin",
    architecture: str = "arm64",
    worker_version: str = "3.0.0",
    codex_version: str | None = None,
    tex_version: str | None = None,
    capabilities: dict[str, object] | None = None,
) -> dict:
    payload: dict[str, object] = {
        "installation_id": installation_id,
        "device_name": device_name,
        "platform": platform,
        "architecture": architecture,
        "worker_version": worker_version,
    }
    if codex_version is not None:
        payload["codex_version"] = codex_version
    if tex_version is not None:
        payload["tex_version"] = tex_version
    if capabilities is not None:
        payload["capabilities"] = capabilities

    response = client.post(
        "/worker/v1/register",
        json=payload,
        headers={"Authorization": f"Bearer {SHARED_KEY}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def settings(tmp_path: Path) -> ServerSettings:
    return build_settings(tmp_path)


@pytest.fixture
def client(settings: ServerSettings) -> Iterator[TestClient]:
    with build_client(settings) as test_client:
        yield test_client


@pytest.fixture
def session_factory(client: TestClient) -> sessionmaker[Session]:
    return client.app.state.session_factory


@pytest.fixture
def authenticated_client(client: TestClient) -> TestClient:
    authenticate(client)
    return client


@pytest.fixture
def two_page_pdf() -> bytes:
    return make_pdf_bytes(2)


@pytest.fixture
def quote_id(authenticated_client: TestClient) -> str:
    return create_quote(authenticated_client)["id"]


def pay_for_new_order(
    client: TestClient,
    *,
    pages: int = 2,
    note: str = "",
    service_tier: str = "annotated_review",
    grading_standard: str = "imo",
    reference_pages: int | None = None,
) -> str:
    """Drive the verified Phase 02 intake path and return the order id.

    The order id is resolved from the quote rather than by matching page
    counts, so a test may create several orders without the helper picking the
    wrong one.
    """
    quote = create_quote(
        client,
        pages=pages,
        note=note,
        service_tier=service_tier,
        grading_standard=grading_standard,
        reference_pages=reference_pages,
    )
    prepay = client.post(
        "/api/v1/payments/prepay", json={"quote_id": quote["id"]}
    ).json()
    callback = client.post(
        "/callbacks/fake/pay",
        json={"fake_transaction_id": prepay["prepay_id"], "status": "SUCCESS"},
    )
    assert callback.status_code == 204, callback.text

    factory = client.app.state.session_factory
    with factory() as session:
        order_id = session.scalar(
            select(Order.id).where(Order.quote_session_id == quote["id"])
        )
    assert order_id is not None, quote["id"]
    return order_id


def deliver_round(
    client: TestClient,
    worker_id: str,
    *,
    result_pages: int | None = None,
    expected_order_id: str | None = None,
) -> dict:
    """Take the next queued job all the way through Worker delivery.

    Aftersales behaviour depends on genuinely delivered state — the
    acceptance_deadline written by the result service, the SUCCEEDED job and
    the round's delivered_at — so tests drive the real Phase 03/04 control
    plane instead of hand-writing rows.

    Leases are handed out FIFO across the whole queue. Callers with more than
    one order in flight should pass ``expected_order_id`` to assert they got
    the job they meant, rather than silently delivering somebody else's.
    """
    leased = client.post(
        "/worker/v1/jobs/lease",
        headers={**worker_headers(worker_id), "Prefer": "wait=0"},
    )
    assert leased.status_code == 200, leased.text
    job = leased.json()
    if expected_order_id is not None:
        assert job["order_id"] == expected_order_id, (
            "leases are FIFO across the whole queue; another order wasahead"
        )
    lease_version = job["lease_version"]

    acked = client.post(
        f"/worker/v1/jobs/{job['job_id']}/ack",
        json={"lease_version": lease_version},
        headers=worker_headers(worker_id),
    )
    assert acked.status_code == 200, acked.text

    grants = client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/uploads",
        json={"lease_version": lease_version},
        headers=worker_headers(worker_id),
    )
    assert grants.status_code == 200, grants.text
    tokens = grants.json()

    uploaded: dict[str, str] = {}
    result_json = result_json_bytes_for_job(job)
    if result_pages is None:
        result_pages = (
            1
            if job["service_tier"] == "summary_report"
            else job["page_count"] + 1
        )
    for kind, payload in (
        ("result_json", result_json),
        ("result_pdf", make_pdf_bytes(result_pages)),
    ):
        response = client.put(
            f"/worker/v1/jobs/{job['job_id']}/result/{kind}",
            content=payload,
            headers={
                **worker_headers(worker_id),
                "X-Upload-Token": tokens[kind]["upload_token"],
                "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                "Content-Type": "application/octet-stream",
            },
        )
        assert response.status_code == 201, response.text
        uploaded[f"{kind}_file_id"] = response.json()["file_id"]

    committed = client.post(
        f"/worker/v1/jobs/{job['job_id']}/result/commit",
        json={"lease_version": lease_version, **uploaded},
        headers=worker_headers(worker_id),
    )
    assert committed.status_code == 200, committed.text
    return {**job, **uploaded, "commit": committed.json()}


def deliver_v1_order(client: TestClient, *, pages: int = 2) -> dict:
    """Return an order sitting in V1_DELIVERED with its acceptance window open.

    Assumes no other job is queued ahead of this one; leases are FIFO across
    the whole queue.
    """
    order_id = pay_for_new_order(client, pages=pages)
    worker_id = register_worker(
        client, installation_id=f"install-deliver-{order_id[:8]}"
    )["worker_id"]
    job = deliver_round(client, worker_id, expected_order_id=order_id)
    return {"order_id": order_id, "worker_id": worker_id, "job": job}


def make_refund_request(
    client: TestClient,
    *,
    pages: int = 2,
    reason: str = "grading_disputed",
) -> dict:
    """Deliver an order, then open a user refund without executing it.

    Returns the order and refund ids so a test can drive execution itself.
    """
    order_id = deliver_v1_order(client, pages=pages)["order_id"]
    response = client.post(
        f"/api/v1/orders/{order_id}/refund", json={"reason": reason}
    )
    assert response.status_code in {200, 202}, response.text
    body = response.json()
    assert body["refund_id"], body
    return {"order_id": order_id, **body}


def create_admin(
    session_factory: sessionmaker[Session],
    *,
    username: str = "phase07-admin",
    password: str = ADMIN_PASSWORD,
    disabled_at: object = None,
) -> str:
    """Insert an admin_users row with a real Argon2id hash and return its id.

    Phase 05 stored the unix "no password login" marker here because it
    authenticated with a shared key. Phase 07 authenticates with the password,
    so the hash has to be genuine — hashing it here rather than inserting a
    fixture constant also keeps the tests honest about the stored format.
    """
    from server.models import AdminUser
    from server.services.admin_sessions import hash_password

    with session_factory() as session:
        admin = AdminUser(
            username=username,
            password_hash=hash_password(password),
            disabled_at=disabled_at,
        )
        session.add(admin)
        session.commit()
        return admin.id


def admin_login(
    client: TestClient,
    *,
    username: str = "phase07-admin",
    password: str = ADMIN_PASSWORD,
) -> str:
    """Log an admin in and return the CSRF token for subsequent mutations.

    The session itself rides on the HttpOnly cookie that ``TestClient`` stores
    automatically, which is exactly how the browser SPA works: no caller ever
    handles the raw session token.
    """
    response = client.post(
        "/admin/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 204, response.text
    session = client.get("/admin/api/v1/auth/session")
    assert session.status_code == 200, session.text
    return session.json()["csrf_token"]


def admin_headers(csrf_token: str) -> dict[str, str]:
    """Headers for an admin mutation: CSRF token plus an allowed Origin."""
    return {"X-CSRF-Token": csrf_token, "Origin": ADMIN_ORIGIN}
