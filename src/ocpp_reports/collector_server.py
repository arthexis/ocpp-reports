"""Start the collector with credentials from a root-managed JSON file."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from .collector import create_collector


def serve_collector(database: Path, credentials_file: Path, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Collector must bind to loopback behind an HTTPS proxy")
    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    if not isinstance(credentials, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in credentials.items()
    ):
        raise ValueError("Collector credentials must be a JSON object mapping source IDs to tokens")
    import uvicorn
    uvicorn.run(create_collector(database, credentials), host=host, port=port)
