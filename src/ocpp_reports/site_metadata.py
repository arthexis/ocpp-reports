"""Collector-owned, optional customer and site contact metadata.

Never copied into replicated OCPP evidence or operational snapshot APIs.
Customer/site IDs, rather than labels, are stable identities.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CUSTOMERS = "report_customers"
SITES = "report_sites"
ASSIGNMENTS = "report_site_assignments"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
DEFAULT_HOURS = {day: ("09:00", "14:00") if day == "sun" else ("09:00", "18:00")
                 for day in DAYS}
FIELDS = ("location_name", "address", "branch_phone", "contact_name", "contact_phone")


def ensure_tables(db: sqlite3.Connection) -> None:
    db.execute(f"""CREATE TABLE IF NOT EXISTS {CUSTOMERS} (
        customer_id TEXT PRIMARY KEY, customer_name TEXT NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS {SITES} (
        site_id TEXT PRIMARY KEY, customer_id TEXT,
        location_name TEXT, address TEXT, branch_phone TEXT,
        contact_name TEXT, contact_phone TEXT, timezone TEXT,
        contact_hours TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS {ASSIGNMENTS} (
        source_id TEXT NOT NULL, charger_id TEXT NOT NULL, site_id TEXT NOT NULL,
        PRIMARY KEY(source_id, charger_id)
    )""")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_report_assignments_site ON {ASSIGNMENTS}(site_id)")


def _connect(database: Path, *, write: bool = False) -> sqlite3.Connection:
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError("Existing collector warehouse required")
    db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                         uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    if not write:
        db.execute("PRAGMA query_only=ON")
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def clean(value: object, *, maximum: int = 240) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid field type")
    value = value.strip()
    if not value:
        return None
    if len(value) > maximum or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Invalid field length or control character")
    return value


def phone(value: object) -> str | None:
    value = clean(value, maximum=40)
    if value is None:
        return None
    if not re.fullmatch(r"\+?[0-9][0-9 ()-]{5,38}", value):
        raise ValueError("Invalid phone number")
    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15:
        raise ValueError("Invalid phone number")
    return ("+" if value.startswith("+") else "") + digits


def timezone_name(value: object) -> str:
    value = clean(value, maximum=80) or "America/Monterrey"
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError("Invalid timezone") from exc
    return value


def hours(value: object) -> str | None:
    """Store only overrides; missing weekdays inherit the contact default."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid contact hours") from exc
    if not isinstance(value, dict) or set(value) - set(DAYS):
        raise ValueError("Invalid contact hours")
    normalized = {}
    for day, entry in value.items():
        if entry is None:
            normalized[day] = None  # Explicitly closed
        elif (isinstance(entry, list) and len(entry) == 2
              and all(isinstance(x, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", x)
                      for x in entry) and entry[0] < entry[1]):
            normalized[day] = entry
        else:
            raise ValueError("Invalid contact hours")
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) if normalized else None


def hours_display(raw: str | None) -> str:
    overrides = json.loads(raw) if raw else {}
    lines = []
    for day, title in zip(DAYS, DAY_NAMES):
        period = overrides.get(day, DEFAULT_HOURS[day])
        lines.append(f"{title}: " + ("Cerrado" if period is None else f"{period[0]}–{period[1]}"))
    return " · ".join(lines)


def hours_fields(raw: str | None) -> dict[str, str]:
    overrides = json.loads(raw) if raw else {}
    result = {}
    for day in DAYS:
        period = overrides.get(day, DEFAULT_HOURS[day])
        result[day + "_start"] = period[0] if period else ""
        result[day + "_end"] = period[1] if period else ""
        result[day + "_closed"] = "1" if period is None else ""
    return result


def parse_hours_fields(form: dict) -> str | None:
    configured = {}
    for day in DAYS:
        if form.get(day + "_closed") == "1":
            configured[day] = None
            continue
        begin = str(form.get(day + "_start") or "").strip()
        end = str(form.get(day + "_end") or "").strip()
        if not begin and not end:  # Empty editable day means inherit default
            continue
        configured[day] = [begin, end]
    return hours(configured)


def _site_query(db: sqlite3.Connection, clause: str, args: tuple) -> dict | None:
    row = db.execute(f"""SELECT s.*,c.customer_name
        FROM {SITES} s LEFT JOIN {CUSTOMERS} c ON c.customer_id=s.customer_id
        WHERE {clause}""", args).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["hours_display"] = hours_display(value["contact_hours"])
    value["map_url"] = ("https://www.google.com/maps/search/?api=1&query="
                        + quote(value["address"], safe="")) if value["address"] else None
    value["branch_tel"] = "tel:" + value["branch_phone"] if value["branch_phone"] else None
    value["contact_tel"] = "tel:" + value["contact_phone"] if value["contact_phone"] else None
    return value


def site_for_charger(database: Path, source_id: str, charger_id: str) -> dict | None:
    with _connect(database) as db:
        if not {CUSTOMERS, SITES, ASSIGNMENTS}.issubset(_tables(db)):
            return None
        return _site_query(db, f"""s.site_id=(SELECT site_id FROM {ASSIGNMENTS}
                              WHERE source_id=? AND charger_id=?)""", (source_id, charger_id))


def _authorized_site(db: sqlite3.Connection, site_id: str,
                     allowed_source_ids: tuple[str, ...] | None) -> bool:
    if not db.execute(f"SELECT 1 FROM {SITES} WHERE site_id=?", (site_id,)).fetchone():
        return False
    if allowed_source_ids is None:
        return True
    if not allowed_source_ids:
        return False
    rows = [row[0] for row in db.execute(
        f"SELECT DISTINCT source_id FROM {ASSIGNMENTS} WHERE site_id=?", (site_id,))]
    return bool(rows) and all(source in allowed_source_ids for source in rows)


def editable_sites(database: Path, allowed_source_ids: tuple[str, ...] | None) -> list[dict]:
    with _connect(database) as db:
        if not {CUSTOMERS, SITES, ASSIGNMENTS}.issubset(_tables(db)):
            return []
        rows = db.execute(f"""SELECT s.site_id,s.location_name,c.customer_name
            FROM {SITES} s LEFT JOIN {CUSTOMERS} c ON c.customer_id=s.customer_id
            ORDER BY COALESCE(c.customer_name,''),COALESCE(s.location_name,'')
            LIMIT 500""").fetchall()
        return [dict(row) for row in rows if
                _authorized_site(db, row["site_id"], allowed_source_ids)]


def _authorized_customer(db: sqlite3.Connection, customer_id: str,
                         allowed_source_ids: tuple[str, ...] | None) -> bool:
    if not db.execute(f"SELECT 1 FROM {CUSTOMERS} WHERE customer_id=?",
                      (customer_id,)).fetchone():
        return False
    sites = [row[0] for row in db.execute(
        f"SELECT site_id FROM {SITES} WHERE customer_id=?", (customer_id,))]
    if allowed_source_ids is None:
        return True
    return bool(sites) and all(_authorized_site(db, site, allowed_source_ids)
                               for site in sites)


def editable_customers(database: Path, allowed_source_ids: tuple[str, ...] | None) -> list[dict]:
    with _connect(database) as db:
        if not {CUSTOMERS, SITES, ASSIGNMENTS}.issubset(_tables(db)):
            return []
        rows = db.execute(
            f"SELECT customer_id,customer_name FROM {CUSTOMERS} ORDER BY customer_name LIMIT 500"
        ).fetchall()
        return [dict(row) for row in rows
                if _authorized_customer(db, row["customer_id"], allowed_source_ids)]


def known_charger(db: sqlite3.Connection, source_id: str, charger_id: str) -> bool:
    tables = _tables(db)
    if "connector_state" in tables and db.execute(
            "SELECT 1 FROM connector_state WHERE source_id=? AND charger_id=? LIMIT 1",
            (source_id, charger_id)).fetchone():
        return True
    if "collector_latest" in tables and db.execute(
            """SELECT 1 FROM collector_latest WHERE source_id=? AND kind='connector'
               AND json_valid(payload) AND json_extract(payload,'$.charger_id')=? LIMIT 1""",
            (source_id, charger_id)).fetchone():
        return True
    return False


def _require_scope(source_id: str, allowed_source_ids: tuple[str, ...] | None) -> None:
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        raise PermissionError("Source not authorized")


def save_site(database: Path, source_id: str, charger_id: str, fields: dict,
              *, allowed_source_ids: tuple[str, ...] | None) -> str:
    """Create or edit the assigned site, atomically. Never infer by display name."""
    _require_scope(source_id, allowed_source_ids)
    values = {key: clean(fields.get(key), maximum=500 if key == "address" else 160)
              for key in FIELDS}
    for key in ("branch_phone", "contact_phone"):
        values[key] = phone(fields.get(key))
    customer_name = clean(fields.get("customer_name"), maximum=160)
    zone = timezone_name(fields.get("timezone"))
    weekly = parse_hours_fields(fields)
    with _connect(database, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_tables(db)
        if not known_charger(db, source_id, charger_id):
            raise ValueError("Unknown charger")
        current = db.execute(f"""SELECT site_id FROM {ASSIGNMENTS}
            WHERE source_id=? AND charger_id=?""", (source_id, charger_id)).fetchone()
        site_id = current["site_id"] if current else uuid4().hex
        if current and not _authorized_site(db, site_id, allowed_source_ids):
            raise PermissionError("Shared site is outside administrative scope")
        prior = db.execute(f"SELECT customer_id FROM {SITES} WHERE site_id=?",
                           (site_id,)).fetchone()
        customer_id = prior["customer_id"] if prior else None
        if "customer_id" in fields:
            # A blank selection explicitly creates a new customer (if named),
            # or unlinks the site; omission retains its current association.
            customer_id = clean(fields["customer_id"], maximum=64)
        if customer_id and not _authorized_customer(db, customer_id, allowed_source_ids):
            raise PermissionError("Customer is outside administrative scope")
        if customer_name:
            if customer_id:
                db.execute(f"UPDATE {CUSTOMERS} SET customer_name=? WHERE customer_id=?",
                           (customer_name, customer_id))
            else:
                customer_id = uuid4().hex
                db.execute(f"INSERT INTO {CUSTOMERS} VALUES (?,?)",
                           (customer_id, customer_name))
        data = (customer_id, *[values[key] for key in FIELDS], zone, weekly)
        if prior:
            db.execute(f"""UPDATE {SITES} SET customer_id=?,location_name=?,
                address=?,branch_phone=?,contact_name=?,contact_phone=?,
                timezone=?,contact_hours=? WHERE site_id=?""", (*data, site_id))
        else:
            db.execute(f"""INSERT INTO {SITES}
                (site_id,customer_id,location_name,address,branch_phone,contact_name,
                 contact_phone,timezone,contact_hours) VALUES (?,?,?,?,?,?,?,?,?)""",
                (site_id, *data))
            db.execute(f"INSERT INTO {ASSIGNMENTS} VALUES (?,?,?)",
                       (source_id, charger_id, site_id))
        db.commit()
    return site_id


def assign_site(database: Path, source_id: str, charger_id: str, site_id: str,
                *, allowed_source_ids: tuple[str, ...] | None) -> None:
    _require_scope(source_id, allowed_source_ids)
    site_id = clean(site_id, maximum=64)
    if not site_id:
        raise ValueError("Missing site")
    with _connect(database, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        ensure_tables(db)
        if not known_charger(db, source_id, charger_id):
            raise ValueError("Unknown charger")
        current = db.execute(f"SELECT site_id FROM {ASSIGNMENTS} WHERE source_id=? AND charger_id=?",
                             (source_id, charger_id)).fetchone()
        if current and not _authorized_site(db, current["site_id"], allowed_source_ids):
            raise PermissionError("Current site is outside administrative scope")
        if not _authorized_site(db, site_id, allowed_source_ids):
            raise PermissionError("Target site is outside administrative scope")
        db.execute(f"""INSERT INTO {ASSIGNMENTS} (source_id,charger_id,site_id)
            VALUES (?,?,?) ON CONFLICT(source_id,charger_id)
            DO UPDATE SET site_id=excluded.site_id""", (source_id, charger_id, site_id))
        db.commit()
