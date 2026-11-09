"""Contract checks for versioned dashboard read endpoints."""
from fastapi.testclient import TestClient
from ocpp_reports import web


def test_v1_routes_and_source_allowlist(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, *, source_id=None, charger_id=None, limit=100,
                      allowed_source_ids=None, **kwargs):
        calls.append((source_id, allowed_source_ids))
        return {
            "transaction_pagination": {"total": 1, "page": 1, "pages": 1,
                                       "counts": {"all": 1, "success": 1, "failed": 0}} if kwargs.get("transaction_pagination") else None,
            "connectors": [{"source_id": "tenant-a", "charger_id": "CP1",
                            "connector_id": 1, "status": "Available", "location": "North"}],
            "transactions": [{"source_id": "tenant-a", "charger_id": "CP1",
                              "energy_wh": 1200}],
            "sources": [], "events": [],
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    app = web.create_app(tmp_path / "unused.sqlite", allowed_source_ids=("tenant-a",))
    client = TestClient(app)
    assert client.get("/api/v1/connectors").json()["items"][0]["charger_id"] == "CP1"
    assert client.get("/api/v1/stations").json()["items"][0]["charger_id"] == "CP1"
    assert client.get("/api/v1/filters").json()["source_ids"] == ["tenant-a"]
    assert client.get("/api/v1/transactions", params={"view": "success"}).status_code == 200
    assert client.get("/api/v1/summary").json()["status_counts"]["available"]["stations"] == 1
    assert client.get("/api/v1/openapi.json").json()["paths"]["/api/v1/connectors"]
    assert client.get("/api/v1/connectors", params={"source_id": "tenant-b"}).status_code == 403
    assert all(allowlist == ("tenant-a",) for _, allowlist in calls)
    assert client.post("/api/v1/connectors").status_code == 405


def test_v1_connector_view_and_tenant_isolation(monkeypatch, tmp_path):
    def fake_snapshot(path, *, source_id=None, charger_id=None, limit=100,
                      allowed_source_ids=None, **kwargs):
        assert allowed_source_ids == ("tenant-a",)
        return {
            "connectors": [
                {"source_id": "tenant-a", "charger_id": "CP1",
                 "connector_id": 0, "status": "Available", "connected": 1},
                {"source_id": "tenant-a", "charger_id": "CP1",
                 "connector_id": 1, "status": "Charging", "connected": 1},
            ],
            "transactions": [], "sources": [], "events": [],
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite",
                                       allowed_source_ids=("tenant-a",)))
    combined = client.get("/api/v1/connectors").json()["items"]
    station = client.get("/api/v1/connectors",
                         params={"connector_view": "station"}).json()["items"]
    individual = client.get("/api/v1/connectors",
                            params={"connector_view": "individual"}).json()["items"]
    assert len(combined) == 2
    assert [row["connector_id"] for row in station] == [0]
    assert [row["connector_id"] for row in individual] == [1]
    assert all("stale" in row and "source_freshness" in row for row in combined)
    assert client.get("/api/v1/connectors",
                      params={"connector_view": "bad"}).status_code == 422
    assert client.get("/api/v1/connectors",
                      params={"source_id": "tenant-b"}).status_code == 403


def test_v1_transactions_share_dashboard_sql_filter_contract(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, *, allowed_source_ids=None, **kwargs):
        calls.append(kwargs.get("transaction_pagination"))
        return {
            "connectors": [], "sources": [], "events": [], "transactions": [],
            "transaction_pagination": {"total": 0, "page": 2, "pages": 2,
                                       "counts": {"all": 0, "success": 0, "failed": 0}},
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    app = web.create_app(tmp_path / "unused.sqlite", timezones=("UTC", "America/Monterrey"))
    client = TestClient(app)
    params = {"source_id": "site-a", "charger_id": "CP1", "view": "failed",
              "page": 2, "filter": "CP1", "status": "Faulted",
              "location": "North", "date_from": "2026-11-01",
              "date_to": "2026-11-08", "tz": "America/Monterrey"}
    assert client.get("/api/v1/transactions", params=params).status_code == 200
    assert calls[-1] == {"term": "CP1", "status": "Faulted", "location": "North",
                         "date_from": "2026-11-01", "date_to": "2026-11-08",
                         "zone": "America/Monterrey", "view": "failed", "page": 2}
    assert client.get("/api/v1/transactions", params={
        "tz": "Europe/Berlin"}).status_code == 422
    assert client.get("/api/v1/transactions", params={
        "date_from": "2026-11-09", "date_to": "2026-11-08"}).status_code == 422
    assert client.get("/api/v1/transactions", params={
        "date_from": "not-a-date"}).status_code == 422


def test_v1_transaction_detail_matches_html_and_hides_cross_tenant(monkeypatch, tmp_path):
    calls = []

    def fake_detail(path, *, source_id, charger_id, transaction_id,
                    allowed_source_ids=None):
        calls.append((source_id, charger_id, transaction_id, allowed_source_ids))
        return {"source_id": source_id, "charger_id": charger_id,
                "transaction_id": transaction_id, "energy_wh": 1500}

    monkeypatch.setattr(web, "transaction_detail", fake_detail)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite",
                                       allowed_source_ids=("tenant-a",),
                                       timezones=("UTC", "America/Monterrey")))
    url = "/api/v1/transactions/tenant-a/CP1/42"
    result = client.get(url, params={"tz": "America/Monterrey"})
    assert result.status_code == 200
    assert result.json()["transaction"]["energy_wh"] == 1500
    assert result.json()["timezone"] == "America/Monterrey"
    assert calls == [("tenant-a", "CP1", 42, ("tenant-a",))]
    assert client.get("/api/v1/transactions/tenant-b/CP1/42").status_code == 404
    assert client.get(url, params={"tz": "Europe/Berlin"}).status_code == 422
    assert len(calls) == 1


