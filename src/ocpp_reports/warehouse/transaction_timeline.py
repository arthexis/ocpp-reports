"""Bounded, source-scoped, read-only samples for a transaction timeline (#215).

Only immutable locally retained export samples can form a timeline. Collector
latest-snapshot records do not establish a historical series.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _when(raw):
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _quantity(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def transaction_timeline(database: str | Path, transaction: dict, *, scan_limit: int = 2048,
                         point_limit: int = 120) -> dict:
    """Use indexed source/event order, never an unbounded event-history scan.

    The result is intentionally incomplete when source history exceeds scan_limit.
    No line/point is synthesized for missing observations or remote-only replicas.
    """
    empty = {"points": [], "limited": False, "source": "local-energy-samples"}
    source, charger = transaction.get("source_id"), transaction.get("charger_id")
    connector, tx_id = transaction.get("connector_id"), transaction.get("id")
    start = _when(transaction.get("started_at"))
    stop = _when(transaction.get("stopped_at")) if transaction.get("stopped_at") else None
    if (not isinstance(source, str) or not isinstance(charger, str)
            or type(connector) is not int or connector <= 0
            or type(tx_id) is not int or start is None
            or transaction.get("stopped_at") and stop is None):
        return empty
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='energy_samples'").fetchone()
        if not exists:
            return empty
        # The (source_id,event_id,sample_index) PRIMARY KEY is the access path.
        rows = db.execute("SELECT payload FROM energy_samples WHERE source_id=? "
                          "ORDER BY event_id DESC,sample_index DESC LIMIT ?",
                          (source, scan_limit + 1)).fetchall()
    limited = len(rows) > scan_limit
    baseline = _quantity(transaction.get("meter_start_wh"))
    points = {}
    for (payload,) in rows[:scan_limit]:
        try:
            sample = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(sample, dict):
            continue
        if (sample.get("charger_id") != charger or
                type(sample.get("connector_id")) is not int or
                sample["connector_id"] != connector or
                type(sample.get("transaction_id")) is not int or
                sample["transaction_id"] != tx_id):
            continue
        at = _when(sample.get("at"))
        if at is None or at < start or (stop is not None and at > stop):
            continue
        energy = _quantity(sample.get("energy_wh"))
        power = _quantity(sample.get("power_w"))
        soc = _quantity(sample.get("soc"))
        delta = (energy - baseline) / 1000 if baseline is not None and energy is not None and energy >= baseline else None
        if delta is None and power is None and (soc is None or soc > 100):
            continue
        key = (at.isoformat(), sample.get("source_event_id"), sample.get("sample_index"))
        points[key] = {"at": at.isoformat(), "energy_kwh": delta,
                       "power_kw": power / 1000 if power is not None else None,
                       "soc": soc if soc is not None and soc <= 100 else None}
    ordered = [points[k] for k in sorted(points)]
    if len(ordered) > point_limit:
        # Fixed-size deterministic selection of actual points, including endpoints.
        indices = [(i * (len(ordered) - 1)) // (point_limit - 1) for i in range(point_limit)]
        ordered = [ordered[i] for i in indices]
        limited = True
    return {"points": ordered, "limited": limited, "source": "local-energy-samples"}


def timeline_charts(points: list[dict]) -> list[dict]:
    """Server-renderable SVG coordinates; retain only observed values."""
    charts = []
    for field, label, unit in (("energy_kwh", "Energía de sesión medida", "kWh"),
                                ("power_kw", "Potencia medida", "kW"),
                                ("soc", "Estado de carga medido", "%")):
        rows = [(datetime.fromisoformat(p["at"]).timestamp(), p[field])
                for p in points if p.get(field) is not None]
        if not rows:
            continue
        low_t, high_t = rows[0][0], rows[-1][0]
        high_y = max(v for _, v in rows)
        low_y = min(v for _, v in rows)
        # Flat or single-point series is shown as a dot, not an invented trend.
        dots = [{"x": round(30 + (t-low_t) * 540 / (high_t-low_t), 2)
                          if high_t > low_t else 300,
                 "y": round(155 - (v-low_y) * 125 / (high_y-low_y), 2)
                          if high_y > low_y else 92,
                 "value": v} for t, v in rows]
        charts.append({"field": field, "label": label, "unit": unit,
                       "dots": dots, "min": low_y, "max": high_y})
    return charts
