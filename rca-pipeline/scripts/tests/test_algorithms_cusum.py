"""Direct unit coverage for agents/algorithms/cusum.py."""
from __future__ import annotations
import sys, unittest
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT / "agents"))
from algorithms.cusum import CUSUMDetector, CUSUMResult, ChangePoint  # type: ignore


class CUSUMFitBehavior(unittest.TestCase):
    def test_fit_learns_baseline_distribution(self):
        rng = np.random.RandomState(42)
        baseline = rng.normal(loc=100.0, scale=5.0, size=500)
        d = CUSUMDetector().fit(baseline)
        self.assertAlmostEqual(d.mean, baseline.mean(), places=5)
        self.assertAlmostEqual(d.std, baseline.std(), places=5)
        self.assertGreater(d.k, 0)
        self.assertGreater(d.h, d.k)
        self.assertTrue(d._fitted)

    def test_k_and_h_scale_with_std(self):
        baseline = np.full(100, 50.0) + np.random.RandomState(0).normal(0, 2.0, 100)
        d = CUSUMDetector(k_factor=0.5, h_factor=5.0).fit(baseline)
        self.assertAlmostEqual(d.k, 0.5 * d.std, places=5)
        self.assertAlmostEqual(d.h, 5.0 * d.std, places=5)


class CUSUMDetectsSyntheticChanges(unittest.TestCase):
    def setUp(self):
        rng = np.random.RandomState(7)
        self.baseline = rng.normal(loc=10.0, scale=1.0, size=200)
        self.det = CUSUMDetector().fit(self.baseline)

    def test_no_change_on_stable_series_returns_no_change_points(self):
        rng = np.random.RandomState(99)
        stable = rng.normal(loc=10.0, scale=0.3, size=100)
        result = self.det.detect(stable)
        self.assertIsInstance(result, CUSUMResult)
        self.assertFalse(result.is_changed)
        self.assertEqual(result.change_points, [])
        self.assertIsNone(result.first_change_index)

    def test_upward_step_change_detected(self):
        rng = np.random.RandomState(13)
        series = np.concatenate([
            rng.normal(loc=10.0, scale=1.0, size=50),
            rng.normal(loc=20.0, scale=1.0, size=50),
        ])
        result = self.det.detect(series)
        self.assertTrue(result.is_changed)
        self.assertGreaterEqual(result.first_change_index, 50)
        self.assertTrue(any(cp.direction == "up" for cp in result.change_points))

    def test_downward_step_change_detected(self):
        rng = np.random.RandomState(21)
        series = np.concatenate([
            rng.normal(loc=10.0, scale=1.0, size=50),
            rng.normal(loc=0.0, scale=1.0, size=50),
        ])
        result = self.det.detect(series)
        self.assertTrue(result.is_changed)
        self.assertGreaterEqual(result.first_change_index, 50)
        self.assertTrue(any(cp.direction == "down" for cp in result.change_points))

    def test_severity_scales_with_magnitude(self):
        rng = np.random.RandomState(33)
        baseline_part = rng.normal(loc=10.0, scale=1.0, size=50)
        small = np.concatenate([baseline_part, rng.normal(loc=13.0, scale=1.0, size=50)])
        large = np.concatenate([baseline_part, rng.normal(loc=25.0, scale=1.0, size=50)])
        sm = self.det.detect(small)
        lg = self.det.detect(large)
        self.assertTrue(sm.is_changed)
        self.assertTrue(lg.is_changed)
        self.assertGreater(lg.change_points[0].magnitude, sm.change_points[0].magnitude)


class CUSUMFindFirstChange(unittest.TestCase):
    def setUp(self):
        rng = np.random.RandomState(101)
        baseline = rng.normal(loc=50.0, scale=2.0, size=200)
        self.det = CUSUMDetector().fit(baseline)

    def test_returns_none_on_stable_input(self):
        rng = np.random.RandomState(202)
        stable = rng.normal(loc=50.0, scale=0.5, size=100)
        self.assertIsNone(self.det.find_first_change(stable))

    def test_returns_changepoint_on_shifted_input(self):
        rng = np.random.RandomState(303)
        series = np.concatenate([
            rng.normal(loc=50.0, scale=2.0, size=40),
            rng.normal(loc=80.0, scale=2.0, size=40),
        ])
        cp = self.det.find_first_change(series)
        self.assertIsNotNone(cp)
        self.assertIsInstance(cp, ChangePoint)
        self.assertGreaterEqual(cp.index, 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
