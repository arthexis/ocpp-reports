"""System service must use the existing authenticated sender without new tokens."""
from pathlib import Path


def test_lcd_hook_service_is_read_only_and_uses_existing_abb_sender():
    root = Path(__file__).resolve().parents[1]
    unit = (root / "ansible/templates/ocpp-reports-lcd-hook.service.j2").read_text()
    assert "User=ubuntu" in unit
    assert "Group=ubuntu" in unit
    assert "Environment=HOME=/home/ubuntu" in unit
    assert "--database /var/lib/ocpp-reports/warehouse.sqlite" in unit
    assert "--state /var/lib/ocpp-reports-lcd/hook-state.json" in unit
    assert "--lcd /home/ubuntu/.local/bin/lcd" in unit
    assert "--sound /home/ubuntu/.local/bin/sound" in unit
    assert "StateDirectory=ocpp-reports-lcd" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "ReadWritePaths=/var/lib/ocpp-reports-lcd" in unit
    assert "server-source.token" not in unit
    assert "ExecStartPre=" not in unit
