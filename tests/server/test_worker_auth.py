from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.config import Environment
from server.models import Worker
from tests.server.conftest import (
    SHARED_KEY,
    authenticate,
    build_client,
    build_settings,
    register_worker,
    worker_headers,
)


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def route_paths(app) -> set[str]:
    """Collect every registered path, including nested included routers."""
    collected: set[str] = set()
    pending = [app.router]
    while pending:
        router = pending.pop()
        for route in getattr(router, "routes", []):
            nested = getattr(route, "original_router", None)
            if nested is not None:
                pending.append(nested)
                continue
            path = getattr(route, "path", None)
            if path is not None:
                collected.add(path)
    return collected


def test_worker_requires_bearer_key_and_worker_id(client: TestClient) -> None:
    assert client.post("/worker/v1/heartbeat", json={}).status_code == 401


def test_worker_requires_the_worker_id_header(client: TestClient) -> None:
    register_worker(client)

    response = client.post(
        "/worker/v1/heartbeat",
        json={},
        headers={"Authorization": f"Bearer {SHARED_KEY}"},
    )

    assert response.status_code == 401


def test_worker_requires_the_shared_key(client: TestClient) -> None:
    worker_id = register_worker(client)["worker_id"]

    response = client.post(
        "/worker/v1/heartbeat",
        json={},
        headers={"X-Worker-ID": worker_id},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "wrong_key",
    [
        SHARED_KEY[:-1],
        SHARED_KEY + "x",
        SHARED_KEY.upper(),
        "",
        "k" * len(SHARED_KEY),
    ],
)
def test_worker_rejects_a_wrong_shared_key(client: TestClient, wrong_key: str) -> None:
    worker_id = register_worker(client)["worker_id"]

    response = client.post(
        "/worker/v1/heartbeat",
        json={},
        headers={
            "Authorization": f"Bearer {wrong_key}",
            "X-Worker-ID": worker_id,
        },
    )

    assert response.status_code == 401


def test_worker_rejects_an_unknown_worker_id(client: TestClient) -> None:
    register_worker(client)

    response = client.post(
        "/worker/v1/heartbeat",
        json={},
        headers=worker_headers("00000000-0000-0000-0000-000000000000"),
    )

    assert response.status_code == 401


