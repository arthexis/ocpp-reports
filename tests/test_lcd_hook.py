"""Read-only, hermetic lifecycle, screen and sound transition contracts."""
from datetime import datetime, timedelta, timezone
import json
import pytest

from ocpp_reports.lcd_hook import (short_location, minutes_since, fresh_soc,
                                   project, transitions, one_cycle)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def data(c1="Charging", c0="Available", tx=True, stop=None, reason=None):
    observed = (NOW - timedelta(minutes=37)).isoformat()
    items = [
        {"source_id": "abc", "charger_id": "ABB", "connector_id": 0,
         "status": c0, "location": "Porsche Monterrey",
         "status_observed_at": observed},
        {"source_id": "abc", "charger_id": "ABB", "connector_id": 1,
         "status": c1, "location": "Porsche Monterrey",
         "status_observed_at": observed, "transaction_id": 9 if tx else None},
    ]
    transaction = {
        "source_id": "abc", "charger_id": "ABB", "connector_id": 1, "id": 9,
        "started_at": observed, "stopped_at": stop.isoformat() if stop else None,
        "reason": reason,
        "soc": 78, "soc_provenance": "ocpp-meter-values",
        "soc_observed_at": (NOW - timedelta(seconds=30)).isoformat(),
    }
    return {"sources": [{"source_id": "abc", "last_ingested_at": NOW.isoformat()}],
            "connectors": items, "transactions": [transaction]}


def test_location_abbreviation_and_elapsed():
    assert minutes_since((NOW - timedelta(minutes=37)).isoformat(), NOW) == "037"
    assert short_location("Porsche Monterrey", "037") == "P Monterrey 037m"
    assert len(short_location("Longer Secondword", "999")) <= 16
    assert short_location("Three Very Long Words", "003").endswith(" 003m")
    assert minutes_since(None, NOW) == "---"


def test_charging_and_verified_soc():
    frames, states = project(data(), now=NOW)
    assert len(frames) == 1
    assert frames[0]["line1"] == "P Monterrey 037m"
    assert frames[0]["line2"] == "Charging 78%"
    assert states["abc|ABB|1"]["state"] == "Charging"


def test_stale_soc_suppressed():
    session = data()
    session["transactions"][0]["soc_observed_at"] = (NOW - timedelta(minutes=5)).isoformat()
    assert project(session, now=NOW)[0][0]["line2"] == "Charging"


def test_connectors_fault_do_not_generate_station_alarm():
    frames, states = project(data(c1="Faulted", c0="Available"), now=NOW)
    assert frames == []
    assert states["abc|ABB|1"]["state"] == "Faulted"


def test_station_fault_visible_and_alertable():
    frames, states = project(data(c1="Faulted", c0="Faulted"), now=NOW)
    assert [f["line2"] for f in frames] == ["Faulted"]
    assert transitions({"abc|ABB|0": {"state": "Available", "tx": None}}, states) == [
        ("abc|ABB|0", "warning")]


def test_emergency_stop_visible_even_with_c0_available():
    stop = NOW - timedelta(seconds=10)
    frames, states = project(data(c1="Available", c0="Available",
                                  stop=stop, reason="EmergencyStop"), now=NOW)
    assert [f["line2"] for f in frames] == ["EmergencyStop"]
    assert frames[0]["until"] == (stop + timedelta(seconds=120)).isoformat()
    assert states["abc|ABB|0"]["state"] == "EmergencyStop"


def test_complete_120_seconds_not_offline_artifact():
    stop = NOW - timedelta(seconds=119)
    frames, states = project(data(c1="Available", tx=False, stop=stop), now=NOW)
    assert [f["line2"] for f in frames] == ["Complete 78%"] or [f["line2"] for f in frames] == ["Complete"]
    frames, _ = project(data(c1="Available", tx=False,
                             stop=NOW-timedelta(seconds=121)), now=NOW)
    assert frames == []


def test_stale_ingestion_disables_outputs_without_false_complete():
    session = data()
    session["sources"][0]["last_ingested_at"] = (NOW - timedelta(minutes=10)).isoformat()
    assert project(session, now=NOW) == ([], {})


def test_sounds_exactly_once_per_status_transition():
    current = {"a": {"state": "Charging", "tx": 9},
               "b": {"state": "Preparing", "tx": None}}
    assert transitions(None, current) == []
    assert transitions(current, current) == []
    after = {"a": {"state": "Complete", "tx": 9},
             "b": {"state": "Charging", "tx": 10}}
    assert transitions(current, after) == [("a", "ok"), ("b", "ok")]


def test_periodic_refresh_never_replays_sound(tmp_path, monkeypatch):
    from ocpp_reports import lcd_hook
    monkeypatch.setattr(lcd_hook, "snapshot", lambda *args, **kw: data())
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        return type("Result", (), {"returncode": 0})()
    state = tmp_path / "hook.json"
    assert one_cycle(tmp_path / "no-db-access", state, lcd="lcd", sound="sound", run=fake_run, now=NOW)["frames"] == 1
    assert one_cycle(tmp_path / "no-db-access", state, lcd="lcd", sound="sound", run=fake_run, now=NOW)["transitions"] == 0
    assert len(calls) == 2
    assert [item[1] for item in calls] == ["reports", "reports"]


def test_send_failure_has_no_sound_and_does_not_replay(tmp_path, monkeypatch):
    from ocpp_reports import lcd_hook
    monkeypatch.setattr(lcd_hook, "snapshot", lambda *args, **kw: data())
    state = tmp_path / "hook.json"
    with pytest.raises(RuntimeError):
        one_cycle(tmp_path / "db", state, lcd="lcd", sound="sound",
                  run=lambda *a, **kw: type("Result", (), {"returncode": 1})(), now=NOW)
    assert json.loads(state.read_text())["schema"] == 1
