import pytest
import httpx
from ocpp_reports.forwarder_config import load_forwarder_config
from ocpp_reports import forwarder


def test_forwarder_toml(tmp_path):
    p = tmp_path / "forwarder.toml"
    p.write_text('[forwarder]\nsource_id = "sat-001"\nendpoint = "https://collector.example/api/replication"\ntoken = "secret"\n')
    assert load_forwarder_config(p)["token"] == "secret"


@pytest.mark.parametrize("body", ["", "[forwarder]\n", '[forwarder]\nsource_id="x"\nendpoint="http://remote/"\ntoken="s"\n'])
def test_invalid_forwarder_toml(tmp_path, body):
    p = tmp_path / "forwarder.toml"
    p.write_text(body)
    with pytest.raises(ValueError):
        load_forwarder_config(p)


def test_unreachable_collector_uses_capped_backoff(monkeypatch, tmp_path):
    delays = []
    monkeypatch.setattr(forwarder, "confirmed_cursor", lambda *args: 0)
    attempts = [0]
    def unavailable(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] <= 8:
            raise httpx.ConnectError("offline")
        return 0
    monkeypatch.setattr(forwarder, "forward_once", unavailable)
    def sleep(delay):
        delays.append(delay)
        if len(delays) == 9:
            raise StopIteration
    with pytest.raises(StopIteration):
        forwarder.forward(tmp_path/"warehouse.sqlite", tmp_path/"cursor.sqlite",
                          "sat-001", "https://collector.example", "secret", sleep=sleep)
    assert delays[:8] == [5, 10, 20, 40, 80, 160, 300, 300]
    assert delays[8] == 2
