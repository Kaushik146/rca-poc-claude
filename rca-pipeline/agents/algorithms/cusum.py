"""
CUSUM (Cumulative Sum) Change Point Detection — pure numpy.

Detects when a time series fundamentally shifts regime (not a temporary spike,
but a sustained shift in mean). Works by tracking cumulative sums above/below
a control band, and flags a change point when the cumulative sum exceeds threshold.

Algorithm:
  For each observation x_t:
    S_high = max(0, S_high + (x_t - mu - k))    # cumsum of positive deviations
    S_low  = min(0, S_low  + (x_t - mu + k))    # cumsum of negative deviations
    k = 0.5 * sigma (decision interval / slack)
    h = 5.0 * sigma (threshold / alarm level)

  When S_high > h: upward change point detected
  When |S_low| > h: downward change point detected

  Reset S_high and S_low after each alarm.

Reference: Page, E. S. (1954). "Continuous inspection schemes."
Biometrika, 41(1-2), 100-115.
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Dict


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ChangePoint:
    index: int                # position in time series where change detected
    direction: str            # "up" or "down"
    cusum_value: float        # absolute value of S_high or S_low at detection
    magnitude: float          # estimated magnitude of shift (in std units)
    severity: str             # CRITICAL / HIGH / MEDIUM / LOW


@dataclass
class CUSUMResult:
    change_points: List[ChangePoint]
    is_changed: bool
    first_change_index: Optional[int]
    description: str


# ─────────────────────────────────────────────────────────────────────────────
# CUSUM Detector
# ─────────────────────────────────────────────────────────────────────────────
class CUSUMDetector:
    """
    CUSUM change point detector.
    Fit on baseline data to learn mean and std, then detect regimes shifts.
    """

    def __init__(self, k_factor: float = 0.5, h_factor: float = 5.0):
        """
        Parameters
        ----------
        k_factor : float
            Decision interval slack parameter. k = k_factor * sigma.
            Default 0.5 is good for balanced sensitivity.
        h_factor : float
            Alarm threshold parameter. h = h_factor * sigma.
            Default 5.0 = 5 standard deviations (conservative).
        """
        self.k_factor = k_factor
        self.h_factor = h_factor
        self.mean = None
        self.std = None
        self.k = None
        self.h = None
        self._fitted = False

    def fit(self, baseline_values: np.ndarray) -> "CUSUMDetector":
        """
        Learn baseline distribution from healthy data.

        Parameters
        ----------
        baseline_values : np.ndarray
            1D array of healthy metric values (normalized 0-1 or raw units).

        Returns
        -------
        self
        """
        baseline_values = np.asarray(baseline_values, dtype=float)
        self.mean = float(np.mean(baseline_values))
        self.std = float(np.std(baseline_values))
        if self.std < 1e-10:
            self.std = 1e-10  # prevent division by zero
        self.k = self.k_factor * self.std
        self.h = self.h_factor * self.std
        self._fitted = True
        return self

    def detect(self, values: np.ndarray) -> CUSUMResult:
        """
        Detect change points in a time series.

        Parameters
        ----------
        values : np.ndarray
            1D array of metric values (same scale as baseline).

        Returns
        -------
        CUSUMResult
            Contains list of ChangePoint objects and metadata.
        """
        if not self._fitted:
            raise RuntimeError("Call fit() before detect()")

        values = np.asarray(values, dtype=float)
        change_points = []

        s_high = 0.0
        s_low = 0.0

        for i, x in enumerate(values):
            # Update cumulative sums
            s_high = max(0.0, s_high + (x - self.mean - self.k))
            s_low = min(0.0, s_low + (x - self.mean + self.k))

            # Check for alarm conditions
            if s_high > self.h:
                # Upward shift detected
                magnitude = (x - self.mean) / self.std if self.std > 0 else 0.0
                severity = self._magnitude_to_severity(abs(magnitude), is_up=True)
                change_points.append(ChangePoint(
                    index=i,
                    direction="up",
                    cusum_value=float(s_high),
                    magnitude=float(magnitude),
                    severity=severity
                ))
                s_high = 0.0  # reset

            elif abs(s_low) > self.h:
                # Downward shift detected
                magnitude = (x - self.mean) / self.std if self.std > 0 else 0.0
                severity = self._magnitude_to_severity(abs(magnitude), is_up=False)
                change_points.append(ChangePoint(
                    index=i,
                    direction="down",
                    cusum_value=float(abs(s_low)),
                    magnitude=float(magnitude),
                    severity=severity
                ))
                s_low = 0.0  # reset

        is_changed = len(change_points) > 0
        first_idx = change_points[0].index if change_points else None
        desc = f"Detected {len(change_points)} change point(s)" if change_points else "No change detected"

        return CUSUMResult(
            change_points=change_points,
            is_changed=is_changed,
            first_change_index=first_idx,
            description=desc
        )

    def detect_multi(self, metric_dict: Dict[str, np.ndarray]) -> Dict[str, CUSUMResult]:
        """
        Run CUSUM on multiple named metrics simultaneously.

        Parameters
        ----------
        metric_dict : dict[str, np.ndarray]
            Dictionary mapping metric name → array of values.

        Returns
        -------
        dict[str, CUSUMResult]
            Results keyed by metric name.
        """
        results = {}
        for metric_name, values in metric_dict.items():
            results[metric_name] = self.detect(values)
        return results

    def find_first_change(self, values: np.ndarray) -> Optional[ChangePoint]:
        """
        Find just the earliest change point (or None).

        Parameters
        ----------
        values : np.ndarray
            Time series values.

        Returns
        -------
        ChangePoint or None
        """
        result = self.detect(values)
        if result.change_points:
            return result.change_points[0]
        return None

    def _magnitude_to_severity(self, magnitude: float, is_up: bool = True) -> str:
        """Map magnitude (in std units) to severity level."""
        m = abs(magnitude)
        if m >= 5.0:
            return "CRITICAL"
        elif m >= 3.0:
            return "HIGH"
        elif m >= 2.0:
            return "MEDIUM"
        else:
            return "LOW"


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing CUSUM Change Point Detector...")

    # Generate a stable baseline
    rng = np.random.RandomState(42)
    baseline = rng.normal(0.3, 0.02, 50)

    # Then a sustained shift upward
    shifted = rng.normal(0.8, 0.02, 50)

    # Combine
    series = np.concatenate([baseline, shifted])

    # Fit on baseline
    detector = CUSUMDetector(k_factor=0.5, h_factor=5.0)
    detector.fit(baseline)

    # Detect on full series
    result = detector.detect(series)

    print(f"\nSeries: 50 healthy points (mean=0.3) + 50 shifted points (mean=0.8)")
    print(f"Detector trained on: mean={detector.mean:.3f}, std={detector.std:.4f}")
    print(f"Parameters: k={detector.k:.4f}, h={detector.h:.4f}")
    print(f"\nDetection result: {result.is_changed}")
    print(f"Number of changes: {len(result.change_points)}")

    if result.change_points:
        cp = result.change_points[0]
        print(f"\nFirst change point:")
        print(f"  Index: {cp.index} (expected ~50)")
        print(f"  Direction: {cp.direction}")
        print(f"  CUSUM value: {cp.cusum_value:.4f}")
        print(f"  Magnitude (std units): {cp.magnitude:.3f}")
        print(f"  Severity: {cp.severity}")

        # Verify the change was detected near index 50
        assert 45 <= cp.index <= 55, f"Change point index {cp.index} not near 50!"
        assert cp.direction == "up", f"Direction should be 'up', got {cp.direction}"
        print("\n✅ CUSUM self-test PASSED")
    else:
        print("\n❌ CUSUM self-test FAILED: No change point detected!")
