"""Start the collector with credentials from a root-managed JSON file."""
from __future__ import annotations

from pathlib import Path

import typer

from .collector import create_collector


def serve_collector(database: Path, credentials_file: Path, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Collector must bind to loopback behind an HTTPS proxy")
    from .collector_config import load_collector_config
    credentials = load_collector_config(credentials_file)
    import uvicorn
    uvicorn.run(create_collector(database, credentials), host=host, port=port)
