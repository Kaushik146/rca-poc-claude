"""
APM Anomaly Detector — ensemble of two complementary algorithms, no sklearn.

┌─────────────────────────────────────────────────────────────┐
│  Algorithm 1: Autoencoder neural network (pure numpy)        │
│    Architecture: 6 → 8 → 4 → 2 → 4 → 8 → 6                 │
│    Trained with real backpropagation on synthetic normal data │
│    Activation: ReLU (hidden layers), Sigmoid (output)        │
│    Loss: Mean Squared Reconstruction Error (MSRE)            │
│    Anomaly: high MSRE = decoder can't reconstruct the input  │
│                                                               │
│  Algorithm 2: Isolation Forest (pure Python/numpy)            │
│    100 trees, max_samples=256                                 │
│    Anomaly: short average isolation path = outlier            │
│                                                               │
│  Ensemble score: 0.55 * ae_score + 0.45 * if_score           │
│  Both are trained fresh when this module is first imported   │
└─────────────────────────────────────────────────────────────┘

Features (all normalised 0-1):
  cpu_pct, error_rate, latency_p99, throughput, mem_pct, db_query_ms
"""

from __future__ import annotations
import re
import numpy as np
from dataclasses import dataclass
from typing import List, Optional

from algorithms.isolation_forest import IsolationForest


# ─────────────────────────────────────────────────────────────────────────────
# Feature normalisation constants (based on realistic APM baselines)
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_NAMES = ["cpu_pct", "error_rate", "latency_p99", "throughput", "mem_pct", "db_query_ms"]

# [min, max] for un-normalising reconstructed values back to human-readable units
FEATURE_RANGES = {
    "cpu_pct":      (0,   100),     # percent
    "error_rate":   (0,   1.0),     # fraction
    "latency_p99":  (0,   5000),    # ms
    "throughput":   (0,   2000),    # req/s
    "mem_pct":      (0,   100),     # percent
    "db_query_ms":  (0,   3000),    # ms
}

N_FEATURES = len(FEATURE_NAMES)


# ─────────────────────────────────────────────────────────────────────────────
# Activation functions & their derivatives
# ─────────────────────────────────────────────────────────────────────────────
def _relu(x):      return np.maximum(0, x)
def _relu_d(x):    return (x > 0).astype(float)
def _sigmoid(x):   return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))
def _sigmoid_d(x): s = _sigmoid(x); return s * (1 - s)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic training data generator
# ─────────────────────────────────────────────────────────────────────────────
def _generate_normal_data(n: int = 3000, seed: int = 42) -> np.ndarray:
    """
    Generate realistic 'healthy' APM snapshots.
    Includes correlated features (high throughput -> slightly higher CPU/latency).
    All values normalised to [0, 1].
    """
    rng = np.random.RandomState(seed)

    # Base healthy ranges (normalised)
    cpu      = rng.beta(2, 5, n)   # skewed low, ~0.2-0.45
    err      = rng.beta(1, 30, n)  # very low, ~0-0.05
    latency  = rng.beta(2, 6, n)   # moderate, ~0.15-0.40
    through  = rng.beta(5, 2, n)   # high, ~0.55-0.90
    mem      = rng.beta(3, 4, n)   # moderate, ~0.35-0.60
    db       = rng.beta(2, 7, n)   # low, ~0.10-0.30

    # Realistic correlations: high throughput -> slight CPU and latency increase
    cpu     = np.clip(cpu     + 0.15 * through,  0, 1)
    latency = np.clip(latency + 0.10 * through,  0, 1)

    # Small random noise
    noise = rng.normal(0, 0.01, (n, N_FEATURES))
    X = np.stack([cpu, err, latency, through, mem, db], axis=1)
    return np.clip(X + noise, 0.0, 1.0).astype(np.float64)


