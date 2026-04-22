"""
Isolation Forest — pure numpy/Python, no sklearn.

How it works:
  An anomaly is a point that is easy to isolate. Build N random binary trees,
  each time picking a random feature and a random split value between that
  feature's min and max. A point that reaches a leaf node quickly (short path)
  is an outlier — it was easy to separate from the rest of the data.

  Anomaly score = 2 ^ (-avg_path_length / c(n))
  where c(n) = 2*H(n-1) - 2*(n-1)/n  (expected path length in a BST of n nodes)

Reference: Liu, Fei Tony, Ting, Kai Ming and Zhou, Zhi-Hua.
           "Isolation forest." ICDM 2008.
"""
import numpy as np
import random
from dataclasses import dataclass, field
from typing import Optional, List


# ─────────────────────────────────────────────────────────────────────────────
# Tree node
# ─────────────────────────────────────────────────────────────────────────────
class _INode:
    __slots__ = ("feature", "threshold", "left", "right", "size", "is_leaf")

    def __init__(self):
        self.feature   = None
        self.threshold = None
        self.left      = None
        self.right     = None
        self.size      = 0
        self.is_leaf   = False


# ─────────────────────────────────────────────────────────────────────────────
# Single Isolation Tree
# ─────────────────────────────────────────────────────────────────────────────
class _ITree:
    def __init__(self, max_depth: int, rng: random.Random):
        self._max_depth = max_depth
        self._rng = rng
        self._root: Optional[_INode] = None

    def fit(self, X: np.ndarray) -> "_ITree":
        self._root = self._grow(X, depth=0)
        return self

    def _grow(self, X: np.ndarray, depth: int) -> _INode:
        node = _INode()
        node.size = len(X)

        if depth >= self._max_depth or len(X) <= 1:
            node.is_leaf = True
            return node

        n_features = X.shape[1]
        # pick a random feature that has non-zero variance.
        # NB: random.Random.randint(a, b) is inclusive on BOTH ends, which
        # would yield feat == n_features and blow up with
        # "index n out of bounds for axis 1 with size n" on the next line.
        # Use randrange(n_features) for half-open [0, n_features).
        feat = None
        for _ in range(n_features):
            cand = self._rng.randrange(n_features)
            lo, hi = float(X[:, cand].min()), float(X[:, cand].max())
            if hi > lo:
                feat = cand
                break
        if feat is None:
            node.is_leaf = True
            return node
        lo, hi = float(X[:, feat].min()), float(X[:, feat].max())

        threshold = self._rng.uniform(lo, hi)
        left_mask  = X[:, feat] < threshold
        right_mask = ~left_mask

        node.feature   = feat
        node.threshold = threshold
        node.left  = self._grow(X[left_mask],  depth + 1)
        node.right = self._grow(X[right_mask], depth + 1)
        return node

    def path_length(self, x: np.ndarray) -> float:
        return self._path(x, self._root, 0)

    def _path(self, x: np.ndarray, node: _INode, depth: int) -> float:
        if node.is_leaf:
            return depth + _c(node.size)
        if x[node.feature] < node.threshold:
            return self._path(x, node.left, depth + 1)
        return self._path(x, node.right, depth + 1)


# ─────────────────────────────────────────────────────────────────────────────
# Expected path length of a failed search in a BST of n nodes
# ─────────────────────────────────────────────────────────────────────────────
def _c(n: int) -> float:
    if n <= 1:
        return 0.0
    h = np.log(n - 1) + 0.5772156649   # Euler–Mascheroni constant
    return 2.0 * h - (2.0 * (n - 1) / n)


# ─────────────────────────────────────────────────────────────────────────────
# Isolation Forest
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class IFScore:
    score: float          # 0-1, higher = more anomalous
    avg_path: float       # average isolation depth across trees
    is_anomaly: bool
    severity: str         # CRITICAL / HIGH / MEDIUM / LOW / NORMAL


