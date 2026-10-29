"""Offline contract tests: uv run python -m unittest discover -s tests -v."""

import json
import unittest

from oncall_agent import data
from oncall_agent.tools import TOOL_SCHEMAS, query_telemetry


class TelemetryContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = next(
            schema["function"] for schema in TOOL_SCHEMAS
            if schema["function"]["name"] == "query_telemetry"
        )
        # Test the catalog the model actually sees, not just its backing constant.
        self.catalog = json.loads(
            self.schema["description"].split("Available metrics by service: ", 1)[1]
        )

    def test_catalog_matches_all_supported_pairs(self):
        advertised = {
            (service, metric)
            for service, metrics in self.catalog.items()
            for metric in metrics
        }
        self.assertEqual(advertised, set(data.METRIC_PROFILES))
        self.assertTrue(set(data.SERVICES).issubset(self.catalog))
        for service, metrics in self.catalog.items():
            self.assertEqual(metrics, data.available_metrics(service))

    def test_every_advertised_pair_can_be_queried(self):
        for service, metrics in self.catalog.items():
            for metric in metrics:
                with self.subTest(service=service, metric=metric):
                    result = query_telemetry(None, None, service, metric)
                    self.assertNotIn("error", result)
                    self.assertEqual(result["service"], service)
                    self.assertEqual(result["metric"], metric)
                    self.assertTrue(result["points"])

    def test_regression_services_expose_only_their_metrics(self):
        self.assertEqual(self.catalog["events-ingest"], [
            "replication_lag_seconds", "throughput_rps",
        ])
        self.assertEqual(self.catalog["index-builder"], [
            "iops_queue_depth", "job_failure_count",
        ])
        self.assertEqual(self.catalog["catalog-api"], [])

    def test_invalid_pairs_still_return_an_error(self):
        for service, metric in [
            ("events-ingest", "http_p99_latency_ms"),
            ("index-builder", "http_p99_latency_ms"),
            ("catalog-api", "http_p99_latency_ms"),
            ("unknown-service", "throughput_rps"),
            ("payments-api", "unknown-metric"),
        ]:
            with self.subTest(service=service, metric=metric):
                result = query_telemetry(None, None, service, metric)
                self.assertIn("No metric", result["error"])
                self.assertNotIn("anomalous", result)


if __name__ == "__main__":
    unittest.main()
