import sqlite3

from fastapi.testclient import TestClient

from ocpp_reports.replication.collector import create_collector


def test_ready_auth_and_zero_writes(tmp_path):
    database = tmp_path / "collector.sqlite3"
    client = TestClient(create_collector(database, {"node-a": "secret-a", "node-b": "secret-b"}))
    route = "/api/replication/ready"
    query = {"source_id": "node-a"}
    assert client.post(route, json=query).status_code == 401
    assert client.post(route, json=query, headers={"Authorization": "Bearer secret-b"}).status_code == 401
    response = client.post(route, json=query, headers={"Authorization": "Bearer secret-a"})
    assert response.status_code == 200
    assert response.json() == {
        "schema": "ocpp-reports/readiness/v1", "source_id": "node-a",
        "ready": True, "held": False, "cursor": 0,
    }
    assert not database.exists()
    assert client.post(route, json=query, headers={"Authorization": "Bearer secret-a"}).status_code == 200
    assert not database.exists()


def test_ready_is_bound_to_loaded_credentials(tmp_path):
    credentials = {"node-a": "old-secret"}
    client = TestClient(create_collector(tmp_path / "collector.sqlite3", credentials.copy()))
    credentials["node-a"] = "new-secret"  # mirrors disk config changing, not app startup
    route = "/api/replication/ready"
    assert client.post(route, json={"source_id": "node-a"},
                       headers={"Authorization": "Bearer new-secret"}).status_code == 401
    assert client.post(route, json={"source_id": "node-a"},
                       headers={"Authorization": "Bearer old-secret"}).status_code == 200


def test_held_readiness_does_not_modify_state(tmp_path):
    database = tmp_path / "collector.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE collector_cursors (source_id TEXT, sequence INTEGER)")
        connection.execute("CREATE TABLE collector_source_holds (source_id TEXT)")
        connection.execute("INSERT INTO collector_cursors VALUES ('node-a',15)")
        connection.execute("INSERT INTO collector_source_holds VALUES ('node-a')")
    client = TestClient(create_collector(database, {"node-a": "secret"}))
    with sqlite3.connect(database) as connection:
        before = connection.total_changes
    response = client.post("/api/replication/ready", json={"source_id": "node-a"},
                           headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT sequence FROM collector_cursors").fetchone()[0] == 15
        assert connection.execute("SELECT COUNT(*) FROM collector_source_holds").fetchone()[0] == 1
