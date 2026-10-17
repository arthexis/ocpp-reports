"""System-wide TOML configuration contract."""
import pytest
from ocpp_reports.config import load_config, mail_settings


def test_mail_settings_from_toml(tmp_path, monkeypatch):
    for name in ("OCPP_REPORTS_SMTP_HOST", "OCPP_REPORTS_SMTP_FROM",
                 "OCPP_REPORTS_SMTP_PORT", "OCPP_REPORTS_SMTP_TLS",
                 "OCPP_REPORTS_OPERATOR_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.toml"
    path.write_text('[mail]\nhost = "smtp.example.com"\nfrom = "reports@example.com"\nto = "me@example.com"\nport = 587\ntls = "starttls"\n')
    settings = mail_settings(path)
    assert settings["host"] == "smtp.example.com"
    assert settings["recipient"] == "me@example.com"
    monkeypatch.setenv("OCPP_REPORTS_SMTP_HOST", "override.example.com")
    assert mail_settings(path)["host"] == "override.example.com"


def test_invalid_toml_and_port(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[mail\n")
    with pytest.raises(ValueError):
        load_config(path)
    path.write_text('[mail]\nport = 70000\n')
    with pytest.raises(ValueError, match="port"):
        mail_settings(path)
