"""Admin authentication: Argon2id passwords over opaque server-side sessions.

Phase 05 shipped a deliberately minimal seam — a static shared key plus an
``X-Admin-ID`` header — and recorded the intent to replace it here. Phase 07
replaces it outright: there is no longer any code path that authenticates an
admin with a shared key, so the blast radius of a leaked
``GRADER_ADMIN_SHARED_KEY`` is now zero.

The properties these tests pin down:

* the raw session token is returned once, in an HttpOnly cookie, and only its
  SHA-256 is ever stored;
* the cookie is scoped to ``/admin`` with ``SameSite=Strict``, and gains
  ``Secure`` outside development;
* every state-changing request needs a CSRF token *and* a matching Origin;
* five failed logins for one username/IP pair earn a 429, and a login never
  reveals whether a username exists;
* the three credential domains stay mutually unintelligible, in both
  directions.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from server.config import Environment
from server.models import AdminSession, AdminUser
from tests.server.conftest import (
    ADMIN_PASSWORD,
    SHARED_KEY,
    admin_login,
    build_client,
    build_settings,
    create_admin,
)


COOKIE_NAME = "grader_admin_session"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return build_client(build_settings(tmp_path))


@pytest.fixture()
def session_factory(client: TestClient) -> sessionmaker[Session]:
    return client.app.state.session_factory


def _set_cookie_header(response) -> str:
    return response.headers["set-cookie"]


def _cookie_attributes(response) -> dict[str, str]:
    """Parse the Set-Cookie attributes for the admin session cookie."""
    jar = SimpleCookie()
    jar.load(_set_cookie_header(response))
    morsel = jar[COOKIE_NAME]
    return {key: value for key, value in morsel.items() if value != ""}


class TestLoginIssuesASecureSession:
    def test_login_sets_an_httponly_strict_admin_scoped_cookie(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        admin_id = create_admin(session_factory)

        response = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "phase07-admin", "password": ADMIN_PASSWORD},
        )

        assert response.status_code == 204
        raw_cookie = client.cookies[COOKIE_NAME]
        assert raw_cookie
        header = _set_cookie_header(response)
        assert "HttpOnly" in header
        # Attribute names are case-insensitive per RFC 6265, so compare the
        # parsed value rather than the raw casing Starlette happens to emit.
        attributes = _cookie_attributes(response)
        assert attributes["samesite"].lower() == "strict"
        assert attributes["path"] == "/admin"
        # Development and tests must not set Secure: a browser refuses a Secure
        # cookie over plain http, which would make local development impossible.
        assert "secure" not in {key.lower() for key in attributes}
        assert admin_id

    def test_staging_marks_the_cookie_secure(self, tmp_path: Path) -> None:
        """A deployed environment must pin the cookie to TLS.

        Staging is the case that can be exercised end to end: production would
        additionally require a real MySQL URL, so the Secure gate itself is
        asserted directly in ``test_production_would_also_mark_the_cookie_secure``.
        """
        client = build_client(
            build_settings(tmp_path, environment=Environment.STAGING)
        )
        create_admin(client.app.state.session_factory)

        response = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "phase07-admin", "password": ADMIN_PASSWORD},
        )

        assert response.status_code == 204
        attributes = _cookie_attributes(response)
        assert "secure" in {key.lower() for key in attributes}
        assert attributes["samesite"].lower() == "strict"

    def test_production_would_also_mark_the_cookie_secure(self) -> None:
        """Production must never be in the plain-http exemption set."""
        from server.api.admin_auth import _INSECURE_TRANSPORT_ENVIRONMENTS

        assert Environment.PRODUCTION not in _INSECURE_TRANSPORT_ENVIRONMENTS
        assert Environment.STAGING not in _INSECURE_TRANSPORT_ENVIRONMENTS

    def test_only_the_hash_of_the_session_token_is_stored(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)

        client.post(
            "/admin/api/v1/auth/login",
            json={"username": "phase07-admin", "password": ADMIN_PASSWORD},
        )

        raw_token = client.cookies[COOKIE_NAME]
        with session_factory() as session:
            records = session.scalars(select(AdminSession)).all()
            assert len(records) == 1
            record = records[0]
            assert record.token_hash == hashlib.sha256(
                raw_token.encode("utf-8")
            ).hexdigest()
            assert raw_token not in record.token_hash
            # A stored raw token would let a database reader impersonate an
            # admin, so the raw value must appear nowhere on the row.
            assert raw_token not in repr(
                {
                    column.name: getattr(record, column.name)
                    for column in AdminSession.__table__.columns
                }
            )

    def test_login_rotates_the_session_token(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A second login must not reuse the first token."""
        create_admin(session_factory)
        credentials = {"username": "phase07-admin", "password": ADMIN_PASSWORD}

        client.post("/admin/api/v1/auth/login", json=credentials)
        first_token = client.cookies[COOKIE_NAME]
        client.post("/admin/api/v1/auth/login", json=credentials)
        second_token = client.cookies[COOKIE_NAME]

        assert first_token != second_token
        with session_factory() as session:
            first = session.scalar(
                select(AdminSession).where(
                    AdminSession.token_hash
                    == hashlib.sha256(first_token.encode("utf-8")).hexdigest()
                )
            )
            assert first is not None
            assert first.revoked_at is not None, "the old session must be revoked"

    def test_session_endpoint_reports_the_admin_and_a_csrf_token(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        body = client.get("/admin/api/v1/auth/session").json()

        assert body["username"] == "phase07-admin"
        assert body["csrf_token"]
        assert "password_hash" not in body
        assert "token_hash" not in body

    def test_logout_revokes_the_session(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        csrf = admin_login(client)

        response = client.post(
            "/admin/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf, "Origin": "http://localhost:5173"},
        )

        assert response.status_code == 204
        assert client.get("/admin/api/v1/auth/session").status_code == 401


class TestPasswordHandling:
    def test_passwords_are_stored_as_argon2id(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)

        with session_factory() as session:
            admin = session.scalar(select(AdminUser))
            assert admin is not None
            assert admin.password_hash.startswith("$argon2id$")
            assert ADMIN_PASSWORD not in admin.password_hash

    def test_a_wrong_password_is_rejected(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)

        response = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "phase07-admin", "password": "not the password"},
        )

        assert response.status_code == 401
        assert COOKIE_NAME not in client.cookies

    def test_login_does_not_reveal_whether_a_username_exists(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """An unknown username and a wrong password must be indistinguishable."""
        create_admin(session_factory)

        unknown = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "no-such-admin", "password": ADMIN_PASSWORD},
        )
        wrong_password = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "phase07-admin", "password": "wrong"},
        )

        assert unknown.status_code == wrong_password.status_code == 401
        assert unknown.json() == wrong_password.json()

    def test_an_unknown_username_still_pays_the_hashing_cost(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Timing must not distinguish an unknown user from a wrong password.

        Returning early when the username is unknown would skip the Argon2id
        verification and answer in a fraction of the time, which is a usable
        oracle for enumerating admin accounts. Asserting on wall-clock time
        would be flaky, so this asserts on the mechanism instead: a verify
        must happen either way.
        """
        create_admin(session_factory)
        calls: list[str] = []
        import server.services.admin_sessions as module

        original = module._HASHER

        class CountingHasher:
            """Wraps the real hasher so the cost is still genuinely paid."""

            def verify(self, stored: str, password: str) -> bool:
                calls.append(stored)
                return original.verify(stored, password)

            def hash(self, password: str) -> str:
                return original.hash(password)

        monkeypatched = pytest.MonkeyPatch()
        monkeypatched.setattr(module, "_HASHER", CountingHasher())
        try:
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "no-such-admin", "password": ADMIN_PASSWORD},
            )
            unknown_user_verifies = len(calls)
            calls.clear()
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "phase07-admin", "password": "wrong"},
            )
            wrong_password_verifies = len(calls)
        finally:
            monkeypatched.undo()

        assert unknown_user_verifies == wrong_password_verifies == 1

    def test_a_success_from_one_address_does_not_clear_another_addresss_count(
        self,
    ) -> None:
        """The reset must be per (username, address), not per username.

        Otherwise an attacker who has burned their five attempts gets a fresh
        five every time the real admin logs in from anywhere else — and a real
        admin logs in several times a day, so this multiplies the online
        guessing rate instead of capping it.
        """
        from server.services.admin_sessions import (
            MAX_FAILED_LOGINS,
            LoginRateLimiter,
            RateLimited,
        )

        limiter = LoginRateLimiter()
        attacker = "203.0.113.9"
        office = "198.51.100.4"
        for _ in range(MAX_FAILED_LOGINS):
            limiter.record_failure("ops-admin", attacker)

        # The real admin signs in successfully from a different address.
        limiter.reset("ops-admin", office)

        with pytest.raises(RateLimited):
            limiter.check("ops-admin", attacker)

    def test_a_csrf_token_with_non_ascii_bytes_is_refused_not_a_crash(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A forged header must reach the 403, not raise out of the guard.

        ``hmac.compare_digest`` rejects non-ASCII ``str`` inputs with TypeError,
        and header values are attacker-controlled, so comparing raw strings turns
        a security refusal into an unhandled 500. The comparison must therefore
        hash first, exactly like the session-token path.
        """
        from server.services.admin_sessions import csrf_token_matches

        create_admin(session_factory)
        admin_login(client)

        # Exercised at the guard's own boundary: the ASGI test client refuses to
        # transmit such a header at all, so a request-level test would silently
        # prove nothing.
        assert (
            csrf_token_matches(
                "\xff\xfe",
                "a" * 64,
                session_secret="s" * 32,
            )
            is False
        )

    def test_a_disabled_admin_cannot_log_in(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(
            session_factory,
            username="disabled-admin",
            disabled_at=datetime.now(timezone.utc),
        )

        response = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "disabled-admin", "password": ADMIN_PASSWORD},
        )

        assert response.status_code in {401, 403}
        assert COOKIE_NAME not in client.cookies

    def test_disabling_an_admin_invalidates_the_live_session(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        admin_id = create_admin(session_factory)
        admin_login(client)
        assert client.get("/admin/api/v1/auth/session").status_code == 200

        with session_factory() as session:
            admin = session.get(AdminUser, admin_id)
            assert admin is not None
            admin.disabled_at = datetime.now(timezone.utc)
            session.commit()

        assert client.get("/admin/api/v1/auth/session").status_code in {401, 403}


class TestSessionLifetime:
    def test_an_expired_session_is_rejected(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        with session_factory() as session:
            record = session.scalar(select(AdminSession))
            assert record is not None
            record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()

        assert client.get("/admin/api/v1/auth/session").status_code == 401

    def test_a_revoked_session_is_rejected(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        with session_factory() as session:
            record = session.scalar(select(AdminSession))
            assert record is not None
            record.revoked_at = datetime.now(timezone.utc)
            session.commit()

        assert client.get("/admin/api/v1/auth/session").status_code == 401

    def test_using_a_session_updates_last_seen_at(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        with session_factory() as session:
            record = session.scalar(select(AdminSession))
            assert record is not None
            original = record.last_seen_at
            record.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=30)
            session.commit()

        client.get("/admin/api/v1/auth/session")

        with session_factory() as session:
            record = session.scalar(select(AdminSession))
            assert record is not None
            assert record.last_seen_at > datetime.now(timezone.utc) - timedelta(
                minutes=5
            )
            assert original is not None

    def test_an_unknown_cookie_value_is_rejected(self, client: TestClient) -> None:
        client.cookies.set(COOKIE_NAME, "not-a-real-token", path="/admin")

        assert client.get("/admin/api/v1/auth/session").status_code == 401


class TestCsrfProtection:
    def test_a_mutation_without_a_csrf_token_is_refused(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        response = client.post(
            "/admin/api/v1/auth/logout",
            headers={"Origin": "http://localhost:5173"},
        )

        assert response.status_code == 403
        # The session must survive a refused mutation.
        assert client.get("/admin/api/v1/auth/session").status_code == 200

    def test_a_mutation_with_a_wrong_csrf_token_is_refused(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        response = client.post(
            "/admin/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": "forged-token",
                "Origin": "http://localhost:5173",
            },
        )

        assert response.status_code == 403

    def test_a_mutation_from_a_foreign_origin_is_refused(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        csrf = admin_login(client)

        response = client.post(
            "/admin/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example.com"},
        )

        assert response.status_code == 403

    def test_reads_do_not_require_a_csrf_token(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        assert client.get("/admin/api/v1/auth/session").status_code == 200

    def test_the_csrf_token_is_not_the_session_token(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Leaking the CSRF token must not leak the session credential."""
        create_admin(session_factory)
        csrf = admin_login(client)

        assert csrf != client.cookies[COOKIE_NAME]


class TestLoginRateLimit:
    def test_five_failures_for_one_username_and_ip_earn_a_429(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        payload = {"username": "phase07-admin", "password": "wrong"}

        for _ in range(5):
            assert (
                client.post("/admin/api/v1/auth/login", json=payload).status_code == 401
            )

        throttled = client.post("/admin/api/v1/auth/login", json=payload)
        assert throttled.status_code == 429
        # Even the correct password must be refused while throttled, otherwise
        # the limit does not actually slow an online guessing attack down.
        correct = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "phase07-admin", "password": ADMIN_PASSWORD},
        )
        assert correct.status_code == 429

    def test_the_limit_does_not_reveal_whether_the_username_exists(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        for _ in range(5):
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "no-such-admin", "password": "wrong"},
            )

        throttled = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "no-such-admin", "password": "wrong"},
        )

        assert throttled.status_code == 429

    def test_a_successful_login_resets_the_counter(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        for _ in range(4):
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "phase07-admin", "password": "wrong"},
            )

        assert (
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "phase07-admin", "password": ADMIN_PASSWORD},
            ).status_code
            == 204
        )

        # Four fresh failures must be allowed again, proving the reset.
        for _ in range(4):
            assert (
                client.post(
                    "/admin/api/v1/auth/login",
                    json={"username": "phase07-admin", "password": "wrong"},
                ).status_code
                == 401
            )

    def test_a_different_username_from_the_same_ip_is_not_throttled(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """The limit is per username+IP, so one admin cannot lock out another."""
        create_admin(session_factory)
        create_admin(session_factory, username="second-admin")
        for _ in range(5):
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "phase07-admin", "password": "wrong"},
            )

        response = client.post(
            "/admin/api/v1/auth/login",
            json={"username": "second-admin", "password": ADMIN_PASSWORD},
        )

        assert response.status_code == 204


class TestCredentialDomainIsolation:
    """Three domains, mutually unintelligible, verified in both directions."""

    def test_a_miniapp_token_cannot_call_the_admin_api(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        from tests.server.conftest import login

        body = login(client)

        response = client.get(
            "/admin/api/v1/auth/session",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )

        assert response.status_code == 401

    def test_the_worker_shared_key_cannot_call_the_admin_api(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)

        response = client.get(
            "/admin/api/v1/auth/session",
            headers={
                "Authorization": f"Bearer {SHARED_KEY}",
                "X-Worker-ID": "worker-1",
            },
        )

        assert response.status_code == 401

    def test_the_retired_admin_shared_key_no_longer_authenticates(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Phase 07 removes the shared-key path outright, not just in tests."""
        from tests.server.conftest import ADMIN_SHARED_KEY

        admin_id = create_admin(session_factory)

        response = client.get(
            "/admin/api/v1/auth/session",
            headers={
                "Authorization": f"Bearer {ADMIN_SHARED_KEY}",
                "X-Admin-ID": admin_id,
            },
        )

        assert response.status_code == 401

    def test_an_admin_cookie_cannot_call_the_miniapp_api(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        assert client.get("/api/v1/me").status_code == 401

    def test_an_admin_cookie_cannot_call_the_worker_api(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        create_admin(session_factory)
        admin_login(client)

        response = client.post("/worker/v1/jobs/lease", json={})

        assert response.status_code == 401


class TestRouteRegistration:
    def test_admin_auth_is_registered_in_production(self, tmp_path: Path) -> None:
        """Admin routes are real endpoints, not fake adapters."""
        settings = build_settings(
            tmp_path,
            environment=Environment.PRODUCTION,
            database_url="mysql+pymysql://grader:pw@localhost/grader",
        )
        from server.main import create_app

        app = create_app(settings)
        # Introspect the OpenAPI schema rather than app.routes: this FastAPI
        # version wraps included routers, so the mounted paths are not visible
        # as attributes on the top-level route objects.
        paths = app.openapi()["paths"]

        assert "/admin/api/v1/auth/login" in paths
        assert "/admin/api/v1/auth/session" in paths
        assert "/admin/api/v1/auth/logout" in paths
        # The refund routes retired their shared key but stay registered.
        assert "/admin/api/v1/refunds/{refund_id}/approve" in paths
        # Meanwhile the fake adapters must still be absent in production.
        assert not [path for path in paths if "simulate-success" in path]
        assert not [path for path in paths if path.startswith("/callbacks/")]

    def test_no_error_response_leaks_a_secret(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        from tests.server.conftest import ADMIN_SHARED_KEY

        create_admin(session_factory)
        responses = [
            client.post(
                "/admin/api/v1/auth/login",
                json={"username": "phase07-admin", "password": "wrong"},
            ),
            client.get("/admin/api/v1/auth/session"),
        ]

        for response in responses:
            body = response.text
            assert ADMIN_PASSWORD not in body
            assert ADMIN_SHARED_KEY not in body
            assert SHARED_KEY not in body
            assert "sqlite" not in body
