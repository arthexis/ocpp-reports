"""Protected, explicit multsource relay configuration and one-shot CLI."""
from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import typer

from .relay import relay_once

app = typer.Typer(help="Collector-to-collector relay; disabled unless configured.", no_args_is_help=True)
DEFAULT_CONFIG = Path("/etc/ocpp-reports/relay.toml")


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o137:
        raise ValueError("Relay configuration must be a protected regular file (0640 or stricter)")
    with path.open("rb") as f:
        document = tomllib.load(f)
    if set(document) != {"relay"} or set(document["relay"]) != {"destination_id", "endpoint", "sources"}:
        raise ValueError("Expected [relay] destination_id, endpoint and sources")
    cfg = document["relay"]
    from .replication.forwarder import validate_endpoint
    validate_endpoint(cfg["endpoint"])
    if not cfg["endpoint"].startswith("https://"):
        raise ValueError("Relay requires HTTPS even over WireGuard")
    if not isinstance(cfg["destination_id"], str) or not cfg["destination_id"]:
        raise ValueError("Missing destination identity")
    if not isinstance(cfg["sources"], dict) or not cfg["sources"]:
        raise ValueError("Relay requires explicit per-source bearer tokens")
    if any(not isinstance(source, str) or not source or
           not isinstance(token, str) or not token or
           source == cfg["destination_id"] for source, token in cfg["sources"].items()):
        raise ValueError("Invalid source authorization or self-replication")
    return cfg


@app.command("check")
def check(path: Path = typer.Option(DEFAULT_CONFIG, "--config")) -> None:
    try:
        cfg = load_config(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"Relay configuration invalid: {type(exc).__name__}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps({"valid": True, "destination": cfg["destination_id"],
                           "sources": sorted(cfg["sources"])}))


@app.command("once")
def once(
    database: Path = typer.Option(..., "--database"),
    state: Path = typer.Option(..., "--state"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config"),
) -> None:
    """One bounded pass over explicitly authorized sources, without printing secrets."""
    import fcntl
    import httpx

    try:
        cfg = load_config(config)
        # Lock across the network call and durable cursor update, not just SQL writes.
        lock_path = state.with_name(state.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Relay already running") from exc
            result = {}
            for source, token in sorted(cfg["sources"].items()):
                result[source] = relay_once(database, state, source, cfg["endpoint"],
                                            token, destination_id=cfg["destination_id"])
        typer.echo(json.dumps({"acknowledged": result}, sort_keys=True))
    except (OSError, ValueError, httpx.HTTPError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"Relay refused: {type(exc).__name__}", err=True)
        raise typer.Exit(2) from exc
