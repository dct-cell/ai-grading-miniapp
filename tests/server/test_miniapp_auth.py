from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.config import Environment, ServerSettings
from server.main import create_app
from server.models import MiniappSession, User
from server.models.base import Base


WORKER_SHARED_KEY = "w" * 32
ADMIN_SHARED_KEY = "a" * 32


@pytest.fixture
def settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        environment=Environment.TEST,
        database_url=f"sqlite+pysqlite:///{tmp_path}/auth.sqlite3",
        data_dir=tmp_path / "data",
        session_secret="s" * 32,
        worker_shared_key=WORKER_SHARED_KEY,
        admin_shared_key=ADMIN_SHARED_KEY,
    )


@pytest.fixture
def client(settings: ServerSettings) -> Iterator[TestClient]:
    app = create_app(settings)
    Base.metadata.create_all(app.state.session_factory.kw["bind"])
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_factory(client: TestClient) -> sessionmaker[Session]:
    return client.app.state.session_factory


def test_fake_login_creates_reusable_user_and_session(client: TestClient) -> None:
    first = client.post("/api/v1/auth/login", json={"code": "test-parent-1"})
    second = client.post("/api/v1/auth/login", json={"code": "test-parent-1"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert first.json()["access_token"] != second.json()["access_token"]


def test_login_rejects_codes_the_fake_provider_does_not_own(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.post("/api/v1/auth/login", json={"code": "wechat-real-code"})

    assert response.status_code == 400
    assert "wechat-real-code" not in response.text
    with session_factory() as session:
        assert session.scalars(select(User)).all() == []


def test_login_stores_only_the_token_hash(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()
    raw_token = body["access_token"]

    with session_factory() as session:
        stored = session.scalars(select(MiniappSession)).one()

    assert len(raw_token) >= 32
    assert stored.token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert raw_token not in stored.token_hash
    assert stored.expires_at > datetime.now(timezone.utc) + timedelta(days=29)
    assert stored.expires_at < datetime.now(timezone.utc) + timedelta(days=31)


def test_login_response_never_exposes_the_internal_openid(client: TestClient) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()

    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    assert set(body["user"]) == {"id", "public_id"}
    assert "fake:" not in str(body)


def test_public_id_uses_a_stable_prefix_and_eight_hex_characters(
    client: TestClient,
) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()

    assert re.fullmatch(r"u-[0-9a-f]{8}", body["user"]["public_id"])


def test_me_returns_the_authenticated_user(client: TestClient) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()

    response = client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json() == body["user"]


def test_me_requires_a_bearer_token(client: TestClient) -> None:
    assert client.get("/api/v1/me").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {WORKER_SHARED_KEY}"},
        {"Authorization": WORKER_SHARED_KEY},
        {"X-Worker-Key": WORKER_SHARED_KEY},
    ],
)
def test_worker_shared_key_cannot_authenticate_as_a_miniapp_user(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    client.post("/api/v1/auth/login", json={"code": "test-parent-1"})

    response = client.get("/api/v1/me", headers=headers)

    assert response.status_code == 401


def test_admin_cookie_cannot_authenticate_as_a_miniapp_user(
    client: TestClient,
) -> None:
    body = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()
    client.cookies.set("admin_session", body["access_token"])

    response = client.get("/api/v1/me")

    assert response.status_code == 401


def test_expired_session_is_rejected(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    token = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()[
        "access_token"
    ]
    with session_factory() as session:
        stored = session.scalars(select(MiniappSession)).one()
        stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(stored)
        session.commit()

    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_revoked_session_is_rejected(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    token = client.post("/api/v1/auth/login", json={"code": "test-parent-1"}).json()[
        "access_token"
    ]
    with session_factory() as session:
        stored = session.scalars(select(MiniappSession)).one()
        stored.revoked_at = datetime.now(timezone.utc)
        session.add(stored)
        session.commit()

    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_unauthorized_errors_never_leak_secrets(
    client: TestClient,
    settings: ServerSettings,
) -> None:
    forged = "forged-token-value"

    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    body = response.text
    assert forged not in body
    assert settings.worker_shared_key not in body
    assert settings.session_secret not in body
    assert settings.database_url not in body


def test_token_hash_is_not_accepted_as_a_token(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    client.post("/api/v1/auth/login", json={"code": "test-parent-1"})
    with session_factory() as session:
        stored = session.scalars(select(MiniappSession)).one()

    response = client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {stored.token_hash}"},
    )

    assert response.status_code == 401


def test_each_login_code_maps_to_a_distinct_user(client: TestClient) -> None:
    first = client.post("/api/v1/auth/login", json={"code": "test-alice"}).json()
    second = client.post("/api/v1/auth/login", json={"code": "test-bob"}).json()

    assert first["user"]["id"] != second["user"]["id"]
    assert first["user"]["public_id"] != second["user"]["public_id"]

    response = client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert response.json()["id"] == first["user"]["id"]


def test_fake_auth_provider_rejects_non_test_codes() -> None:
    from server.adapters.auth import FakeAuthProvider

    provider = FakeAuthProvider()

    assert provider.exchange_code("test-parent-1").openid == "fake:test-parent-1"
    with pytest.raises(ValueError, match="invalid fake login code"):
        provider.exchange_code("parent-1")
