from pathlib import Path

import pytest

from ocpp_reports.dashboard_htpasswd import inventory


def test_redacted_inventory(tmp_path: Path):
    file = tmp_path / "users.htpasswd"
    file.write_text("admin:$apr1$secret$hash\noperator:$2y$12$secret\n")
    result = inventory(file)
    assert result == [
        {"username": "admin", "hash_family": "apr1"},
        {"username": "operator", "hash_family": "bcrypt"},
    ]
    assert "secret" not in str(result)


def test_duplicate_identity_refused(tmp_path: Path):
    file = tmp_path / "users.htpasswd"
    file.write_text("admin:$apr1$one\nadmin:$apr1$two\n")
    with pytest.raises(ValueError, match="duplicate"):
        inventory(file)


def test_symlink_refused(tmp_path: Path):
    file = tmp_path / "users.htpasswd"
    file.write_text("admin:$apr1$one\n")
    link = tmp_path / "alias"
    link.symlink_to(file)
    with pytest.raises(ValueError, match="regular"):
        inventory(link)