def test_disabled_worker_is_forbidden(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker_id = register_worker(client)["worker_id"]
    with session_factory() as session:
        worker = session.get(Worker, worker_id)
        worker.status = "disabled"
        session.add(worker)
        session.commit()

    response = client.post(
        "/worker/v1/heartbeat", json={}, headers=worker_headers(worker_id)
    )

    assert response.status_code == 403


def test_shared_key_comparison_is_constant_time() -> None:
    """A byte-by-byte == on the shared key leaks it through timing."""
    from server.services import workers as worker_service

    source = inspect.getsource(worker_service.verify_shared_key)

    assert "compare_digest" in source
    assert worker_service.verify_shared_key(SHARED_KEY, SHARED_KEY) is True
    assert worker_service.verify_shared_key(SHARED_KEY[:-1], SHARED_KEY) is False
    assert worker_service.verify_shared_key(SHARED_KEY + "x", SHARED_KEY) is False
    assert worker_service.verify_shared_key("", SHARED_KEY) is False


def test_registration_returns_the_protocol_parameters(client: TestClient) -> None:
    body = register_worker(client)

    assert body["worker_id"]
    assert body["heartbeat_interval_seconds"] == 20
    assert body["lease_seconds"] == 120
    assert body["long_poll_seconds"] == 25
    assert body["minimum_worker_version"]


def test_registration_never_echoes_the_shared_key(client: TestClient) -> None:
    response = client.post(
        "/worker/v1/register",
        json={
            "installation_id": "install-echo",
            "device_name": "mac-studio",
            "platform": "darwin",
            "architecture": "arm64",
            "worker_version": "3.0.0",
        },
        headers={"Authorization": f"Bearer {SHARED_KEY}"},
    )

    assert response.status_code == 201
    assert SHARED_KEY not in response.text


def test_registration_requires_the_shared_key(client: TestClient) -> None:
    response = client.post(
        "/worker/v1/register",
        json={
            "installation_id": "install-1",
            "device_name": "mac-studio",
            "platform": "darwin",
            "architecture": "arm64",
            "worker_version": "3.0.0",
        },
    )

    assert response.status_code == 401


def test_registration_does_not_accept_a_client_supplied_worker_id(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.post(
        "/worker/v1/register",
        json={
            "installation_id": "install-forged",
            "device_name": "mac-studio",
            "platform": "darwin",
            "architecture": "arm64",
            "worker_version": "3.0.0",
            "worker_id": "forged-worker-id",
        },
        headers={"Authorization": f"Bearer {SHARED_KEY}"},
    )

    assert response.status_code == 201
    assert response.json()["worker_id"] != "forged-worker-id"
    with session_factory() as session:
        assert session.get(Worker, "forged-worker-id") is None


def test_repeated_registration_returns_the_same_worker_id(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = register_worker(client, installation_id="install-stable")
    second = register_worker(client, installation_id="install-stable")

    assert first["worker_id"] == second["worker_id"]
    with session_factory() as session:
        assert count(session, Worker) == 1


def test_distinct_installations_receive_distinct_worker_ids(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = register_worker(client, installation_id="install-a")
    second = register_worker(client, installation_id="install-b")

    assert first["worker_id"] != second["worker_id"]
    with session_factory() as session:
        assert count(session, Worker) == 2


def test_registration_records_the_reported_environment(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = register_worker(
        client,
        installation_id="install-env",
        device_name="tim-macbook",
        platform="darwin",
        architecture="arm64",
        worker_version="3.0.0",
        codex_version="codex 0.9.1",
        tex_version="XeTeX 3.141592653",
        capabilities={"xelatex": True, "codex": True},
    )

    with session_factory() as session:
        worker = session.get(Worker, body["worker_id"])

    assert worker.installation_id == "install-env"
    assert worker.device_name == "tim-macbook"
    assert worker.platform == "darwin"
    assert worker.architecture == "arm64"
    assert worker.worker_version == "3.0.0"
    assert worker.codex_version == "codex 0.9.1"
    assert worker.tex_version == "XeTeX 3.141592653"
    assert worker.capabilities == {"xelatex": True, "codex": True}
    assert worker.status == "online"
    assert worker.current_job_id is None


def test_re_registration_refreshes_the_reported_environment(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = register_worker(
        client,
        installation_id="install-upgrade",
        worker_version="3.0.0",
        codex_version="codex 0.9.0",
    )

    register_worker(
        client,
        installation_id="install-upgrade",
        worker_version="3.1.0",
        codex_version="codex 0.9.5",
    )

    with session_factory() as session:
        worker = session.get(Worker, body["worker_id"])

    assert worker.worker_version == "3.1.0"
    assert worker.codex_version == "codex 0.9.5"


def test_re_registration_does_not_re_enable_a_disabled_worker(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Only an operator may re-enable a Worker that was switched off."""
    worker_id = register_worker(client, installation_id="install-disabled")["worker_id"]
    with session_factory() as session:
        worker = session.get(Worker, worker_id)
        worker.status = "disabled"
        session.add(worker)
        session.commit()

    register_worker(client, installation_id="install-disabled")

    with session_factory() as session:
        assert session.get(Worker, worker_id).status == "disabled"


def test_heartbeat_records_the_server_side_timestamp(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker_id = register_worker(client)["worker_id"]
    with session_factory() as session:
        before = session.get(Worker, worker_id).last_heartbeat_at

    response = client.post(
        "/worker/v1/heartbeat", json={}, headers=worker_headers(worker_id)
    )

    assert response.status_code == 200
    with session_factory() as session:
        assert session.get(Worker, worker_id).last_heartbeat_at >= before


def test_heartbeat_recovers_a_worker_from_suspected_offline(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker_id = register_worker(client)["worker_id"]
    with session_factory() as session:
        worker = session.get(Worker, worker_id)
        worker.status = "suspected_offline"
        session.add(worker)
        session.commit()

    client.post("/worker/v1/heartbeat", json={}, headers=worker_headers(worker_id))

    with session_factory() as session:
        assert session.get(Worker, worker_id).status == "online"


def test_miniapp_session_token_cannot_reach_worker_routes(client: TestClient) -> None:
    """The mini-program and Worker authentication domains stay separate."""
    worker_id = register_worker(client)["worker_id"]
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()

    response = client.post(
        "/worker/v1/heartbeat",
        json={},
        headers={
            "Authorization": f"Bearer {body['access_token']}",
            "X-Worker-ID": worker_id,
        },
    )

    assert response.status_code == 401


def test_miniapp_session_token_cannot_register_a_worker(client: TestClient) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()

    response = client.post(
        "/worker/v1/register",
        json={
            "installation_id": "install-miniapp",
            "device_name": "mac-studio",
            "platform": "darwin",
            "architecture": "arm64",
            "worker_version": "3.0.0",
        },
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )

    assert response.status_code == 401


def test_worker_shared_key_cannot_reach_miniapp_routes(client: TestClient) -> None:
    worker_id = register_worker(client)["worker_id"]

    me = client.get("/api/v1/me", headers=worker_headers(worker_id))
    quotes = client.post("/api/v1/quotes", headers=worker_headers(worker_id))
    orders = client.get("/api/v1/orders", headers=worker_headers(worker_id))

    assert me.status_code == 401
    assert quotes.status_code == 401
    assert orders.status_code == 401


def test_worker_id_is_not_a_miniapp_session_token(client: TestClient) -> None:
    worker_id = register_worker(client)["worker_id"]

    response = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {worker_id}"}
    )

    assert response.status_code == 401


def test_worker_errors_never_leak_the_configured_secrets(
    client: TestClient,
    settings,
) -> None:
    worker_id = register_worker(client)["worker_id"]

    responses = [
        client.post("/worker/v1/heartbeat", json={}),
        client.post(
            "/worker/v1/heartbeat",
            json={},
            headers={"Authorization": "Bearer wrong-key", "X-Worker-ID": worker_id},
        ),
        client.post("/worker/v1/register", json={}),
    ]

    for response in responses:
        assert response.status_code in {401, 422}
        for secret in (
            settings.worker_shared_key,
            settings.session_secret,
            settings.database_url,
        ):
            assert secret not in response.text


def test_worker_routes_exist_in_production(tmp_path: Path) -> None:
    """Worker control-plane routes are real endpoints, not fake adapters."""
    from server.main import create_app

    settings = build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )
    app = create_app(settings)
    paths = route_paths(app)

    assert "/worker/v1/register" in paths
    assert "/worker/v1/heartbeat" in paths


def test_worker_authentication_is_independent_of_the_miniapp_gate(
    tmp_path: Path,
) -> None:
    """Worker auth must not depend on the non-production fake login router."""
    settings = build_settings(tmp_path, environment=Environment.STAGING)
    with build_client(settings) as client:
        worker_id = register_worker(client)["worker_id"]
        authenticate(client)
        del client.headers["Authorization"]

        response = client.post(
            "/worker/v1/heartbeat", json={}, headers=worker_headers(worker_id)
        )

    assert response.status_code == 200
