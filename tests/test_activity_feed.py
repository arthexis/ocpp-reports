from ocpp_reports.activity_feed import changes, enqueue
from unittest.mock import Mock

def test_fault_transition_only():
    normal = {"private-id": {"state": "Charging"}}
    fault = {"private-id": {"state": "Faulted"}}
    assert changes(None, fault) == ()
    assert changes(normal, fault) == ("fault_observed",)
    assert changes(fault, normal) == ("fault_cleared",)
    assert changes(fault, fault) == ()
    assert changes(fault, {}) == ()

def test_sink_disabled(monkeypatch):
    monkeypatch.delenv("GWAY_ACTIVITY_FEED_EXTERNAL", raising=False)
    runner = Mock()
    assert not enqueue("reports.fault_observed", "Charger fault observed", "warning",
                       "2026-11-24T10:00:00Z", repository="arthexis/ocpp-reports",
                       category="ocpp", run=runner)
    runner.assert_not_called()