# ─────────────────────────────────────────────────────────────────────────────
# Autoencoder with real backpropagation
# ─────────────────────────────────────────────────────────────────────────────
class _Autoencoder:
    """
    Fully-connected autoencoder.
    Architecture: 6 -> 8 -> 4 -> 2 (bottleneck) -> 4 -> 8 -> 6
    Hidden layers use ReLU; output uses Sigmoid (keeps output in [0,1]).
    Trained with mini-batch gradient descent via analytic backpropagation.
    """

    def __init__(self, seed: int = 7):
        rng = np.random.RandomState(seed)

        def _he(fan_in, fan_out):
            """He initialisation for ReLU layers."""
            return rng.randn(fan_in, fan_out) * np.sqrt(2.0 / fan_in), np.zeros(fan_out)

        self.W1, self.b1 = _he(6, 8)
        self.W2, self.b2 = _he(8, 4)
        self.W3, self.b3 = _he(4, 2)   # bottleneck
        self.W4, self.b4 = _he(2, 4)
        self.W5, self.b5 = _he(4, 8)
        self.W6, self.b6 = _he(8, 6)

    def forward(self, X: np.ndarray):
        z1 = X  @ self.W1 + self.b1;  a1 = _relu(z1)
        z2 = a1 @ self.W2 + self.b2;  a2 = _relu(z2)
        z3 = a2 @ self.W3 + self.b3;  a3 = _relu(z3)
        z4 = a3 @ self.W4 + self.b4;  a4 = _relu(z4)
        z5 = a4 @ self.W5 + self.b5;  a5 = _relu(z5)
        z6 = a5 @ self.W6 + self.b6;  out = _sigmoid(z6)
        return out, (X, z1, a1, z2, a2, z3, a3, z4, a4, z5, a5, z6, out)

    def backward(self, cache, lr: float) -> float:
        """Backpropagate MSE loss, update weights in-place, return scalar loss."""
        X, z1, a1, z2, a2, z3, a3, z4, a4, z5, a5, z6, out = cache
        n = X.shape[0]
        loss = float(np.mean((out - X) ** 2))

        d = (2 / n) * (out - X) * _sigmoid_d(z6)
        dW6 = a5.T @ d;  db6 = d.sum(0);  d = (d @ self.W6.T) * _relu_d(z5)
        dW5 = a4.T @ d;  db5 = d.sum(0);  d = (d @ self.W5.T) * _relu_d(z4)
        dW4 = a3.T @ d;  db4 = d.sum(0);  d = (d @ self.W4.T) * _relu_d(z3)
        dW3 = a2.T @ d;  db3 = d.sum(0);  d = (d @ self.W3.T) * _relu_d(z2)
        dW2 = a1.T @ d;  db2 = d.sum(0);  d = (d @ self.W2.T) * _relu_d(z1)
        dW1 = X.T  @ d;  db1 = d.sum(0)

        for W, dW, b, db in [
            (self.W6, dW6, self.b6, db6), (self.W5, dW5, self.b5, db5),
            (self.W4, dW4, self.b4, db4), (self.W3, dW3, self.b3, db3),
            (self.W2, dW2, self.b2, db2), (self.W1, dW1, self.b1, db1),
        ]:
            W -= lr * dW
            b -= lr * db

        return loss

    def train(self, X: np.ndarray, epochs: int = 250, lr: float = 0.003,
              batch_size: int = 128, verbose: bool = False) -> List[float]:
        history = []
        n = len(X)
        rng = np.random.RandomState(0)
        for epoch in range(epochs):
            perm  = rng.permutation(n)
            ep_loss, batches = 0.0, 0
            for i in range(0, n, batch_size):
                batch = X[perm[i: i + batch_size]]
                _, cache = self.forward(batch)
                ep_loss += self.backward(cache, lr)
                batches += 1
            avg = ep_loss / batches
            history.append(avg)
            if verbose and epoch % 50 == 0:
                print(f"    epoch {epoch:3d}/{epochs}  loss={avg:.6f}")
        return history

    def reconstruct(self, x: np.ndarray) -> np.ndarray:
        out, _ = self.forward(x.reshape(1, -1))
        return out.flatten()

    def msre(self, x: np.ndarray) -> float:
        return float(np.mean((self.reconstruct(x) - x) ** 2))


