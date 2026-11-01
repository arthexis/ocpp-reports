"""Keep local ingestion attached to subsequent CSMS starts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_csms_start_pulls_in_ingest_after_clean_dependency_stop():
    ingest = (ROOT / "ansible/templates/ocpp-reports-ingest.service.j2").read_text()
    reader = (ROOT / "ansible/templates/ocpp-reports-csms-reader.service.j2").read_text()
    tasks = (ROOT / "ansible/tasks/local-ingest.yml").read_text()

    assert "WantedBy=multi-user.target ocpp-csms.service" in ingest
    assert "Requires=ocpp-reports-csms-reader.service" in ingest
    assert "After=network.target ocpp-reports-csms-reader.service" in ingest
    assert "Requires=ocpp-csms.service" in reader
    assert "After=ocpp-csms.service" in reader
    assert "dest: /etc/systemd/system/ocpp-csms.service.wants/ocpp-reports-ingest.service" in tasks
    assert "src: /etc/systemd/system/ocpp-reports-ingest.service" in tasks
    assert "reports_local_csms_start_link.changed" in tasks
