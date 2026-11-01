"""Read-only, in-process dashboard latency benchmark.

Measures actual FastAPI route handlers, without requiring a bound port, HTTP
credentials, or write access to the reports warehouse.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
import tracemalloc
from pathlib import Path

from .warehouse.queries import snapshot


def _percentile(values: list[float], percentile: float) -> float:
    """Nearest-rank percentile, including the observed maximum for p100."""
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * percentile / 100) - 1)]


def _stats(samples: list[float]) -> dict:
    return {
        "iterations": len(samples),
        "p50_ms": round(_percentile(samples, 50), 3),
        "p95_ms": round(_percentile(samples, 95), 3),
        "p99_ms": round(_percentile(samples, 99), 3),
        "max_ms": round(max(samples), 3),
        "mean_ms": round(statistics.mean(samples), 3),
    }


def _inventory(path: Path) -> dict:
    # SQLite URI mode=ro prevents accidental creation of a missing database.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        counts = {}
        for table in ("sources", "connector_state", "current_transactions",
                      "events", "collector_latest"):
            if table in tables:
                counts[table] = db.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
        return {"size_bytes": path.stat().st_size, "row_counts": counts}


def benchmark(database: Path, *, iterations: int = 100, warmup: int = 5) -> dict:
    if iterations < 1 or iterations > 10000:
        raise ValueError("iterations must be between 1 and 10000")
    if warmup < 0 or warmup > 1000:
        raise ValueError("warmup must be between 0 and 1000")
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Warehouse not found: {path}")
    # Import optional web dependencies only if benchmarking is requested.
    try:
        from fastapi.testclient import TestClient
        from .web import create_app
    except ImportError as exc:
        raise RuntimeError("Dashboard benchmark requires ocpp-reports[web,benchmark] (httpx2)") from exc

    inventory = _inventory(path)
    app = create_app(path)
    scenarios = {
        "dashboard": "/",
        "transactions_page_2": "/?tx_page=2",
        "charger_filter": "/?connector_view=station",
        "api_snapshot": "/api/snapshot",
    }
    results = {}
    with TestClient(app) as client:
        for name, url in scenarios.items():
            def execute() -> None:
                response = client.get(url)
                if response.status_code != 200:
                    raise RuntimeError(f"{name}: HTTP {response.status_code}: {response.text[:160]}")
            for _ in range(warmup):
                execute()
            samples = []
            for _ in range(iterations):
                started = time.perf_counter()
                execute()
                samples.append((time.perf_counter() - started) * 1000)
            # Memory measurement is separate: tracemalloc materially alters timings.
            tracemalloc.start()
            try:
                execute()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            results[name] = {**_stats(samples), "peak_python_bytes": peak}

    def query() -> None:
        snapshot(path, transaction_pagination={
            "term": "", "status": "", "location": "", "date_from": "",
            "date_to": "", "zone": "UTC", "view": "success", "page": 1,
        }, skip_events=True)

    for _ in range(warmup):
        query()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        query()
        samples.append((time.perf_counter() - started) * 1000)
    results["snapshot_query"] = _stats(samples)
    return {
        "schema": "ocpp-reports/benchmark/v1",
        "database": str(path),
        "warehouse": inventory,
        "iterations": iterations,
        "warmup": warmup,
        "scenarios": results,
        "method": "in-process FastAPI TestClient; sequential; no network; read-only",
    }


def write_result(result: dict, output: Path) -> None:
    """Create a result file without clobbering prior measurements."""
    target = Path(output).expanduser()
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
