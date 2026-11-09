"""Parity tests for the shared dashboard/API connector projection (#195)."""
import copy
import unittest
from datetime import datetime, timezone

from ocpp_reports.web import project_dashboard_connectors


class SharedConnectorProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.snapshot = {
            "sources": [],
            "connectors": [
                {"source_id": "node-a", "charger_id": "cp-1", "connector_id": 0,
                 "status": "Available", "connected": 1},
                {"source_id": "node-a", "charger_id": "cp-1", "connector_id": 1,
                 "status": "Charging", "connected": 1},
                {"source_id": "node-b", "charger_id": "cp-2", "connector_id": 1,
                 "status": "Faulted", "connected": 0},
            ],
        }

    def test_html_and_json_use_identical_projection(self):
        html = project_dashboard_connectors(copy.deepcopy(self.snapshot),
                                            now=self.now, connector_view="combined")
        json_view = project_dashboard_connectors(copy.deepcopy(self.snapshot),
                                                 now=self.now, connector_view="combined")
        self.assertEqual(html, json_view)
        self.assertEqual(len(html["connectors"]), 3)
        for row in json_view["connectors"]:
            self.assertIn("source_freshness", row)
            self.assertIn("connection_fresh", row)
            self.assertIn("stale", row)
            self.assertIn("status_discrepancy_badge", row)

    def test_connector_views_preserve_full_status_counts(self):
        combined = project_dashboard_connectors(copy.deepcopy(self.snapshot),
                                                now=self.now, connector_view="combined")
        station = project_dashboard_connectors(copy.deepcopy(self.snapshot),
                                               now=self.now, connector_view="station")
        individual = project_dashboard_connectors(copy.deepcopy(self.snapshot),
                                                  now=self.now, connector_view="individual")
        self.assertEqual(combined["status_counts"], station["status_counts"])
        self.assertEqual(combined["status_counts"], individual["status_counts"])
        self.assertEqual(len(station["connectors"]), 1)
        self.assertEqual(len(individual["connectors"]), 2)

    def test_invalid_view_is_rejected(self):
        with self.assertRaises(ValueError):
            project_dashboard_connectors(copy.deepcopy(self.snapshot),
                                         now=self.now, connector_view="unsupported")


if __name__ == "__main__":
    unittest.main()
