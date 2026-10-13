"""Pure report presentation, independent of CSMS ingestion and delivery."""
from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from jinja2 import Environment, StrictUndefined, select_autoescape
from .validation import validate_summary

TEMPLATES = files("ocpp_reports").joinpath("templates")


def render_report(report: dict[str, Any], format: str = "html") -> str:
    """Render a versioned internal summary; never infer missing measurements."""
    validate_summary(report)
    summary = report["summary"]
    period = report["period"]
    rows = report["transactions"]
    if not isinstance(rows, list) or not isinstance(summary, dict) or not isinstance(period, dict):
        raise ValueError("Malformed report")
    if format == "csv":
        from .csv_rendering import render_csv
        return render_csv(report)
    if format not in ("html", "text"):
        raise ValueError("Format must be html, text or csv")
    template_name = "summary.html.j2" if format == "html" else "summary.txt.j2"
    env = Environment(autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
                      undefined=StrictUndefined, keep_trailing_newline=True)
    template = env.from_string(TEMPLATES.joinpath(template_name).read_text(encoding="utf-8"))
    missing = int(summary["missing_energy_transactions"])
    provisional = summary.get("completeness") != "complete" or missing > 0
    return template.render(
        source_id=report["source_id"],
        period=period,
        summary=summary,
        transactions=rows,
        provisional=provisional,
        revision=report.get("revision", "unversioned"),
        energy_kwh=float(summary["known_energy_wh"]) / 1000,
    )


def render_file(source: str, format: str = "html") -> str:
    from pathlib import Path
    return render_report(json.loads(Path(source).read_text(encoding="utf-8")), format=format)