# ─────────────────────────────────────────────────────────────────────────────
# Ensemble result dataclass
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AnomalyScore:
    # Ensemble
    ensemble_score:    float
    is_anomaly:        bool
    severity:          str        # CRITICAL / HIGH / MEDIUM / LOW / NORMAL
    confidence:        float

    # Autoencoder component
    ae_msre:           float
    ae_is_anomaly:     bool
    ae_threshold:      float
    ae_feature_errors: list
    reconstructed:     list
    anomalous_features: list

    # Isolation Forest component
    if_score:          float
    if_is_anomaly:     bool
    if_avg_path:       float

    # Legacy alias
    msre: float = 0.0

    def __post_init__(self):
        self.msre = self.ae_msre


# ─────────────────────────────────────────────────────────────────────────────
# Main ensemble detector
# ─────────────────────────────────────────────────────────────────────────────
class AnomalyDetector:
    """
    Ensemble: Autoencoder (55%) + Isolation Forest (45%).
    Both models are trained from scratch on synthetic normal APM data at init time.
    No external ML libraries. Pure numpy + stdlib.
    """
    AE_WEIGHT = 0.55
    IF_WEIGHT = 0.45

    def __init__(self, verbose: bool = False):
        X_train = _generate_normal_data(n=3000)

        if verbose: print("  [AnomalyDetector] Training autoencoder (6→8→4→2→4→8→6) ...")
        self._ae = _Autoencoder(seed=7)
        self._ae.train(X_train, epochs=250, lr=0.003, verbose=verbose)

        # AE threshold: 99th percentile MSRE on training data
        train_msres = np.array([self._ae.msre(x) for x in X_train])
        self._ae_threshold = float(np.percentile(train_msres, 99))

        if verbose: print("  [AnomalyDetector] Training Isolation Forest (100 trees) ...")
        self._if = IsolationForest(n_estimators=100, contamination=0.01, random_state=42)
        self._if.fit(X_train)

        # Ensemble threshold: 99th percentile of ensemble score on training data
        ae_norms = np.clip(train_msres / (self._ae_threshold * 3), 0, 1)
        if_scores = np.array([self._if.score_sample(x).score for x in X_train])
        ensemble = self.AE_WEIGHT * ae_norms + self.IF_WEIGHT * if_scores
        self._ens_threshold = float(np.percentile(ensemble, 99))

    def score(self, cpu_pct: float, error_rate: float, latency_p99: float,
              throughput: float, mem_pct: float, db_query_ms: float) -> AnomalyScore:
        x = np.array([cpu_pct, error_rate, latency_p99, throughput, mem_pct, db_query_ms])
        return self._score_vec(x)

    def _score_vec(self, x: np.ndarray) -> AnomalyScore:
        # Autoencoder
        recon     = self._ae.reconstruct(x)
        feat_errs = (recon - x) ** 2
        msre      = float(feat_errs.mean())
        ae_norm   = float(np.clip(msre / (self._ae_threshold * 3), 0, 1))
        ae_anom   = msre > self._ae_threshold

        # Top offending features (un-normalised for readability)
        top_idx = feat_errs.argsort()[::-1][:3]
        anom_feats = []
        for i in top_idx:
            fname = FEATURE_NAMES[i]
            lo, hi = FEATURE_RANGES[fname]
            anom_feats.append({
                "feature":                fname,
                "error":                  float(feat_errs[i]),
                "observed":               float(x[i]) * (hi - lo) + lo,
                "reconstructed_as_normal": float(recon[i]) * (hi - lo) + lo,
            })

        recon_unnorm = [
            float(recon[i]) * (FEATURE_RANGES[n][1] - FEATURE_RANGES[n][0]) + FEATURE_RANGES[n][0]
            for i, n in enumerate(FEATURE_NAMES)
        ]

        # Isolation Forest
        if_res  = self._if.score_sample(x)
        if_norm = float(if_res.score)

        # Ensemble
        ens     = self.AE_WEIGHT * ae_norm + self.IF_WEIGHT * if_norm
        is_anom = ens > self._ens_threshold

        if ens > 0.80:   sev = "CRITICAL"
        elif ens > 0.60: sev = "HIGH"
        elif ens > 0.45: sev = "MEDIUM"
        elif is_anom:    sev = "LOW"
        else:            sev = "NORMAL"

        conf = float(min(1.0, ens / max(self._ens_threshold, 1e-9)))

        return AnomalyScore(
            ensemble_score=float(ens), is_anomaly=is_anom, severity=sev, confidence=conf,
            ae_msre=msre, ae_is_anomaly=ae_anom, ae_threshold=self._ae_threshold,
            ae_feature_errors=[{"feature": FEATURE_NAMES[i], "error": float(e)}
                               for i, e in enumerate(feat_errs)],
            reconstructed=recon_unnorm, anomalous_features=anom_feats,
            if_score=float(if_res.score), if_is_anomaly=if_res.is_anomaly,
            if_avg_path=float(if_res.avg_path),
        )

    def score_time_series(self, snapshots: List[dict]) -> List[AnomalyScore]:
        return [self._score_vec(np.array([s.get(f, 0.0) for f in FEATURE_NAMES])) for s in snapshots]

    def find_incident_start(self, snapshots: List[dict], window: int = 3) -> Optional[int]:
        scores = self.score_time_series(snapshots)
        streak = 0
        for i, s in enumerate(scores):
            streak = (streak + 1) if s.is_anomaly else 0
            if streak >= window:
                return i - window + 1
        return None

    def calibrate_threshold(self, healthy_snapshots: List[dict]) -> float:
        if len(healthy_snapshots) == 0:
            return self._ens_threshold  # or appropriate default
        X = np.array([[s.get(f, 0.0) for f in FEATURE_NAMES] for s in healthy_snapshots])
        ae_msres  = np.array([self._ae.msre(x) for x in X])
        ae_norms  = np.clip(ae_msres / (self._ae_threshold * 3), 0, 1)
        if_scores = np.array([self._if.score_sample(x).score for x in X])
        ens       = self.AE_WEIGHT * ae_norms + self.IF_WEIGHT * if_scores
        self._ens_threshold = float(np.percentile(ens, 99))
        return self._ens_threshold


