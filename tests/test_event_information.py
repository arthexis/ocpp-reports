"""No arbitrary payload strings may become event summary/detail context."""
from ocpp_reports.warehouse.event_information import event_information


def test_information_from_only_safe_fields():
    assert event_information({"action": "StatusNotification", "status": "Available",
                              "connector_id": 0}) == "C0 · Available"
    assert event_information({"action": "StatusNotification", "status": "Faulted",
                              "connector_id": 2, "error_code": "ReaderFailure",
                              "info": "PRIVATE"}).endswith("Faulted · ReaderFailure")
    assert event_information({"action": "StatusNotification", "status": None,
                              "connector_id": None}) == "Sin estado en este registro"
    assert event_information({"action": "StartTransaction",
                              "transaction_id": 15}) == "Sesión 15"
    assert event_information({"action": "MeterValues"}) == "Medición recibida"
    assert event_information({"action": "Heartbeat"}) == "Señal recibida"
    assert event_information({"action": "BootNotification"}) == "Inicio del cargador registrado"
    assert event_information({"action": "Authorize", "id_tag": "RFID-SENSITIVE"}) == "Solicitud registrada"
    assert event_information({"action": "GetCompositeSchedule", "connector_id": 1}) == "C1 · Consulta registrada"
    assert event_information({"action": "StopTransaction", "transaction_id": None}) == "Fin registrado"


def test_information_never_echoes_untrusted_values():
    sensitive = "<script>PRIVATE-RFID-NOT-RENDER</script>"
    for action in ("StatusNotification", "MeterValues", "BootNotification", "Authorize", "Unknown"):
        text = event_information({"action": action, "status": sensitive,
                                  "error_code": sensitive, "connector_id": sensitive,
                                  "transaction_id": sensitive, "info": sensitive,
                                  "id_tag": sensitive, "vendor_id": sensitive})
        assert "PRIVATE" not in text and "<" not in text
    assert event_information({"action": "StatusNotification",
                              "status": "Charging", "connector_id": True}) == "Charging"
    assert event_information({"action": "StartTransaction",
                              "transaction_id": -1}) == "Inicio registrado"
