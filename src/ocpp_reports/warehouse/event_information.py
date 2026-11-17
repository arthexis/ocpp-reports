"""Bounded, safe event context for a charger-scoped event history.

Never display arbitrary vendor text, raw OCPP evidence, RFID or idTag.
"""
from __future__ import annotations

from collections.abc import Mapping


_STATES = frozenset({
    "Available", "Preparing", "Charging", "SuspendedEV", "SuspendedEVSE",
    "Finishing", "Reserved", "Unavailable", "Faulted",
})
_ERRORS = frozenset({
    "ConnectorLockFailure", "EVCommunicationError", "GroundFailure",
    "HighTemperature", "InternalError", "LocalListConflict", "OtherError",
    "OverCurrentFailure", "OverVoltage", "PowerMeterFailure",
    "PowerSwitchFailure", "ReaderFailure", "ResetFailure", "UnderVoltage",
    "WeakSignal",
})


def _number(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 999999999 else None


def event_information(event: Mapping[str, object]) -> str:
    """Display only explicitly supported and safe fields of a report event."""
    action = event.get("action")
    connector, transaction = _number(event.get("connector_id")), _number(event.get("transaction_id"))
    scope = f"C{connector}" if connector is not None else None
    if action == "StatusNotification":
        status = event.get("status")
        if not isinstance(status, str) or status not in _STATES:
            return "Sin estado en este registro"
        parts = [value for value in (scope, status) if value]
        code = event.get("error_code")
        if isinstance(code, str) and code in _ERRORS:
            parts.append(code)
        return " · ".join(parts)
    if action == "StartTransaction":
        return f"Sesión {transaction}" if transaction is not None else (
            f"{scope} · Inicio registrado" if scope else "Inicio registrado")
    if action == "StopTransaction":
        return f"Sesión {transaction}" if transaction is not None else "Fin registrado"
    if action == "MeterValues":
        return f"{scope} · Medición recibida" if scope else "Medición recibida"
    if action == "Heartbeat":
        return "Señal recibida"
    if action == "BootNotification":
        return "Inicio del cargador registrado"
    if action == "Authorize":
        return "Solicitud registrada"
    if action in ("GetCompositeSchedule", "GetConfiguration"):
        return f"{scope} · Consulta registrada" if scope else "Consulta registrada"
    if action in ("SetChargingProfile", "Reset", "RemoteStartTransaction",
                  "RemoteStopTransaction"):
        return f"{scope} · Solicitud registrada" if scope else "Solicitud registrada"
    if action in ("FirmwareStatusNotification", "DiagnosticsStatusNotification"):
        return "Notificación registrada"
    return "Sin información adicional"