# ─────────────────────────────────────────────────────────────────────────────
# APM text parser
# ─────────────────────────────────────────────────────────────────────────────
# Regexes that capture plain values (first number on relevant line)
_CPU_RE = re.compile(r"cpu[_\s]*(?:usage|percent|pct)?[:\s=]+(\d+(?:\.\d+)?)\s*%?", re.I)
_ERR_RE = re.compile(r"error[_\s]*rate[:\s=]+(\d+(?:\.\d+)?)\s*%?", re.I)
_LAT_RE = re.compile(r"(?:p99|latency|response)[_\s]*(?:p99)?[:\s=]+(\d+(?:\.\d+)?)\s*ms", re.I)
_THR_RE = re.compile(r"(?:throughput|rps|req(?:uests)?[/\s]*(?:min|s(?:ec)?))[:\s=]+(\d+(?:\.\d+)?)", re.I)
_MEM_RE = re.compile(r"mem(?:ory)?[_\s]*(?:usage|percent|pct)?[:\s=]+(\d+(?:\.\d+)?)\s*%?", re.I)
_DB_RE  = re.compile(r"(?:db|database|query)[_\s]*(?:time|latency|avg|ms)?[:\s=]+(\d+(?:\.\d+)?)\s*ms", re.I)

# Arrow pattern: "X → Y" or "X -> Y" — extracts all numbers from a line with an arrow
_ARROW_RE = re.compile(r"(\d+(?:\.\d+)?)[^→>\-\d]*[→>]+[^0-9]*(\d+(?:\.\d+)?)")


def _extract_line_values(regex, block, scale=100.0, cap=1.0):
    """Extract baseline and incident values from a metric line.
    Handles 'X → Y' arrow format by finding the regex match, then checking
    if the same line has an arrow pattern with a second value."""
    m = regex.search(block)
    if not m:
        return None, None

    val1 = min(float(m.group(1)) / scale, cap)

    # Find which line the match is on, then check for arrow on that line
    match_pos = m.start()
    line_start = block.rfind('\n', 0, match_pos) + 1
    line_end = block.find('\n', match_pos)
    if line_end == -1:
        line_end = len(block)
    line = block[line_start:line_end]

    # Look for arrow pattern on this line
    arrow_m = _ARROW_RE.search(line)
    if arrow_m:
        val2 = min(float(arrow_m.group(2)) / scale, cap)
        return val1, val2

    return val1, None


