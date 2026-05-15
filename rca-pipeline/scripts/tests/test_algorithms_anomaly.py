"""Direct unit coverage for agents/algorithms/anomaly_detector.py."""
from __future__ import annotations
import sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT / "agents"))
from algorithms.anomaly_detector import AnomalyDetector, AnomalyScore  # type: ignore


# Inputs to score() are interpreted in [0, 1] (autoencoder trained on
# normalized data). Raw percentages/ms must be pre-normalized upstream.
HEALTHY_SNAPSHOT = dict(
    cpu_pct=0.35, error_rate=0.005, latency_p99=0.20,
    throughput=0.75, mem_pct=0.50, db_query_ms=0.15,
)
ANOMALY_SNAPSHOT = dict(
    cpu_pct=0.98, error_rate=0.45, latency_p99=0.96,
    throughput=0.06, mem_pct=0.92, db_query_ms=0.97,
)


class AnomalyDetectorBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det = AnomalyDetector(verbose=False)

    def test_score_returns_full_anomaly_score_shape(self):
        result = self.det.score(**HEALTHY_SNAPSHOT)
        self.assertIsInstance(result, AnomalyScore)
        self.assertIsInstance(result.ensemble_score, float)
        self.assertIsInstance(result.is_anomaly, bool)
        self.assertIsInstance(result.severity, str)
        self.assertIsInstance(result.ae_msre, float)
        self.assertIsInstance(result.if_score, float)

    def test_anomaly_scores_higher_than_healthy(self):
        healthy = self.det.score(**HEALTHY_SNAPSHOT).ensemble_score
        anomalous = self.det.score(**ANOMALY_SNAPSHOT).ensemble_score
        self.assertGreater(anomalous, healthy)

    def test_clearly_anomalous_flagged_as_anomaly(self):
        result = self.det.score(**ANOMALY_SNAPSHOT)
        self.assertTrue(result.is_anomaly)

    def test_ensemble_score_in_unit_interval(self):
        for snap in (HEALTHY_SNAPSHOT, ANOMALY_SNAPSHOT):
            r = self.det.score(**snap)
            self.assertGreaterEqual(r.ensemble_score, 0.0)
            self.assertLessEqual(r.ensemble_score, 1.0 + 1e-6)


class AnomalyDetectorTimeSeries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det = AnomalyDetector(verbose=False)
        cls.healthy_stream = [HEALTHY_SNAPSHOT.copy() for _ in range(10)]
        cls.mixed_stream = (
            [HEALTHY_SNAPSHOT.copy() for _ in range(5)]
            + [ANOMALY_SNAPSHOT.copy() for _ in range(5)]
        )

    def test_score_time_series_returns_one_score_per_snapshot(self):
        scores = self.det.score_time_series(self.healthy_stream)
        self.assertEqual(len(scores), len(self.healthy_stream))
        for s in scores:
            self.assertIsInstance(s, AnomalyScore)

    def test_find_incident_start_returns_an_index_for_mixed_stream(self):
        idx = self.det.find_incident_start(self.mixed_stream, window=2)
        self.assertIsNotNone(idx)
        self.assertGreaterEqual(idx, 0)
        self.assertLess(idx, len(self.mixed_stream))


if __name__ == "__main__":
    unittest.main(verbosity=2)
