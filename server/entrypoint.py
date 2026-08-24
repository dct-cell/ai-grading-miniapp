"""Uvicorn entrypoint that validates the complete runtime environment."""

from server.config import ServerSettings
from server.main import create_app


app = create_app(ServerSettings())
