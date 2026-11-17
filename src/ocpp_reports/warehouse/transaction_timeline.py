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

from .history_enrichment import verified_overlay


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


def _temperature(value):
    return value if type(value) in (int, float) and math.isfinite(value) and -273.15 <= value <= 1000 else None


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
        # Local evidence uses its verified transaction cursor window. Remote
        # evidence has an independent indexed identity tuple, never latest state.
        bounds = None
        if {"energy_samples", "transaction_observations"} <= tables:
            bounds = db.execute(
                "SELECT MIN(page_after),MAX(page_next) FROM transaction_observations "
                "WHERE source_id=? AND charger_id=? AND transaction_id=?",
                (source, charger, tx_id),
            ).fetchone()
        if bounds and (type(bounds[0]) is int and type(bounds[1]) is int
                       and bounds[0] >= 0 and bounds[1] > bounds[0]):
            sample_source = "local-energy-samples"
            overlay = ("LEFT JOIN sample_enrichments x ON "
                       "x.source_id=e.source_id AND x.event_id=e.event_id "
                       "AND x.sample_index=e.sample_index "
                       if "sample_enrichments" in tables else "")
            enrichment = "x.payload" if overlay else "NULL"
            rows = db.execute(
                f"SELECT e.event_id,e.sample_index,e.payload,{enrichment} "
                f"FROM energy_samples e {overlay}"
                "WHERE e.source_id=? AND e.event_id>? AND e.event_id<=? "
                "ORDER BY e.event_id DESC,e.sample_index DESC LIMIT ?",
                (source, bounds[0], bounds[1], scan_limit + 1),
            ).fetchall()
        elif "collector_meter_samples" in tables:
            # Only verified per-session immutable samples. No scanning a remote
            # source or fabricating a time series from collector_latest.
            sample_source = "replicated-meter-samples"
            overlay = ("LEFT JOIN collector_meter_enrichments x ON "
                       "x.source_id=e.source_id AND x.event_id=e.event_id "
                       "AND x.sample_index=e.sample_index "
                       if "collector_meter_enrichments" in tables else "")
            enrichment = "x.payload" if overlay else "NULL"
            rows = db.execute(
                f"SELECT e.event_id,e.sample_index,e.payload,{enrichment} "
                f"FROM collector_meter_samples e {overlay}"
                "WHERE e.source_id=? AND e.charger_id=? AND e.connector_id=? "
                "AND e.transaction_id=? "
                "ORDER BY e.event_id DESC,e.sample_index DESC LIMIT ?",
                (source, charger, connector, tx_id, scan_limit + 1),
            ).fetchall()
        else:
            return empty
    limited = len(rows) > scan_limit
    baseline = _quantity(transaction.get("meter_start_wh"))
    now = datetime.now(timezone.utc)
    samples = {}
    # Preserve physical cursor order before time sorting or point selection.
    # A rejected/corrupt sample occupies a slot, so gaps cannot be joined.
    for source_order, (event_id, sample_index, payload, enrichment) in enumerate(reversed(rows[:scan_limit])):
        try:
            item = json.loads(payload)
            if enrichment is not None:
                verified = verified_overlay(item, payload, json.loads(enrichment))
                if verified is not None:
                    item = verified
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
        voltage = _quantity(item.get("voltage_v"))
        current = _quantity(item.get("current_import_a"))
        offered = _quantity(item.get("current_offered_a"))
        temperature = _temperature(item.get("temperature_c"))
        if all(value is None for value in
               (meter, watts, soc, voltage, current, offered, temperature)):
            continue
        key = (at.isoformat(), event_id, sample_index)
        samples[key] = {"at": at.isoformat(), "_source_order": source_order, "meter_wh": meter,
                        "power_kw": watts / 1000 if watts is not None else None,
                        "soc": soc, "voltage_v": voltage, "current_import_a": current,
                        "current_offered_a": offered, "temperature_c": temperature}
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
            "source": sample_source, "coverage": "observed-window"}


def timeline_charts(points: list[dict]) -> list[dict]:
    """SVG coordinates and continuity segments from real observations only."""
    charts = []
    for field, label, unit in (("energy_kwh", "Energía de sesión medida", "kWh"),
                                ("power_kw", "Potencia medida", "kW"),
                                ("soc", "Estado de carga medido", "%"),
                                ("voltage_v", "Voltaje observado", "V"),
                                ("current_import_a", "Corriente importada", "A"),
                                ("current_offered_a", "Corriente ofrecida", "A"),
                                ("temperature_c", "Temperatura informada", "°C")):
        # Source-order positions survive sorting, rejected values and decimation.
        # A line may join only adjacent retained source samples, never skip one.
        present = [(p.get("_source_order", i), datetime.fromisoformat(p["at"]).timestamp(), p.get(field))
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