def parse_apm_text_to_snapshots(text: str) -> List[dict]:
    """Parse APM text into metric snapshots. Handles 'X → Y' arrow format by creating
    both baseline and incident snapshots per service block."""
    snapshots = []
    blocks = re.split(r"\n{2,}", text.strip())
    if len(blocks) == 1:
        blocks = [b for b in text.strip().split("\n") if b.strip()]

    for block in blocks:
        baseline: dict = {}
        incident: dict = {}
        has_arrows = False

        cpu1, cpu2 = _extract_line_values(_CPU_RE, block, 100.0)
        if cpu1 is not None: baseline["cpu_pct"] = cpu1
        if cpu2 is not None: incident["cpu_pct"] = cpu2; has_arrows = True

        # Error rate: always treat as percentage if % sign is present on the line
        m = _ERR_RE.search(block)
        if m:
            v1 = float(m.group(1))
            match_pos = m.start()
            line_start = block.rfind('\n', 0, match_pos) + 1
            line_end = block.find('\n', match_pos)
            if line_end == -1: line_end = len(block)
            line = block[line_start:line_end]
            has_pct = '%' in line
            baseline["error_rate"] = min(v1 / 100.0 if has_pct or v1 > 1 else v1, 1.0)
            arrow_m = _ARROW_RE.search(line)
            if arrow_m:
                v2 = float(arrow_m.group(2))
                incident["error_rate"] = min(v2 / 100.0 if has_pct or v2 > 1 else v2, 1.0)
                has_arrows = True

        lat1, lat2 = _extract_line_values(_LAT_RE, block, 5000.0)
        if lat1 is not None: baseline["latency_p99"] = lat1
        if lat2 is not None: incident["latency_p99"] = lat2; has_arrows = True

        thr1, thr2 = _extract_line_values(_THR_RE, block, 2000.0)
        if thr1 is not None: baseline["throughput"] = thr1
        if thr2 is not None: incident["throughput"] = thr2; has_arrows = True

        mem1, mem2 = _extract_line_values(_MEM_RE, block, 100.0)
        if mem1 is not None: baseline["mem_pct"] = mem1
        if mem2 is not None: incident["mem_pct"] = mem2; has_arrows = True

        db1, db2 = _extract_line_values(_DB_RE, block, 3000.0)
        if db1 is not None: baseline["db_query_ms"] = db1
        if db2 is not None: incident["db_query_ms"] = db2; has_arrows = True

        # Emit baseline snapshot
        present = {k: v for k, v in baseline.items() if v is not None}
        if len(present) >= 3:
            for k in FEATURE_NAMES:
                present.setdefault(k, 0.0)
            present["_label"] = "baseline"
            snapshots.append(present)

        # Emit incident snapshot (merged: use incident values where available, baseline otherwise)
        if has_arrows:
            merged = dict(baseline)
            merged.update(incident)
            present2 = {k: v for k, v in merged.items() if v is not None}
            if len(present2) >= 3:
                for k in FEATURE_NAMES:
                    present2.setdefault(k, 0.0)
                present2["_label"] = "incident"
                snapshots.append(present2)

    return snapshots


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Training AnomalyDetector...")
    det = AnomalyDetector(verbose=True)

    healthy  = det.score(0.30, 0.01, 0.22, 0.80, 0.42, 0.18)
    incident = det.score(0.78, 0.31, 0.93, 0.31, 0.72, 0.90)

    print(f"\nHealthy  → ens={healthy.ensemble_score:.3f}  anomaly={healthy.is_anomaly}  sev={healthy.severity}")
    print(f"Incident → ens={incident.ensemble_score:.3f}  anomaly={incident.is_anomaly}  sev={incident.severity}")
    print(f"  AE msre={incident.ae_msre:.4f} (threshold={incident.ae_threshold:.4f})")
    print(f"  IF score={incident.if_score:.4f}  path={incident.if_avg_path:.2f}")
    print(f"  Top offenders: {[f['feature'] for f in incident.anomalous_features]}")

    assert not healthy.is_anomaly,  "Healthy point wrongly flagged!"
    assert incident.is_anomaly,     "Incident not detected!"
    print("\nSelf-test PASSED ✅")
