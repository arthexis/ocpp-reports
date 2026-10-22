"""Existing charger GetDiagnostics archives import locally on a combined Watchtower."""
import hashlib
import json
from pathlib import Path

from ocpp_reports.diagnostic_archive import import_local, inventory


def test_import_existing_and_retained_charger_archives_idempotently(tmp_path):
    data_dir = tmp_path / "csms"
    records = data_dir / "diagnostics-requests"
    records.mkdir(parents=True)
    uploads = tmp_path / "uploads"
    for token, cp, subdir, payload in [
        ("a" * 40, "ABB001", "", b"older ABB diagnostic"),
        ("b" * 40, "ABB002", "retained", b"retained ABB diagnostic"),
    ]:
        (records / f"{token}.json").write_text(json.dumps({"charger": cp}))
        folder = uploads / subdir / token
        folder.mkdir(parents=True)
        (folder / "upload.bin").write_bytes(payload)
    archive = tmp_path / "reports"
    assert import_local(archive, data_dir, uploads, "porsche-abb") == 2
    assert import_local(archive, data_dir, uploads, "porsche-abb") == 0
    for cp, payload in [("ABB001", b"older ABB diagnostic"), ("ABB002", b"retained ABB diagnostic")]:
        items = inventory(archive, "porsche-abb", cp)
        assert len(items) == 1
        digest = hashlib.sha256(payload).hexdigest()
        assert items[0]["sha256"] == digest
        assert (archive / "porsche-abb" / cp / f"{digest}.bin").read_bytes() == payload
    assert (uploads / ("a" * 40) / "upload.bin").read_bytes() == b"older ABB diagnostic"
