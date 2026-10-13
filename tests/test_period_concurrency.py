"""B5.3 concurrent period reservations and crash-recovery behavior."""
from concurrent.futures import ThreadPoolExecutor
from ocpp_reports.outbox import Outbox


def test_concurrent_reservations_only_create_one_delivery(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    def reserve(_):
        return Outbox(path).enqueue_period(period_key="operator|source|start|end",
            recipient="operator@example.org", subject="report", body="body", revision="r1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(16)))
    assert sum(created for _, created in results) == 1
    assert len({key for key, _ in results}) == 1
    assert len(Outbox(path).list()) == 1


def test_interrupted_smtp_claim_requires_manual_retry(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    outbox = Outbox(path)
    key, created = outbox.enqueue_period(period_key="p", recipient="operator@example.org",
        subject="report", body="body", revision="r1")
    assert created
    assert outbox.claim().key == key
    assert outbox.claim() is None
    restarted = Outbox(path)
    assert restarted.period_status("p")["state"] == "sending"
    assert restarted.enqueue_period(period_key="p", recipient="operator@example.org",
        subject="report", body="body", revision="r1") == (key, False)
    assert restarted.claim() is None
    restarted.retry(key)
    assert restarted.claim().key == key
    assert restarted.period_status("p")["attempts"] == 2
