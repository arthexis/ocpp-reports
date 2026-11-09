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



def test_v1_and_legacy_event_history_deny_operator_before_warehouse(monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []

    def forbidden_details(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("operator must not query event history")

    monkeypatch.setattr(web, "event_group_details", forbidden_details)
    app = web.create_app(tmp_path / "unused.sqlite",
                         allowed_source_ids=("tenant-a",))

    @app.middleware("http")
    async def operator_identity(request, call_next):
        request.state.principal = SimpleNamespace(
            role=SimpleNamespace(value="operator"))
        return await call_next(request)

    client = TestClient(app)
    for path in ("/api/v1/event-group", "/api/event-group"):
        for source in ("tenant-a", "tenant-b"):
            response = client.get(path, params={
                "group_source": source, "first_event_id": 11})
            assert response.status_code == 403
            assert "events" not in response.json()
    assert calls == []


def test_v1_event_history_denies_cross_tenant_admin_before_query(monkeypatch, tmp_path):
    calls = []

    def forbidden_details(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("cross-tenant event detail must not query warehouse")

    monkeypatch.setattr(web, "event_group_details", forbidden_details)
    client = TestClient(web.create_app(
        tmp_path / "unused.sqlite", allowed_source_ids=("tenant-a",)))
    for path in ("/api/v1/event-group", "/api/event-group"):
        response = client.get(path, params={
            "group_source": "tenant-b", "first_event_id": 11})
        assert response.status_code == 403
    assert calls == []



def test_v1_and_html_share_timezone_and_date_window_validation(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, **kwargs):
        calls.append(kwargs)
        return {
            "connectors": [], "transactions": [], "events": [], "sources": [],
            "transaction_pagination": {
                "total": 0, "page": 1, "pages": 1,
                "counts": {"all": 0, "success": 0, "failed": 0}},
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    client = TestClient(web.create_app(
        tmp_path / "unused.sqlite", timezones=("UTC", "America/Monterrey")))
    invalid = [
        {"tz": "Europe/Berlin"},
        {"date_from": "2026-02-30"},
        {"date_from": "2026-11-09", "date_to": "2026-11-08"},
    ]
    for params in invalid:
        for path in ("/", "/api/v1/connectors", "/api/v1/transactions"):
            assert client.get(path, params=params).status_code == 422
    assert calls == []


def test_dashboard_dst_display_uses_instant_not_naive_wall_clock():
    # DST fallback in New York: two UTC instants map to the same local hour.
    first = web.display_date("2026-11-01T05:30:00Z", "America/New_York")
    second = web.display_date("2026-11-01T06:30:00Z", "America/New_York")
    assert first.startswith("2026-11-01 01:30")
    assert second.startswith("2026-11-01 01:30")
    assert web.display_date("2026-11-01T06:30:00Z", "UTC").startswith(
        "2026-11-01 06:30")



def test_v1_event_groups_share_html_sql_pagination_and_tenant_scope(monkeypatch, tmp_path):
    calls = []

    def fake_groups(path, **kwargs):
        calls.append(kwargs)
        return {"groups": [{"source_id": "tenant-a", "first_event_id": 42, "event_count": 2}],
                "page": kwargs["page"], "has_next": True}

    monkeypatch.setattr(web, "event_groups_page", fake_groups)
    client = TestClient(web.create_app(
        tmp_path / "unused.sqlite", allowed_source_ids=("tenant-a",),
        timezones=("UTC", "America/Monterrey")))
    params = {"source_id": "tenant-a", "page": 2, "filter": "CP1",
              "date_from": "2026-11-01", "date_to": "2026-11-09",
              "tz": "America/Monterrey"}
    response = client.get("/api/v1/event-groups", params=params)
    assert response.status_code == 200
    assert response.json()["groups"][0]["first_event_id"] == 42
    assert response.json()["page"] == 2
    assert response.json()["has_next"] is True
    schema = client.get("/api/v1/openapi.json").json()
    response_schema = schema["paths"]["/api/v1/event-groups"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["$ref"].endswith("/EventGroupsResponse")
    assert calls[0]["allowed_source_ids"] == ("tenant-a",)
    assert calls[0]["term"] == "CP1"
    assert calls[0]["zone"] == "America/Monterrey"
    assert client.get("/api/v1/event-groups", params={
        "source_id": "tenant-b"}).status_code == 403
    assert client.get("/api/v1/event-groups", params={
        "page": 0}).status_code == 422
    assert len(calls) == 1


def test_v1_event_groups_operator_denied_before_sql(monkeypatch, tmp_path):
    from types import SimpleNamespace

    def forbidden(*args, **kwargs):
        raise AssertionError("operator must not query event groups")

    monkeypatch.setattr(web, "event_groups_page", forbidden)
    app = web.create_app(tmp_path / "unused.sqlite")

    @app.middleware("http")
    async def operator_identity(request, call_next):
        request.state.principal = SimpleNamespace(
            role=SimpleNamespace(value="operator"))
        return await call_next(request)

    assert TestClient(app).get("/api/v1/event-groups").status_code == 403
    from ocpp_reports.dashboard_login import route_permission
    assert route_permission("/api/v1/event-groups", "GET") is not None
    assert route_permission("/api/v1/event-groups", "POST") is None



def test_v1_openapi_declares_typed_operational_station_and_transaction_counts(tmp_path):
    client = TestClient(web.create_app(tmp_path / "unused.sqlite"))
    schema = client.get("/api/v1/openapi.json")
    assert schema.status_code == 200
    components = schema.json()["components"]["schemas"]
    assert components["SummaryResponse"]["properties"]["status_counts"]
    assert components["OperationalCount"]["properties"]["stations"]["type"] == "integer"
    assert components["OperationalCount"]["properties"]["connectors"]["type"] == "integer"
    assert components["TransactionCounts"]["properties"]["success"]["type"] == "integer"
    assert components["TransactionCounts"]["properties"]["failed"]["type"] == "integer"
    assert components["StationItem"]["properties"]["source_id"]["type"] == "string"
    assert components["StationsResponse"]["properties"]["items"]["items"]["$ref"].endswith("/StationItem")
    assert components["ConnectorsResponse"]["properties"]["items"]["items"]["$ref"].endswith("/ConnectorItem")
    assert components["TransactionsResponse"]["properties"]["items"]["items"]["$ref"].endswith("/TransactionItem")
    assert components["ConnectorItem"]["properties"]["connector_id"]["type"] == "integer"
    assert components["TransactionItem"]["properties"]["source_id"]["type"] == "string"



def test_html_event_panel_and_api_v1_share_group_query(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot(path, **kwargs):
        return {
            "connectors": [], "transactions": [], "events": [], "sources": [],
            "transaction_pagination": {
                "total": 0, "page": 1, "pages": 1,
                "counts": {"all": 0, "success": 0, "failed": 0}},
        }

    def fake_groups(path, **kwargs):
        calls.append(kwargs)
        return {"groups": [{"source_id": "tenant-a", "charger_id": "CP1",
                            "location": "North", "action": "Heartbeat",
                            "at": "2026-11-09T12:00:00Z",
                            "event_count": 3, "first_event_id": 42}],
                "page": kwargs["page"], "has_next": True}

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    monkeypatch.setattr(web, "event_groups_page", fake_groups)
    client = TestClient(web.create_app(
        tmp_path / "unused.sqlite", allowed_source_ids=("tenant-a",)))
    html = client.get("/", params={"source_id": "tenant-a", "event_page": 2})
    api = client.get("/api/v1/event-groups", params={
        "source_id": "tenant-a", "page": 2})
    assert html.status_code == 200
    assert api.status_code == 200
    assert 'data-first-id="42"' in html.text
    assert "Heartbeat" in html.text
    assert api.json()["groups"][0]["first_event_id"] == 42
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]["page"] == 2
    assert calls[0]["allowed_source_ids"] == ("tenant-a",)



def test_dashboard_event_fetch_contract_and_partial_refresh(monkeypatch, tmp_path):
    """Guard the actual browser fetch/DOM contract (the dashboard does not use htmx)."""
    def fake_snapshot(path, **kwargs):
        return {
            "connectors": [], "transactions": [], "events": [], "sources": [],
            "transaction_pagination": {
                "total": 0, "page": 1, "pages": 1,
                "counts": {"all": 0, "success": 0, "failed": 0}},
        }

    def fake_groups(path, **kwargs):
        return {"groups": [{"source_id": "tenant-a", "charger_id": "CP1",
                            "location": "North", "action": "Heartbeat",
                            "at": "2026-11-09T12:00:00Z",
                            "event_count": 2, "first_event_id": 42}],
                "page": 1, "has_next": False}

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    monkeypatch.setattr(web, "event_groups_page", fake_groups)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite",
                                       allowed_source_ids=("tenant-a",)))
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'data-source="tenant-a"' in html
    assert 'data-first-id="42"' in html
    assert 'new URL("/api/event-group", window.location.origin)' in html
    assert 'url.searchParams.set("group_source", details.dataset.source)' in html
    assert 'url.searchParams.set("first_event_id", details.dataset.firstId)' in html
    assert 'url.searchParams.set("offset", String(offset))' in html
    assert 'fetch(url, {credentials: "same-origin"})' in html
    assert 'holder.replaceChildren(wrapper, status, more)' in html
    assert 'more.hidden = exhausted' in html
    assert 'details.addEventListener("toggle"' in html



def test_api_v1_invalid_connector_view_is_rejected(monkeypatch, tmp_path):
    """Invalid view must be a 422 before any warehouse access."""
    def forbidden_snapshot(path, **kwargs):
        raise AssertionError("Invalid connector view must not query warehouse")

    monkeypatch.setattr(web, "snapshot", forbidden_snapshot)
    client = TestClient(web.create_app(tmp_path / "unused.sqlite"))
    response = client.get("/api/v1/connectors", params={"connector_view": "invalid"})
    assert response.status_code == 422



def test_dashboard_poll_preserves_active_controls_and_expanded_events(monkeypatch, tmp_path):
    """Template contract for non-destructive fetch refresh (not browser execution)."""
    def fake_snapshot(path, **kwargs):
        return {
            "connectors": [], "transactions": [], "events": [], "sources": [],
            "transaction_pagination": {
                "total": 0, "page": 1, "pages": 1,
                "counts": {"all": 0, "success": 0, "failed": 0}},
        }

    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    monkeypatch.setattr(web, "event_groups_page", lambda path, **kwargs: {
        "groups": [], "page": kwargs["page"], "has_next": False})
    client = TestClient(web.create_app(tmp_path / "unused.sqlite"))
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'credentials: "same-origin", cache: "no-store"' in html
    assert 'headers: {"Accept": "text/html"}' in html
    assert 'new DOMParser().parseFromString(await response.text(), "text/html")' in html
    assert '!current.contains(document.activeElement)' in html
    assert '!eventRegion.querySelector("details[open]")' in html
    assert '!eventRegion.contains(document.activeElement)' in html
    assert 'setupEventGroups(eventRegion)' in html
    assert 'window.setInterval(refreshDashboard, 15000)' in html
    assert 'window.addEventListener("pagehide"' in html


def test_integrated_warehouse_html_api_parity_without_mocks(integrated_dashboard_warehouse):
    """Exercise actual SQLite queries, source isolation and date validation."""
    client = TestClient(web.create_app(
        integrated_dashboard_warehouse,
        allowed_source_ids=("tenant-a",),
        timezones=("UTC", "America/New_York")))
    html = client.get("/")
    groups = client.get("/api/v1/event-groups")
    assert html.status_code == 200
    assert groups.status_code == 200
    assert groups.json()["groups"]
    assert all(g["source_id"] == "tenant-a" for g in groups.json()["groups"])
    for group in groups.json()["groups"]:
        assert 'data-first-id="' + str(group["first_event_id"]) + '"' in html.text
    assert client.get("/api/v1/event-groups", params={
        "source_id": "tenant-b"}).status_code == 403
    assert client.get("/", params={"source_id": "tenant-b"}).status_code == 403
    assert client.get("/api/v1/event-groups", params={
        "tz": "Europe/Berlin"}).status_code == 422
    assert client.get("/api/v1/transactions", params={
        "date_from": "2026-11-02", "date_to": "2026-11-01"}).status_code == 422
    second = client.get("/api/v1/transactions", params={"page": 2})
    assert second.status_code == 200
    assert second.json()["total"] == 45
    assert second.json()["pages"] == 3
    assert second.json()["page"] == 2
    assert len(second.json()["items"]) == 20
    assert all(row["source_id"] == "tenant-a" for row in second.json()["items"])
    assert client.get("/api/v1/transactions", params={
        "source_id": "tenant-b", "page": 2}).status_code == 403
    assert client.get("/api/v1/transactions", params={
        "view": "failed"}).json()["total"] == 4
    combined = client.get("/api/v1/connectors")
    assert combined.status_code == 200
    assert {item["connector_id"] for item in combined.json()["items"]} == {0, 1}
    assert all(item["source_id"] == "tenant-a" for item in combined.json()["items"])
    station = client.get("/api/v1/connectors", params={"connector_view": "station"})
    individual = client.get("/api/v1/connectors", params={"connector_view": "individual"})
    assert [item["connector_id"] for item in station.json()["items"]] == [0]
    assert [item["connector_id"] for item in individual.json()["items"]] == [1]
    assert client.get("/api/v1/connectors", params={
        "source_id": "tenant-b"}).status_code == 403


def test_shared_warehouse_dst_fall_back_dates(integrated_dashboard_warehouse):
    """Two UTC instants in the repeated New York hour share one local date."""
    import json
    import sqlite3

    with sqlite3.connect(integrated_dashboard_warehouse) as db:
        for n, started in ((1001, "2026-11-01T05:30:00Z"),
                           (1002, "2026-11-01T06:30:00Z")):
            payload = {
                "id": n, "charger_id": "CP-tenant-a", "state": "closed",
                "active": False, "started_at": started, "stopped_at": started,
                "energy_wh": 1000,
            }
            db.execute(
                "INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                ("tenant-a", "CP-tenant-a", n, 0, 0, "dst",
                 json.dumps(payload), started),
            )
    client = TestClient(web.create_app(
        integrated_dashboard_warehouse,
        allowed_source_ids=("tenant-a",),
        timezones=("UTC", "America/New_York")))
    response = client.get("/api/v1/transactions", params={
        "tz": "America/New_York", "date_from": "2026-11-01",
        "date_to": "2026-11-01"})
    assert response.status_code == 200
    assert response.json()["total"] == 47
    assert client.get("/api/v1/transactions", params={
        "tz": "Europe/Berlin", "date_from": "2026-11-01"}).status_code == 422


def test_integrated_connector_projection_and_html_semantics_without_mocks(
        integrated_dashboard_warehouse):
    """Use one real SQLite warehouse for the HTML and JSON connector contracts."""
    client = TestClient(web.create_app(
        integrated_dashboard_warehouse, allowed_source_ids=("tenant-a",)))
    html = client.get("/")
    response = client.get("/api/v1/connectors")
    assert html.status_code == response.status_code == 200
    connectors = response.json()["items"]
    assert {item["connector_id"] for item in connectors} == {0, 1}
    for item in connectors:
        assert item["source_id"] == "tenant-a"
        for field in ("status", "source_freshness", "activity_freshness",
                      "connection_fresh", "status_time_unknown", "stale",
                      "power_w", "energy_wh"):
            assert field in item, (field, item)
        assert item["source_freshness"] in ("fresh", "old", "unknown")
        assert item["activity_freshness"] in ("fresh", "old", "unknown")
        assert isinstance(item["stale"], bool)
        assert isinstance(item["connection_fresh"], bool)
        assert isinstance(item["status_time_unknown"], bool)
        if item["power_w"] is None:
            assert "—" in html.text
        if item["energy_wh"] is not None:
            assert "%.2f" % (item["energy_wh"] / 1000) in html.text
    station_items = client.get("/api/v1/stations").json()["items"]
    station_connectors = [row for station in station_items for row in station["connectors"]]
    assert {(r["source_id"], r["charger_id"], r["connector_id"])
            for r in station_connectors} == {
                (r["source_id"], r["charger_id"], r["connector_id"])
                for r in connectors}
    for station_row in station_connectors:
        peer = next(r for r in connectors if
                    (r["source_id"], r["charger_id"], r["connector_id"]) ==
                    (station_row["source_id"], station_row["charger_id"],
                     station_row["connector_id"]))
        for field in ("status", "source_freshness", "activity_freshness",
                      "connection_fresh", "status_time_unknown", "stale",
                      "power_w", "energy_wh", "connected"):
            assert station_row[field] == peer[field], field
    assert "Potencia" in html.text and "Medidor" in html.text
    assert client.get("/api/v1/connectors", params={
        "source_id": "tenant-b"}).status_code == 403


def test_real_sqlite_openapi_schema_and_unknown_meter_values(
        integrated_dashboard_warehouse):
    """Validate published API schema and unknown values against actual warehouse data."""
    client = TestClient(web.create_app(
        integrated_dashboard_warehouse, allowed_source_ids=("tenant-a",)))
    spec_response = client.get("/api/v1/openapi.json")
    assert spec_response.status_code == 200
    assert spec_response.headers["cache-control"] == "no-store"
    spec = spec_response.json()
    for endpoint in ("/api/v1/summary", "/api/v1/connectors",
                     "/api/v1/stations", "/api/v1/filters",
                     "/api/v1/transactions", "/api/v1/sources",
                     "/api/v1/event-groups"):
        assert "get" in spec["paths"][endpoint], endpoint
        assert "200" in spec["paths"][endpoint]["get"]["responses"], endpoint
    schemas = spec["components"]["schemas"]
    assert "ConnectorsResponse" in schemas
    assert "TransactionsResponse" in schemas
    assert "SummaryResponse" in schemas
    rows = client.get("/api/v1/connectors").json()["items"]
    assert len(rows) == 2
    assert all(row["power_w"] is None for row in rows)
    assert next(row for row in rows if row["connector_id"] == 0)["energy_wh"] is None
    assert next(row for row in rows if row["connector_id"] == 1)["energy_wh"] == 1500
    assert all(row["source_freshness"] == "unknown" for row in rows)
    assert all(row["status_time_unknown"] is False for row in rows)
