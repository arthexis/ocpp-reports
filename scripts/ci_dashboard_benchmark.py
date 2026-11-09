"""Benchmark synthetic read-only warehouse in CI, never production."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from ocpp_reports.dashboard_benchmark import benchmark
from ocpp_reports.warehouse.schema import _schema
from ocpp_reports.warehouse.collector_schema import ensure_collector_schema


def main():
    output = Path(os.environ.get("BENCHMARK_ARTIFACT", "dashboard-benchmark.json"))
    with tempfile.TemporaryDirectory(prefix="dashboard-ci-") as directory:
        path = Path(directory) / "warehouse.sqlite"
        with sqlite3.connect(path) as db:
            _schema(db)
            ensure_collector_schema(db)
            for source in ("a", "b", "c"):
                db.execute("INSERT INTO sources(source_id,cursor) VALUES (?,0)", (source,))
                for charger in range(10):
                    for connector in (0, 1):
                        payload = {"charger_id": f"cp-{charger}", "connector_id": connector,
                                   "status": "Available", "connected": True,
                                   "observed_at": "2026-11-01T06:30:00Z"}
                        db.execute("INSERT INTO collector_latest VALUES (?,?,?,?,?)",
                                   (source, "connector", f"cp-{charger}:{connector}",
                                    connector + 1, json.dumps(payload)))
                for n in range(1, 1001):
                    tx = {"id": n, "charger_id": f"cp-{n % 10}", "state": "closed",
                          "active": False, "started_at": "2026-11-01T05:30:00Z",
                          "stopped_at": "2026-11-01T06:30:00Z",
                          "energy_wh": 0 if n % 10 == 0 else 1000}
                    db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                               (source, tx["charger_id"], n, 0, 0, "ci",
                                json.dumps(tx), "2026-11-01T06:30:00Z"))
                for n in range(1, 2001):
                    event = {"id": n, "kind": "ocpp", "charger_id": f"cp-{n % 10}",
                             "action": "Heartbeat", "at": "2026-11-01T06:30:00Z"}
                    db.execute("INSERT INTO events VALUES (?,?,?)",
                               (source, n, json.dumps(event)))
        before = path.read_bytes()
        result = benchmark(path, iterations=100, warmup=5)
        assert path.read_bytes() == before, "Benchmark modified warehouse"
        result["synthetic_fixture"] = {"sources": 3, "connectors": 60,
                                       "transactions": 3000, "events": 6000}
        output.write_text(json.dumps(result, indent=2) + "\n")
        slow = {name: row["p95_ms"] for name, row in result["scenarios"].items()
                if row["p95_ms"] >= 1000}
        print(json.dumps({"p95_ms": {name: row["p95_ms"] for name, row
                                   in result["scenarios"].items()}, "slow": slow}))
        if slow:
            raise SystemExit("Synthetic dashboard p95 >= 1000ms")


if __name__ == "__main__":
    main()
