"""Read-only OCPP Reports -> GWay LCD carousel and at-most-once audio producer.

The Reports service account owns its own authenticated LCD/Sound route.  No
application or raw OCPP data is included in the transport payload.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
import subprocess
import tempfile
import time
import unicodedata

from .warehouse_queries import snapshot
from .warehouse.transaction_timeline import transaction_timeline

ACTIVE = {"Preparing", "Charging"}
ERROR = {"Faulted", "Unavailable"}
SOUNDS = {"Preparing": "notice", "Charging": "ok", "Complete": "ok",
          "Faulted": "warning", "Unavailable": "warning", "EmergencyStop": "error"}
MAX_DISPLAYS = 16
DEFAULT_DB = Path("/var/lib/ocpp-reports/warehouse.sqlite")
DEFAULT_STATE = Path("/var/lib/ocpp-reports/lcd-hook-state.json")


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def minutes_since(timestamp, now):
    began = instant(timestamp)
    if began is None or began > now:
        return "---"
    return f"{min(999, int((now - began).total_seconds() // 60)):03d}"


def short_location(name, minute):
    """A two-word location loses the first word except its initial."""
    text = unicodedata.normalize("NFKD", str(name or "Unknown"))
    text = "".join(c for c in text if ord(c) < 128 and c.isprintable()).strip()
    words = text.split()
    if len(words) == 2:
        text = f"{words[0][0]} {words[1]}"
    else:
        text = " ".join(words) or "Unknown"
    suffix = f" {minute}m"
    return text[:16 - len(suffix)].rstrip() + suffix


def fresh_soc(transaction, now):
    """Only a timestamped OCPP-meter SoC for this exact transaction."""
    if not isinstance(transaction, dict):
        return None
    # Populated exclusively from transaction_timeline's verified immutable
    # OCPP MeterValues sample, source/charger/connector/tx ID already checked.
    sample = transaction.get("_lcd_verified_soc")
    if type(sample) is dict:
        observed = instant(sample.get("at"))
        value = sample.get("value")
        if (type(value) in (int, float) and math.isfinite(value)
                and 0 <= value <= 100 and observed is not None
                and timedelta(0) <= now - observed <= timedelta(seconds=120)):
            return f"{round(value):d}%"
    source = transaction.get("soc_provenance") or transaction.get("soc_end_provenance")
    if source != "ocpp-meter-values":
        return None
    value = transaction.get("soc") if transaction.get("soc") is not None else transaction.get("soc_end")
    observed = instant(transaction.get("soc_observed_at") or transaction.get("soc_end_observed_at"))
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        return None
    if observed is None or not timedelta(0) <= now - observed <= timedelta(seconds=120):
        return None
    return f"{round(value):d}%"


def _fresh_sources(data, now):
    sources = set()
    for item in data.get("sources", []):
        received = instant(item.get("last_ingested_at"))
        if received is not None and timedelta(0) <= now - received <= timedelta(seconds=180):
            sources.add(item.get("source_id"))
    return sources


def project(data, *, now):
    """Return frames and connector states; never infer station errors from C1."""
    fresh = _fresh_sources(data, now)
    transactions = {}
    recently_stopped = {}
    emergency = {}
    for row in data.get("transactions", []):
        source, charger, connector = row.get("source_id"), row.get("charger_id"), row.get("connector_id")
        if source not in fresh or type(connector) is not int or connector <= 0:
            continue
        key = (source, charger, connector)
        tx_id = row.get("id")
        if type(tx_id) is int:
            transactions[source, charger, connector, tx_id] = row
        stopped = instant(row.get("stopped_at"))
        if stopped is None or not timedelta(0) <= now - stopped < timedelta(seconds=120):
            continue
        if key not in recently_stopped or stopped > recently_stopped[key][0]:
            recently_stopped[key] = (stopped, row)
        if row.get("reason") == "EmergencyStop" or row.get("stop_reason") == "EmergencyStop":
            station = (source, charger)
            if station not in emergency or stopped > emergency[station][0]:
                emergency[station] = (stopped, row)

    stations = {}
    connectors = []
    for row in data.get("connectors", []):
        source, charger, index = row.get("source_id"), row.get("charger_id"), row.get("connector_id")
        if source not in fresh or type(index) is not int:
            continue
        if index == 0:
            stations[(source, charger)] = row
        elif index > 0:
            connectors.append(row)
    displays = []
    states = {}

    def append(source, charger, connector, state, start, location, soc=None, until=None, tx_id=None, urgent=False, elapsed_at=None):
        key = f"{source}|{charger}|{connector}"
        states[key] = {"state": state, "tx": tx_id, "audible": True}
        title = short_location(location, minutes_since(start, elapsed_at or now))
        line2 = state + (f" {soc}" if soc and state in ("Charging", "Complete") else "")
        frame = {"id": hashlib.sha256(key.encode()).hexdigest()[:24],
                 "line1": title, "line2": line2[:16]}
        if until is not None:
            frame["until"] = until.isoformat()
        displays.append((0 if urgent else 1, source, charger, connector, frame))

    for (source, charger), row in sorted(stations.items()):
        state = str(row.get("status") or "")
        # Baseline healthy C0 too: an eventual station-wide fault or
        # EmergencyStop is a true audible transition, not a new identity.
        states[f"{source}|{charger}|0"] = {"state": state, "tx": None, "audible": False}
        if state in ERROR:
            append(source, charger, 0, state, row.get("status_observed_at"),
                   row.get("location") or source, urgent=True)
    for (source, charger), (stopped, row) in sorted(emergency.items()):
        name = next((c.get("location") for c in connectors if
                     c.get("source_id") == source and c.get("charger_id") == charger), source)
        # A real emergency stop outranks an ordinary station status.
        displays[:] = [d for d in displays if not (d[1] == source and d[2] == charger and d[3] == 0)]
        append(source, charger, 0, "EmergencyStop", row.get("stopped_at"),
               name, until=stopped + timedelta(seconds=120), urgent=True)

    for row in sorted(connectors, key=lambda r: (
            str(r.get("source_id")), str(r.get("charger_id")), int(r.get("connector_id")))):
        source, charger, index = row["source_id"], row["charger_id"], row["connector_id"]
        status = str(row.get("status") or "")
        key = f"{source}|{charger}|{index}"
        tx_id = row.get("transaction_id")
        tx = transactions.get((source, charger, index, tx_id)) if type(tx_id) is int else None
        location = row.get("location") or source
        stopped_info = recently_stopped.get((source, charger, index))
        # An exporter may retain a last-known Charging connector status after
        # the authoritative transaction has stopped. Prefer the verified stop
        # only if no different/new transaction superseded it.
        completed = (stopped_info is not None and
                     (type(tx_id) is not int or tx_id == stopped_info[1].get("id")) and
                     (source, charger) not in emergency)
        if completed:
            stopped, finished = stopped_info
            append(source, charger, index, "Complete",
                   finished.get("started_at") or finished.get("stopped_at"),
                   location, until=stopped + timedelta(seconds=120),
                   tx_id=finished.get("id"), elapsed_at=stopped)
        elif status in ACTIVE:
            start = tx.get("started_at") if status == "Charging" and tx else row.get("status_observed_at")
            append(source, charger, index, status, start, location,
                   fresh_soc(tx, now) if status == "Charging" else None, tx_id=tx_id)
        else:
            # Connector-only faults are not station-level incidents. Do not
            # generate a sound or LCD frame when C0 remains healthy/unknown.
            states[key] = {"state": status, "tx": tx_id, "audible": False}
    # Preserve deterministic ordering with station-wide incidents first.
    displays.sort(key=lambda d: (d[0], d[1], d[2], d[3]))
    return [entry[-1] for entry in displays[:MAX_DISPLAYS]], states


def read_state(path):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError("unsafe hook state")
    data = json.loads(path.read_text(encoding="utf-8"))
    if type(data) is not dict or data.get("schema") != 1 or type(data.get("states")) is not dict:
        raise ValueError("invalid hook state")
    return data["states"]


def store_state(path, states):
    if path.is_symlink():
        raise ValueError("unsafe hook state target")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = json.dumps({"schema": 1, "states": states},
                          separators=(",", ":"), sort_keys=True).encode()
    if len(document) > 256 * 1024:
        raise ValueError("hook state over budget")
    fd, temporary = tempfile.mkstemp(prefix=".lcd-hook-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(document)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def transitions(before, after):
    """Only changes to a previously observed exact connector state make sound."""
    if before is None:
        return []
    result = []
    for key, row in after.items():
        old = before.get(key)
        if old is None or old.get("state") == row["state"]:
            continue
        if row.get("audible") is True and row["state"] in SOUNDS:
            result.append((key, SOUNDS[row["state"]]))
    return result


def one_cycle(database, state, *, lcd, sound, run=subprocess.run, now=None):
    now = now or datetime.now(timezone.utc)
    data = snapshot(Path(database), transaction_limit=500, skip_events=True)
    # A projected SoC endpoint alone is not evidence of a recent reading.
    # Consult indexed immutable meter history for each exact active session.
    charging = {(c.get("source_id"), c.get("charger_id"),
                 c.get("connector_id"), c.get("transaction_id"))
                for c in data.get("connectors", [])
                if c.get("status") == "Charging" and type(c.get("transaction_id")) is int}
    count = 0
    for tx in data.get("transactions", []):
        key = (tx.get("source_id"), tx.get("charger_id"), tx.get("connector_id"), tx.get("id"))
        if key not in charging or tx.get("stopped_at") is not None or count >= MAX_DISPLAYS:
            continue
        count += 1
        try:
            trace = transaction_timeline(database, tx, scan_limit=256, point_limit=120)
            measured = [point for point in trace.get("points", [])
                        if type(point.get("soc")) in (int, float)
                        and instant(point.get("at")) is not None
                        and timedelta(0) <= now - instant(point["at"]) <= timedelta(seconds=120)]
            if measured:
                latest = max(measured, key=lambda point: point["at"])
                tx["_lcd_verified_soc"] = {"value": latest["soc"], "at": latest["at"]}
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass  # Missing meter evidence never produces an inferred SoC.
    frames, statuses = project(data, now=now)
    previous = read_state(Path(state))
    # Journal the new state BEFORE any physical sound side effect. A crash
    # can lose a notification but must never replay an uncertain output.
    store_state(Path(state), statuses)
    try:
        from .activity_feed import publish
        publish(previous, statuses, now)
    except Exception:
        pass
    # One atomic whole-carousel update, not independent overlay modifications.
    payload = json.dumps(frames, sort_keys=True, separators=(",", ":"))
    result = run([str(lcd), "reports", payload], timeout=18, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("LCD reports forwarding unconfirmed (no fallback/retry)")
    for _key, earcon in transitions(previous, statuses):
        # No retry for an uncertain sound, even if the transport times out.
        run([str(sound), earcon], timeout=18, capture_output=True, check=False)
    return {"frames": len(frames), "transitions": len(transitions(previous, statuses))}


def run_worker(database=DEFAULT_DB, state=DEFAULT_STATE, *,
               lcd="lcd", sound="sound", interval=30, once=False):
    if not 10 <= interval <= 300:
        raise ValueError("invalid refresh interval")
    # One producer owns the persisted state and remote output at a time.
    # Competing service/manual invocations cannot both claim one transition.
    lock = Path(str(state) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            started = time.monotonic()
            try:
                one_cycle(database, state, lcd=lcd, sound=sound)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                # A failed cycle never triggers a local hardware fallback.
                print(f"lcd-hook: {exc}", file=sys.stderr, flush=True)
                if once:
                    raise
            if once:
                return
            time.sleep(max(0.5, interval - (time.monotonic() - started)))
    finally:
        os.close(fd)
