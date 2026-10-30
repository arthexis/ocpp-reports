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


def _transport_failure(exc: Exception) -> str:
    """Classify verification failures without printing URLs or secrets."""
    import ssl
    import httpx

    cause = exc
    while cause is not None:
        if isinstance(cause, (ssl.SSLError, ssl.CertificateError)):
            return "collector_tls_failure"
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc, httpx.ConnectError) and any(
        term in str(exc).lower() for term in ("certificate verify", "ssl", "tls")
    ):
        return "collector_tls_failure"
    return "collector_unreachable"


def check_collector(config: dict) -> dict:
    """Authenticated no-write readiness; return only a sanitized result."""
    import httpx
    from .forwarder import validate_endpoint

    source = config["source_id"]
    result = {"ready": False, "status": "blocked", "reason": "collector_unreachable",
              "source_id": source}
    try:
        validate_endpoint(config["endpoint"])
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(
                config["endpoint"].rstrip("/") + "/ready",
                json={"source_id": source},
                headers={"Authorization": "Bearer " + config["token"]},
            )
    except httpx.HTTPError as exc:
        result["reason"] = _transport_failure(exc)
        return result
    except ValueError:
        result["reason"] = "invalid_configuration"
        return result

    if response.status_code != 200:
        result["reason"] = {
            401: "collector_unauthorized", 403: "collector_unauthorized",
            404: "collector_unsupported", 405: "collector_unsupported",
            503: "collector_not_ready",
        }.get(response.status_code, "collector_http_error")
        return result
    try:
        payload = response.json()
    except ValueError:
        result["reason"] = "collector_invalid_response"
        return result
    if not isinstance(payload, dict) or payload.get("schema") != "ocpp-reports/readiness/v1":
        result["reason"] = "collector_unsupported"
        return result
    if (payload.get("source_id") != source or payload.get("ready") is not True
            or payload.get("held") is not False
            or type(payload.get("cursor")) is not int or payload["cursor"] < 0):
        result["reason"] = "collector_invalid_response"
        return result
    return {"ready": True, "status": "ready", "reason": "ready",
            "source_id": source, "collector_cursor": payload["cursor"],
            "tls_verified": True}


@app.command("check")
def remote_check(
    remote: bool = typer.Option(False, "--remote", help="Check live collector credentials without forwarding"),
    path: Path = typer.Option(DEFAULT_FORWARDER_CONFIG, "--path", hidden=True),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify running collector authentication without forwarding or writes."""
    import json

    result = {"ready": False, "status": "blocked", "reason": "invalid_configuration"}
    if not remote:
        result["reason"] = "remote_required"
    else:
        try:
            config = load_forwarder_config(path)
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            result["reason"] = "invalid_configuration"
        else:
            result = check_collector(config)
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    elif result["ready"]:
        typer.echo(f"Collector ready: {result['source_id']}; cursor={result['collector_cursor']}")
    else:
        typer.echo(f"Collector readiness blocked: {result['reason']}", err=True)
    if not result["ready"]:
        raise typer.Exit(2)
