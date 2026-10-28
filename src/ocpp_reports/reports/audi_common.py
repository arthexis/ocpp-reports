"""Shared Audi CSV primitives; preserve both established output layouts.

The historical audi-csv renderer uses ISO-8601 dates and legacy field names.
The configurable profile uses a space-separated local timestamp and battery
field names. These are deliberately separate public output contracts.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

HEADERS = ("LOCACION", "CONECTOR", "FECHA INICIO", "FECHA FINAL",
           "WH INICIO", "WH FINAL", "WH USADOS", "% INICIAL",
           "% FINAL", "RAZON FINAL")


def writer_buffer() -> tuple[io.StringIO, csv.writer]:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    return output, writer


def local_date(value: str | None, zone: ZoneInfo, *, iso: bool) -> str:
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone)
    return parsed.isoformat() if iso else parsed.strftime("%Y-%m-%d %H:%M:%S")


def blank(value: object) -> object:
    return "" if value is None else value
