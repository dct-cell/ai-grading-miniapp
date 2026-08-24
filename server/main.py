from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from server.adapters.factories import build_auth_provider, build_payment_gateway
from server.api import (
    admin_auth,
    admin_operations,
    admin_orders,
    admin_refunds,
    admin_workers,
    callbacks,
    miniapp_aftersales,
    miniapp_auth,
    miniapp_downloads,
    miniapp_orders,
    miniapp_payments,
    miniapp_quotes,
    worker_jobs,
    worker_results,
    workers,
)
from server.config import Environment, ServerSettings
from server.db import create_session_factory


FAKE_ADAPTER_ENVIRONMENTS = frozenset(
    {Environment.DEVELOPMENT, Environment.TEST, Environment.STAGING}
)


def create_app(settings: ServerSettings) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    auth_provider = build_auth_provider(settings)
    payment_gateway = build_payment_gateway(settings)
    session_factory = create_session_factory(settings.database_url)
    engine = cast(Engine, session_factory.kw["bind"])

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for adapter in (auth_provider, payment_gateway):
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
            engine.dispose()

    app = FastAPI(
        title="Competition Grader Service",
        version="3.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.auth_provider = auth_provider
    app.state.payment_gateway = payment_gateway

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        with app.state.session_factory() as session:
            session.scalar(text("select 1"))
        probe = settings.data_dir / f".write-probe-{uuid4().hex}"
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            probe.unlink(missing_ok=True)
        return {"database": "ok", "storage": "ok"}

    app.include_router(miniapp_auth.router)
    app.include_router(miniapp_quotes.router)
    app.include_router(miniapp_payments.router)
    app.include_router(miniapp_orders.router)
    app.include_router(miniapp_aftersales.router)
    # Delivering a paid-for result is a real feature in every environment, so
    # this router is outside the fake-adapter gate.
    app.include_router(miniapp_downloads.router)
    # The Worker control plane is a real endpoint in every environment; it
    # authenticates with the shared key, not with a fake adapter.
    app.include_router(workers.router)
    app.include_router(worker_jobs.router)
    app.include_router(worker_results.router)
    app.include_router(callbacks.wechat_router)
    # The Admin console is likewise real in every environment: refund approvals
    # have to work in production. It authenticates with its own credential
    # domain — Argon2id passwords over opaque cookie sessions — not with a fake
    # adapter, so it is outside the gate below.
    app.include_router(admin_auth.router)
    app.include_router(admin_orders.router)
    app.include_router(admin_operations.router)
    app.include_router(admin_refunds.router)
    app.include_router(admin_workers.router)
    # Fake auth and payment adapters must never be reachable in production.
    if (
        settings.environment in FAKE_ADAPTER_ENVIRONMENTS
        and not settings.wechat_live_mode
    ):
        app.include_router(miniapp_payments.fake_router)
        app.include_router(callbacks.router)

    return app
