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


@app.command("check")
def remote_check(
    remote: bool = typer.Option(False, "--remote", help="Check live collector credentials without forwarding"),
    path: Path = typer.Option(DEFAULT_FORWARDER_CONFIG, "--path", hidden=True),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify active collector authentication and source readiness without writes."""
    import json
    import httpx
    from .forwarder import validate_endpoint
    if not remote:
        raise typer.BadParameter("Use --remote to check the live collector")
    try:
        config = load_forwarder_config(path)
        endpoint = config["endpoint"].rstrip("/") + "/ready"
        validate_endpoint(config["endpoint"])
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(
                endpoint, json={"source_id": config["source_id"]},
                headers={"Authorization": "Bearer " + config["token"]},
            )
        if response.status_code != 200:
            reasons = {401: "credentials_rejected", 403: "forbidden",
                       404: "unsupported", 405: "unsupported", 503: "source_held"}
            raise ValueError(reasons.get(response.status_code, "collector_http_error")
                             + " (HTTP " + str(response.status_code) + ")")
        body = response.json()
        if (body.get("schema") != "ocpp-reports/readiness/v1"
                or body.get("source_id") != config["source_id"]
                or body.get("ready") is not True or body.get("held") is not False
                or type(body.get("cursor")) is not int or body["cursor"] < 0):
            raise ValueError("Invalid collector readiness response")
        result = {"ready": True, "source_id": config["source_id"],
                  "collector_cursor": body["cursor"], "tls_verified": True}
        typer.echo(json.dumps(result, sort_keys=True) if json_output
                   else f"Collector ready: {config['source_id']}; cursor={body['cursor']}")
    except (OSError, ValueError, httpx.HTTPError) as exc:
        # Do not render HTTP response bodies or credential/config values.
        if isinstance(exc, httpx.HTTPError):
            reason = "collector_transport_or_tls_failure"
        elif isinstance(exc, OSError):
            reason = "configuration_unreadable"
        else:
            reason = str(exc)
        typer.echo(f"Collector readiness blocked: {reason}", err=True)
        raise typer.Exit(2)

