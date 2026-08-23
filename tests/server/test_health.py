from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from server.config import Environment, ServerSettings
from server.main import create_app


def test_liveness_and_readiness(tmp_path) -> None:
    settings = ServerSettings(
        environment=Environment.TEST,
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path,
        session_secret="s" * 32,
        worker_shared_key="w" * 32,
        admin_shared_key="a" * 32,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {
            "database": "ok",
            "storage": "ok",
        }


def test_app_disposes_database_engine_on_shutdown(tmp_path, monkeypatch) -> None:
    settings = ServerSettings(
        environment=Environment.TEST,
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path,
        session_secret="s" * 32,
        worker_shared_key="w" * 32,
        admin_shared_key="a" * 32,
    )
    app = create_app(settings)
    engine = app.state.session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    original_pool = engine.pool
    original_dispose = engine.dispose
    dispose_calls = 0

    def dispose() -> None:
        nonlocal dispose_calls
        dispose_calls += 1
        original_dispose()

    monkeypatch.setattr(engine, "dispose", dispose)

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200

    assert dispose_calls == 1
    assert engine.pool is not original_pool


def test_readiness_is_safe_for_concurrent_requests(tmp_path, monkeypatch) -> None:
    settings = ServerSettings(
        environment=Environment.TEST,
        database_url=f"sqlite+pysqlite:///{tmp_path}/health.sqlite3",
        data_dir=tmp_path / "data",
        session_secret="s" * 32,
        worker_shared_key="w" * 32,
        admin_shared_key="a" * 32,
    )
    removals_ready = Barrier(2)
    first_removal_completed = Event()
    recorded_probe_paths: list[Path] = []
    recorded_probe_paths_lock = Lock()
    original_unlink = Path.unlink

    def synchronized_unlink(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        if path.name.startswith(".write-probe"):
            with recorded_probe_paths_lock:
                recorded_probe_paths.append(path)
            removal_order = removals_ready.wait(timeout=5)
            if removal_order == 0:
                try:
                    original_unlink(path, missing_ok=missing_ok)
                finally:
                    first_removal_completed.set()
                return
            first_removal_completed.wait(timeout=5)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", synchronized_unlink)

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(lambda _: client.get("/health/ready"), range(2))
            )

    assert len(recorded_probe_paths) == 2
    assert len(set(recorded_probe_paths)) == 2
    assert [response.status_code for response in responses] == [200, 200]
    assert list(settings.data_dir.iterdir()) == []
