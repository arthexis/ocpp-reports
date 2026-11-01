"""Recent transactions render bounded 20-row pages with stable navigation."""
from copy import deepcopy
from fastapi.testclient import TestClient

from ocpp_reports import web


def _data():
    # 54 eligible and 1 failed, newest first.
    transactions = [
        {"source_id": "a", "charger_id": "cp", "location": "Site",
         "id": n, "state": "stopped", "started_at": f"2026-10-30T12:00:00Z",
         "stopped_at": f"2026-10-30T12:00:00Z",
         "energy_wh": 1000 if n != 50 else 0}
        for n in range(55, 0, -1)
    ]
    return {"sources": [], "connectors": [], "transactions": transactions, "events": []}


def test_pages_are_20_rows_with_numbered_prev_next(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(_data()))
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("a",)))
    first = client.get("/?tx_view=all")
    assert first.status_code == 200
    assert first.text.count('<tr><td>Site</td><td>cp</td>') == 20
    assert 'Mostrando 1–20 de 55' in first.text
    assert 'aria-current="page">1</span>' in first.text
    assert 'tx_page=2' in first.text and 'tx_page=3' in first.text
    assert 'rel="next"' in first.text
    assert 'rel="prev"' not in first.text
    second = client.get("/?tx_view=all&tx_page=2")
    assert second.status_code == 200
    assert second.text.count('<tr><td>Site</td><td>cp</td>') == 20
    assert 'Mostrando 21–40 de 55' in second.text
    assert 'aria-current="page">2</span>' in second.text
    assert 'rel="prev"' in second.text and 'rel="next"' in second.text
    final = client.get("/?tx_view=all&tx_page=3")
    assert 'Mostrando 41–55 de 55' in final.text
    assert final.text.count('<tr><td>Site</td><td>cp</td>') == 15
    assert 'rel="next"' not in final.text
    assert 'tx_page=2' in final.text
    assert 'Mostrando 41–55' in client.get("/?tx_view=all&tx_page=9999").text
    assert client.get("/?tx_page=0").status_code == 422
    assert client.get("/?tx_page=-5").status_code == 422


def test_tabs_preserve_counts_but_page_after_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(_data()))
    client = TestClient(web.create_app(tmp_path / "unused"))
    eligible = client.get("/")
    assert eligible.status_code == 200
    assert 'Exitosas (54)' in eligible.text
    assert 'Fallidas (1)' in eligible.text
    assert 'Todas (55)' in eligible.text
    assert 'Mostrando 1–20 de 54' in eligible.text
    assert 'next.searchParams.delete("tx_page")' in eligible.text
    failed = client.get("/?tx_view=failed&tx_page=3")
    assert failed.status_code == 200
    assert 'Mostrando 1–1 de 1' in failed.text
    assert 'aria-current="page">1</span>' in failed.text
    assert '>50</td>' in failed.text
    assert 'rel="next"' not in failed.text


def test_links_keep_source_filters_and_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: deepcopy(_data()))
    client = TestClient(web.create_app(tmp_path / "unused", allowed_source_ids=("a",),
                                       timezones=("America/Monterrey", "UTC")))
    response = client.get("/?source_id=a&tz=UTC&tx_view=all&tx_page=2")
    assert response.status_code == 200
    assert 'source_id=a' in response.text
    assert 'tx_view=all' in response.text
    assert 'tz=UTC' in response.text
    assert 'tx_page=3' in response.text
    assert client.get("/?source_id=forbidden&tx_page=2").status_code == 403
    assert len(client.get("/api/snapshot?limit=55").json()["transactions"]) == 55


def test_empty_page_is_stable(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": []})
    client = TestClient(web.create_app(tmp_path / "unused"))
    html = client.get("/?tx_page=12").text
    assert "Sin transacciones" in html
    assert "Mostrando 0–0 de 0" in html
    assert 'aria-current="page">1</span>' in html