class IsolationForest:
    """
    Isolation Forest implementation in pure Python/numpy.

    Parameters
    ----------
    n_estimators : int
        Number of isolation trees (default 100).
    max_samples : int or "auto"
        Subsample size per tree. "auto" = min(256, n).
    contamination : float
        Expected fraction of outliers (used to set decision threshold).
    random_state : int
        Seed for reproducibility.
    """

    def __init__(
        self,
        n_estimators: int = 100,
        max_samples: int | str = "auto",
        contamination: float = 0.1,
        random_state: int = 42,
    ):
        self.n_estimators  = n_estimators
        self.max_samples   = max_samples
        self.contamination = contamination
        self.random_state  = random_state
        self._trees: List[_ITree] = []
        self._c_norm = 1.0
        self._threshold = 0.5
        self._fitted = False

    def fit(self, X: np.ndarray) -> "IsolationForest":
        """
        Train the forest on normal data X (shape: [n_samples, n_features]).
        Call this with ONLY healthy/normal observations.
        """
        if X is None or (hasattr(X, '__len__') and len(X) == 0):
            return self
        X = np.asarray(X, dtype=float)
        n, _ = X.shape

        psi = n if self.max_samples == "auto" else min(self.max_samples, n)
        psi = min(psi, 256)

        max_depth = int(np.ceil(np.log2(psi))) if psi > 1 else 1
        self._c_norm = _c(psi)

        rng_master = random.Random(self.random_state)
        self._trees = []

        for _ in range(self.n_estimators):
            seed = rng_master.randint(0, 2**31)
            rng  = random.Random(seed)
            idxs = rng.choices(range(n), k=psi)
            tree = _ITree(max_depth=max_depth, rng=rng)
            tree.fit(X[idxs])
            self._trees.append(tree)

        # Calibrate threshold from the training data itself
        scores = self._raw_scores(X)
        sorted_scores = np.sort(scores)
        cutoff_idx = min(int((1.0 - self.contamination) * len(sorted_scores)), len(sorted_scores) - 1)
        self._threshold = float(sorted_scores[cutoff_idx])
        self._fitted = True
        return self

    def _raw_scores(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        scores = np.zeros(len(X))
        for i, x in enumerate(X):
            paths = [t.path_length(x) for t in self._trees]
            avg_path = float(np.mean(paths))
            # Anomaly score per paper: 2^(-avg/c)
            scores[i] = 2.0 ** (-avg_path / self._c_norm) if self._c_norm > 0 else 0.5
        return scores

    def score_sample(self, x: np.ndarray) -> IFScore:
        """Score a single feature vector. Returns IFScore."""
        assert self._fitted, "Call fit() first"
        x = np.asarray(x, dtype=float)
        paths    = [t.path_length(x) for t in self._trees]
        avg_path = float(np.mean(paths))
        raw      = 2.0 ** (-avg_path / self._c_norm) if self._c_norm > 0 else 0.5
        is_anom  = raw > self._threshold

        if raw > 0.80:   sev = "CRITICAL"
        elif raw > 0.65: sev = "HIGH"
        elif raw > 0.55: sev = "MEDIUM"
        elif is_anom:    sev = "LOW"
        else:            sev = "NORMAL"

        return IFScore(score=raw, avg_path=avg_path, is_anomaly=is_anom, severity=sev)

    def score_batch(self, X: np.ndarray) -> List[IFScore]:
        """Score a batch of feature vectors."""
        return [self.score_sample(x) for x in np.asarray(X, dtype=float)]


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    rng = np.random.RandomState(0)

    # Normal APM data: low CPU, low error rate, moderate latency
    normal = rng.normal(loc=[0.3, 0.02, 0.25, 0.8, 0.4, 0.2],
                        scale=[0.05, 0.01, 0.05, 0.05, 0.05, 0.03],
                        size=(500, 6))
    normal = np.clip(normal, 0, 1)

    forest = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    forest.fit(normal)

    # Normal-looking point
    n_score = forest.score_sample(np.array([0.32, 0.02, 0.27, 0.79, 0.41, 0.21]))
    print(f"Normal point   → score={n_score.score:.3f}  anomaly={n_score.is_anomaly}  sev={n_score.severity}")

    # Anomalous point (incident scenario: CPU spike + high errors + high latency)
    a_score = forest.score_sample(np.array([0.88, 0.35, 0.92, 0.31, 0.72, 0.95]))
    print(f"Incident point → score={a_score.score:.3f}  anomaly={a_score.is_anomaly}  sev={a_score.severity}")

    assert not n_score.is_anomaly,  "Normal point wrongly flagged!"
    assert a_score.is_anomaly,      "Incident point not detected!"
    print("IsolationForest self-test PASSED ✅")
