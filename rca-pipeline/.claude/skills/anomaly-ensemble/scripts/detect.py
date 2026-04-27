#!/usr/bin/env python3
"""
anomaly-ensemble — fallback when vendor AIOps returns nothing.

Input (stdin JSON):
{
  "features_by_service": {
    "<service>": [
      {"cpu_pct": 0.31, "error_rate": 0.002, "latency_p99": 180,
       "throughput": 1200, "mem_pct": 0.55, "db_query_ms": 45}, ...
    ]
  }
}

Output (stdout JSON): {"anomalies": [{service, index, severity, ensemble_score, ...}, ...]}

Wraps agents/algorithms/anomaly_detector.AnomalyDetector. The detector trains
itself on synthetic normal APM data at init time, so we instantiate once and
reuse across all services in this invocation.
"""
from __future__ import annotations
import json
import os
import pickle
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[4]
# Match the project convention: agents/ is on PYTHONPATH, algorithms/ is top-level.
sys.path.insert(0, str(REPO_ROOT / "agents"))

from algorithms.anomaly_detector import AnomalyDetector  # type: ignore

# Training is ~30s (250 epochs AE + 100 isolation trees). Cache the fitted
# detector to disk so only the first invocation pays that cost. Invalidate
# by deleting cache/ or bumping _CACHE_VERSION.
_CACHE_VERSION = "v1"
_CACHE_DIR = HERE.parent / "cache"
_CACHE_PATH = _CACHE_DIR / f"detector-{_CACHE_VERSION}.pkl"


def _load_or_train_detector(verbose: bool = False) -> "AnomalyDetector":
    if os.environ.get("RCA_SKIP_DETECTOR_CACHE") != "1" and _CACHE_PATH.exists():
        try:
            with open(_CACHE_PATH, "rb") as f:
                return pickle.load(f)
        except Exception:
            # Corrupt cache — fall through to retrain
            pass
    detector = AnomalyDetector(verbose=verbose)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "wb") as f:
            pickle.dump(detector, f)
    except Exception:
        # Best-effort cache write; don't fail invocation on IO error.
        pass
    return detector


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    features = payload.get("features_by_service", {}) or {}

    # Instantiate the detector once (training or load-from-cache).
    try:
        detector = _load_or_train_detector(verbose=False)
    except Exception as e:
        print(json.dumps({"error": f"detector_init_failed: {type(e).__name__}: {e}"}))
        return

    out: list[dict] = []
    for service, rows in features.items():
        if not isinstance(rows, list) or not rows:
            continue
        try:
            scores = detector.score_time_series(rows)
        except Exception as e:
            out.append({"service": service, "error": f"{type(e).__name__}: {e}"})
            continue
        for i, s in enumerate(scores):
            if not s.is_anomaly:
                continue
            out.append({
                "service": service,
                "index": i,
                "severity": s.severity,
                "ensemble_score": round(float(s.ensemble_score), 4),
                "confidence": round(float(s.confidence), 4),
                "top_features": [f["feature"] for f in (s.anomalous_features or [])][:3],
            })

    print(json.dumps({"anomalies": out}, indent=2))


if __name__ == "__main__":
    main()
