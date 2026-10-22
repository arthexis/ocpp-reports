"""Forwarder configuration stored in one protected TOML file."""
from __future__ import annotations
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
import typer

DEFAULT_FORWARDER_CONFIG = Path("/etc/ocpp-reports/forwarder.toml")
app = typer.Typer(help="Forwarder configuration.", no_args_is_help=True)


def load_forwarder_config(path: Path = DEFAULT_FORWARDER_CONFIG) -> dict:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    if set(document) != {"forwarder"} or not isinstance(document["forwarder"], dict):
        raise ValueError("Expected [forwarder] table")
    data = document["forwarder"]
    if set(data) != {"source_id", "endpoint", "token"}:
        raise ValueError("Expected source_id, endpoint and token")
    if any(not isinstance(data[key], str) or not data[key].strip() for key in data):
        raise ValueError("Forwarder fields must be nonempty strings")
    from .forwarder import validate_endpoint
    validate_endpoint(data["endpoint"])
    return data


@app.command("config")
def config_command(check: bool = typer.Option(False, "--check"),
                   path: Path = typer.Option(DEFAULT_FORWARDER_CONFIG, "--path", hidden=True)) -> None:
    """Edit TOML with VISUAL/EDITOR/nano and validate without displaying secrets."""
    if not check:
        if not sys.stdin.isatty():
            raise typer.BadParameter("Editing requires an interactive terminal")
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
            typer.echo(f"Cannot launch editor: {exc}", err=True)
            raise typer.Exit(2)
    try:
        data = load_forwarder_config(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"Forwarder configuration invalid: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"Forwarder configuration valid: source={data['source_id']}; token hidden")
