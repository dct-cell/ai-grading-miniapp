from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from server.models import MiniappSession, User
from tests.server.conftest import build_client, build_settings


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_login_recovers_when_another_request_created_the_user_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The loser of a first-login race must reuse the winner's account.

    SQLite serialises writers, so the race is simulated at the service level:
    the account already exists by the time the insert runs, exactly as it
    would on MySQL when two requests interleave between lookup and insert.
    """
    import server.services.sessions as sessions_module
    from server.adapters.auth import FakeAuthProvider

    settings = build_settings(tmp_path)

    with build_client(settings) as client:
        factory: sessionmaker[Session] = client.app.state.session_factory

        # The winner commits first, in its own transaction.
        with factory() as winner:
            winner.add(User(openid="fake:test-race", public_id="u-committed"))
            winner.commit()

        # The loser starts from a stale read that saw no account at all.
        original_scalar = Session.scalar
        stale_reads: list[str] = []

        def scalar_hiding_the_winner_once(self, statement, *args, **kwargs):
            result = original_scalar(self, statement, *args, **kwargs)
            if (
                isinstance(result, User)
                and result.openid == "fake:test-race"
                and not stale_reads
            ):
                stale_reads.append(result.id)
                return None
            return result

        monkeypatch.setattr(Session, "scalar", scalar_hiding_the_winner_once)
        with factory() as loser:
            issued = sessions_module.login(
                loser, FakeAuthProvider(), "test-race"
            )
        monkeypatch.setattr(Session, "scalar", original_scalar)

        with factory() as session:
            users = session.scalars(select(User)).all()
            sessions = count(session, MiniappSession)

    assert stale_reads, "the stale read never happened"
    assert len(users) == 1
    assert users[0].public_id == "u-committed"
    assert issued.user.id == users[0].id
    assert sessions == 1
    assert issued.raw_token


def test_repeated_logins_reuse_one_account_and_issue_distinct_tokens(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path)

    with build_client(settings) as client:
        responses = [
            client.post("/api/v1/auth/login", json={"code": "test-repeat"})
            for _ in range(4)
        ]
        factory: sessionmaker[Session] = client.app.state.session_factory
        with factory() as session:
            users = session.scalars(select(User)).all()
            sessions = count(session, MiniappSession)

    assert [response.status_code for response in responses] == [200] * 4
    assert len(users) == 1
    assert sessions == 4
    assert len({response.json()["access_token"] for response in responses}) == 4


def test_public_id_collision_falls_back_to_another_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A duplicate public_id must not surface as a 500."""
    import server.services.sessions as sessions_module

    settings = build_settings(tmp_path)
    generated = iter(["dup", "dup", "unique1", "unique2"])
    monkeypatch.setattr(
        sessions_module.secrets,
        "token_hex",
        lambda _n: next(generated),
    )

    with build_client(settings) as client:
        first = client.post("/api/v1/auth/login", json={"code": "test-a"})
        second = client.post("/api/v1/auth/login", json={"code": "test-b"})
        factory: sessionmaker[Session] = client.app.state.session_factory
        with factory() as session:
            public_ids = {user.public_id for user in session.scalars(select(User)).all()}

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(public_ids) == 2