def test_v1_event_history_matches_legacy_and_scopes_tenant(monkeypatch, tmp_path):
    calls = []

    def fake_event_details(path, **kwargs):
        calls.append(kwargs)
        return {"events": [{"at": "2026-11-08T12:00:00Z", "event_id": 11}],
                "has_next": False}

    monkeypatch.setattr(web, "event_group_details", fake_event_details)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite",
                                       allowed_source_ids=("tenant-a",)))
    params = {"group_source": "tenant-a", "first_event_id": 11}
    legacy = client.get("/api/event-group", params=params)
    versioned = client.get("/api/v1/event-group", params=params)
    assert legacy.status_code == versioned.status_code == 200
    assert legacy.json()["events"] == versioned.json()["events"]
    assert calls[0]["allowed_source_ids"] == calls[1]["allowed_source_ids"] == ("tenant-a",)
    assert client.get("/api/v1/event-group", params={
        "group_source": "tenant-b", "first_event_id": 11}).status_code == 403
    assert client.get("/api/v1/event-group", params={
        "group_source": "tenant-a", "first_event_id": 0}).status_code == 422
    assert len(calls) == 2


def test_v1_detail_and_event_routes_require_explicit_permissions():
    from ocpp_reports.dashboard_login import route_permission
    assert route_permission("/api/v1/transactions/tenant-a/CP1/42", "GET") is not None
    assert route_permission("/api/v1/event-group", "GET") is not None
    assert route_permission("/api/v1/transactions/tenant-a/CP1/42", "POST") is None
    assert route_permission("/api/v1/transactions/tenant-a/CP1", "GET") is None

def test_v1_sources_freshness_respects_allowlist(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, *, allowed_source_ids=None, **kwargs):
        calls.append(allowed_source_ids)
        return {"sources": [{"source_id": "tenant-a"}],
                "connectors": [], "transactions": [], "events": []}

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite",
                                       allowed_source_ids=("tenant-a",)))
    response = client.get("/api/v1/sources")
    assert response.status_code == 200
    assert response.json()["items"][0]["source_id"] == "tenant-a"
    assert "freshness" in response.json()["items"][0]
    assert client.get("/api/v1/sources", params={"source_id": "tenant-b"}).status_code == 403
    assert calls == [("tenant-a",)]


def test_v1_sources_route_authorized_only_for_admin_diagnostics():
    from ocpp_reports.dashboard_login import route_permission
    assert route_permission("/api/v1/sources", "GET") is not None
    assert route_permission("/api/v1/sources", "POST") is None


def test_v1_sources_deny_non_admin_without_querying_warehouse(monkeypatch, tmp_path):
    from types import SimpleNamespace

    def forbidden_snapshot(*args, **kwargs):
        raise AssertionError("non-admin source diagnostics must not query warehouse")

    monkeypatch.setattr(web, "snapshot", forbidden_snapshot)
    app = web.create_app(tmp_path / "unused.sqlite")

    @app.middleware("http")
    async def fake_operator(request, call_next):
        request.state.principal = SimpleNamespace(role=SimpleNamespace(value="operator"))
        return await call_next(request)

    response = TestClient(app).get("/api/v1/sources")
    assert response.status_code == 403
    assert "items" not in response.json()

def test_v1_connector_filters_preserve_source_freshness(monkeypatch, tmp_path):
    def fake_snapshot(path, **kwargs):
        return {"sources": [{"source_id": "tenant-a"}],
                "connectors": [{"source_id": "tenant-a", "charger_id": "CP1",
                                "connector_id": 1, "status": "Available",
                                "location": "North", "connected": 1}],
                "transactions": [], "events": []}

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite"))
    response = client.get("/api/v1/connectors", params={
        "filter": "CP1", "status": "Available", "location": "North"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["source_freshness"] == "unknown"
    assert client.get("/api/v1/connectors", params={
        "location": "South"}).json()["items"] == []
    assert client.get("/api/v1/connectors", params={
        "date_from": "bad"}).status_code == 422
