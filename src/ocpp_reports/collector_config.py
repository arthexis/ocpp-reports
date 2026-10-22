"""Collector TOML configuration and safe editor workflow."""
from __future__ import annotations
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
import typer

DEFAULT_COLLECTOR_CONFIG = Path("/etc/ocpp-reports/collector.toml")
app = typer.Typer(help="Collector configuration.", no_args_is_help=True)


def load_collector_config(path: Path = DEFAULT_COLLECTOR_CONFIG) -> dict[str, str]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if set(data) != {"sources"} or not isinstance(data["sources"], dict):
        raise ValueError("Expected a [sources] table containing source ID = token entries")
    credentials = data["sources"]
    if not credentials or any(not isinstance(k, str) or not k.strip()
                              or not isinstance(v, str) or not v.strip()
                              for k, v in credentials.items()):
        raise ValueError("Collector requires nonempty source IDs and tokens")
    return credentials


@app.command("config")
def config_command(
    check: bool = typer.Option(False, "--check", help="Validate without editing"),
    path: Path = typer.Option(DEFAULT_COLLECTOR_CONFIG, "--path", hidden=True),
) -> None:
    """Edit collector TOML using VISUAL/EDITOR or nano; validate afterwards."""
    if not check:
        if not sys.stdin.isatty():
            typer.echo("Editing requires an interactive terminal", err=True)
            raise typer.Exit(2)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        try:
            argv = shlex.split(editor)
            if not argv:
                raise ValueError("Empty editor")
            command = argv + [str(path)] if os.access(path, os.W_OK) else ["sudo", "-e", str(path)]
            env = dict(os.environ)
            if command[0] == "sudo":
                env["SUDO_EDITOR"] = editor
            result = subprocess.run(command, env=env, check=False)
            if result.returncode:
                raise typer.Exit(result.returncode)
        except (OSError, ValueError) as exc:
            typer.echo(f"Cannot open editor: {exc}", err=True)
            raise typer.Exit(2)
    try:
        credentials = load_collector_config(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"Collector configuration invalid: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"Collector configuration valid: {len(credentials)} source(s); tokens hidden")
