"""Fail-closed, bounded, read-only timelines of immutable local OCPP samples.

The existing transaction_observations PK identifies the event cursor span of
the requested transaction; energy_samples' PK indexes (source,event,sample).
Collector-only latest projections intentionally have no historical time series.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _quantity(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def transaction_timeline(database: str | Path, transaction: dict, *, scan_limit: int = 2048,
                         point_limit: int = 120) -> dict:
    """Fetch no more than scan_limit+1 indexed samples in a proven event window.

    A missing local cursor window is *not* permission to scan the whole source.
    Window existence alone does not certify complete historical coverage.
    """
    empty = {"points": [], "limited": False, "discontinuous": False,
             "source": "local-energy-samples", "coverage": "unavailable"}
    source, charger = transaction.get("source_id"), transaction.get("charger_id")
    connector, tx_id = transaction.get("connector_id"), transaction.get("id")
    start = _when(transaction.get("started_at"))
    stop = _when(transaction.get("stopped_at")) if transaction.get("stopped_at") else None
    if (not isinstance(source, str) or not source or not isinstance(charger, str) or not charger
            or type(connector) is not int or connector <= 0
            or type(tx_id) is not int or start is None
            or transaction.get("stopped_at") and stop is None
            or stop is not None and stop < start
            or scan_limit < 1 or point_limit < 2):
        return empty
    with sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"energy_samples", "transaction_observations"} <= tables:
            return empty
        # Existing composite PK: (source,charger,transaction,page_after,...).
        bounds = db.execute(
            "SELECT MIN(page_after),MAX(page_next) FROM transaction_observations "
            "WHERE source_id=? AND charger_id=? AND transaction_id=?",
            (source, charger, tx_id),
        ).fetchone()
        if not bounds or bounds[0] is None or bounds[1] is None or (
                type(bounds[0]) is not int or type(bounds[1]) is not int
                or bounds[0] < 0 or bounds[1] <= bounds[0]):
            return empty
        # Indexed PK range on (source,event_id,sample_index); the limit applies
        # *within the transaction's observed event interval*, not to a source.
        rows = db.execute(
            "SELECT event_id,sample_index,payload FROM energy_samples "
            "WHERE source_id=? AND event_id>? AND event_id<=? "
            "ORDER BY event_id DESC,sample_index DESC LIMIT ?",
            (source, bounds[0], bounds[1], scan_limit + 1),
        ).fetchall()
    limited = len(rows) > scan_limit
    baseline = _quantity(transaction.get("meter_start_wh"))
    now = datetime.now(timezone.utc)
    samples = {}
    # Preserve physical cursor order before time sorting or point selection.\n    # A rejected/corrupt sample occupies a slot, so gaps cannot be joined.\n    for source_order, (event_id, sample_index, payload) in enumerate(reversed(rows[:scan_limit])):
        try:
            item = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict) or (
                item.get("source_event_id") != event_id or
                item.get("sample_index") != sample_index or
                item.get("charger_id") != charger or
                type(item.get("connector_id")) is not int or
                item["connector_id"] != connector or
                type(item.get("transaction_id")) is not int or
                item["transaction_id"] != tx_id):
            continue
        at = _when(item.get("at"))
        if at is None or at < start or at > now or (stop is not None and at > stop):
            continue
        meter = _quantity(item.get("energy_wh"))
        watts = _quantity(item.get("power_w"))
        # The current CSMS export/v1 contract has no verified SoC series field.
        # Only an explicitly labeled OCPP MeterValues source may opt in.
        soc = (_quantity(item.get("soc"))
               if item.get("soc_provenance") == "ocpp-meter-values" else None)
        if soc is not None and soc > 100:
            soc = None
        if meter is None and watts is None and soc is None:
            continue
        key = (at.isoformat(), event_id, sample_index)
        samples[key] = {"at": at.isoformat(), "_source_order": source_order, "meter_wh": meter,
                        "power_kw": watts / 1000 if watts is not None else None,
                        "soc": soc}
    ordered = [samples[k] for k in sorted(samples)]
    # A meter reset (or any counter regression) invalidates subsequent session
    # energy. Power and independently sourced SoC remain distinct observations.
    discontinuous, last_meter = False, None
    for point in ordered:
        meter = point.pop("meter_wh")
        if meter is not None:
            if last_meter is not None and meter < last_meter:
                discontinuous = True
            last_meter = meter
        point["energy_kwh"] = (
            (meter - baseline) / 1000
            if not discontinuous and meter is not None and baseline is not None
            and meter >= baseline else None
        )
    # A truncated scan may omit a reset or earlier evidence; do not imply
    # continuous or final energy in a partial-coverage series.
    if limited:
        for point in ordered:
            point["energy_kwh"] = None
    if len(ordered) > point_limit:
        selected = [(i * (len(ordered) - 1)) // (point_limit - 1)
                    for i in range(point_limit)]
        ordered = [ordered[i] for i in selected]
        limited = True
    return {"points": ordered, "limited": limited,
            "discontinuous": discontinuous,
            "source": "local-energy-samples", "coverage": "observed-window"}


def timeline_charts(points: list[dict]) -> list[dict]:
    """SVG coordinates and continuity segments from real observations only."""
    charts = []
    for field, label, unit in (("energy_kwh", "Energía de sesión medida", "kWh"),
                                ("power_kw", "Potencia medida", "kW"),
                                ("soc", "Estado de carga medido", "%")):
        # Source-order positions survive sorting, rejected values and decimation.
        # A line may join only adjacent retained source samples, never skip one.
        present = [(p.get("_source_order", i), datetime.fromisoformat(p["at"]).timestamp(), p[field])
                   for i, p in enumerate(points) if p.get(field) is not None]
        if not present:
            continue
        low_t, high_t = present[0][1], present[-1][1]
        low_y = min(r[2] for r in present)
        high_y = max(r[2] for r in present)
        dots = []
        segments = []
        previous = None
        for index, stamp, value in present:
            dot = {"x": round(30 + (stamp - low_t) * 540 / (high_t - low_t), 2)
                         if high_t > low_t else 300,
                   "y": round(155 - (value - low_y) * 125 / (high_y - low_y), 2)
                         if high_y > low_y else 92,
                   "value": value}
            dots.append(dot)
            # Missing intermediate observations must break the visual curve.
            if previous and index == previous[0] + 1 and stamp > previous[1]:
                segments.append({"a": previous[2], "b": dot})
            previous = (index, stamp, dot)
        charts.append({"field": field, "label": label, "unit": unit,
                       "dots": dots, "segments": segments,
                       "min": low_y, "max": high_y})
    return charts
