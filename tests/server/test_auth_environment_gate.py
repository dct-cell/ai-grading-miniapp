from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import Environment
from tests.server.conftest import build_client, build_settings


NON_PRODUCTION = [Environment.DEVELOPMENT, Environment.TEST, Environment.STAGING]


def _production_settings(tmp_path: Path):
    """Production settings. The URL is never connected to for route checks."""
    return build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )


def route_paths(app) -> set[str]:
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


@pytest.mark.parametrize("environment", NON_PRODUCTION)
def test_test_account_login_is_available_outside_production(
    tmp_path: Path,
    environment: Environment,
) -> None:
    settings = build_settings(tmp_path, environment=environment)

    with build_client(settings) as client:
        response = client.post("/api/v1/auth/login", json={"code": "test-parent-1"})

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_production_does_not_register_the_test_account_login(
    tmp_path: Path,
) -> None:
    from server.main import create_app

    app = create_app(_production_settings(tmp_path))

    paths = route_paths(app)

    assert "/api/v1/auth/login" not in paths
    assert "/api/v1/me" in paths


def test_production_openapi_omits_the_test_account_login(tmp_path: Path) -> None:
    from server.main import create_app

    with TestClient(create_app(_production_settings(tmp_path))) as client:
        documented = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/auth/login" not in documented
    assert "/api/v1/me" in documented


def test_production_test_account_login_request_returns_404(tmp_path: Path) -> None:
    from server.main import create_app

    app = create_app(_production_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/v1/auth/login", json={"code": "test-anyone"})
        protected = client.get("/api/v1/me")

    assert response.status_code == 404
    assert protected.status_code == 401


def test_production_cannot_be_authenticated_with_a_fake_identity(
    tmp_path: Path,
) -> None:
    """A session minted by the fake provider must not authenticate in production."""
    from server.main import create_app

    staging = build_settings(tmp_path, environment=Environment.STAGING)
    with build_client(staging) as staging_client:
        token = staging_client.post(
            "/api/v1/auth/login", json={"code": "test-parent-1"}
        ).json()["access_token"]

    production = build_settings(
        tmp_path,
        environment=Environment.PRODUCTION,
        database_url="mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )
    app = create_app(production)

    assert "/api/v1/auth/login" not in route_paths(app)
    assert token
