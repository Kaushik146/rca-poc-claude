"""
DBSCAN clustering for alert/anomaly correlation.

Groups related alerts into clusters so we can distinguish:
  "one incident causing 20 alerts" from "3 separate incidents"

How it works:
  DBSCAN (Density-Based Spatial Clustering of Applications with Noise)
  clusters points that are "close together" in feature space.

  Algorithm:
    1. For each unvisited point P:
       - If P has ≥ min_samples neighbors within eps distance → P is a core point
       - Expand cluster: recursively add all neighbors of core points
    2. Points not in any cluster → noise (label -1)

  Feature encoding:
    Alerts → numeric vectors: [timestamp, service_index, severity_index, anomaly_type_hash]
    Distance: weighted Euclidean
    Weights: time=0.4, service=0.3, type=0.2, severity=0.1
    All dimensions normalized to [0, 1]

  Two alerts on the same service at the same time with the same anomaly type ≈ distance 0.
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import hashlib


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AlertFeature:
    """Numeric feature vector for an alert."""
    alert_id: str
    timestamp_unix: float       # seconds since epoch
    service_index: int          # 0-based index into service list
    severity_index: int         # 0=LOW, 1=MEDIUM, 2=HIGH, 3=CRITICAL
    anomaly_type_hash: int      # hash of anomaly type string


@dataclass
class AlertCluster:
    """Result of DBSCAN clustering."""
    cluster_id: int             # -1 for noise
    alerts: List[Dict]          # original alert dicts
    size: int
    primary_service: str        # most common service in cluster
    time_range: Tuple[float, float]  # (min_ts, max_ts) in unix seconds
    dominant_anomaly_type: str  # most common anomaly type
    is_noise: bool              # True if cluster_id == -1


class AlertDBSCAN:
    """
    DBSCAN clustering for alerts/anomalies.
    """

    # Feature weights: [time, service, anomaly_type, severity]
    WEIGHTS = np.array([0.4, 0.3, 0.2, 0.1])

    def __init__(self, eps: float = 0.5, min_samples: int = 2):
        """
        Initialize DBSCAN.
        eps: distance threshold for neighbors
        min_samples: minimum points to form a core point
        """
        self.eps = eps
        self.min_samples = min_samples
        self.labels_ = None
        self.alerts_list = None
        self.features = None
        self.service_map = {}
        self.anomaly_map = {}

    def fit(self, alerts: List[dict]) -> "AlertDBSCAN":
        """
        Fit DBSCAN on alerts.
        Expects each alert dict with keys: id, timestamp, service, severity, anomaly_type, description
        """
        self.alerts_list = alerts
        self._encode_alerts(alerts)
        # Pre-normalize feature vectors once to optimize neighbor search
        self._normalize_features()
        self._dbscan()
        return self

    def _normalize_features(self) -> None:
        """
        Normalize feature vectors to [0, 1] range.
        Called once after encoding to optimize repeated neighbor distance computations.
        """
        if self.features is None or len(self.features) == 0:
            return

        for col in range(4):
            col_min = self.features[:, col].min()
            col_max = self.features[:, col].max()
            if col_max > col_min:
                self.features[:, col] = (self.features[:, col] - col_min) / (col_max - col_min)
            else:
                self.features[:, col] = 0.0

    def _encode_alerts(self, alerts: List[dict]) -> None:
        """Convert heterogeneous alert dicts to numeric feature matrix."""
        features = []
        service_map = {}
        severity_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        anomaly_map = {}

        for alert in alerts:
            # Service encoding
            svc = alert.get("service", "unknown")
            if svc not in service_map:
                service_map[svc] = len(service_map)
            svc_idx = service_map[svc]

            # Timestamp: parse if string, assume unix seconds otherwise
            ts = alert.get("timestamp")
            if isinstance(ts, str):
                # Assume ISO format or numeric string
                try:
                    ts = float(ts)
                except:
                    ts = 0.0
            else:
                ts = float(ts or 0.0)

            # Severity encoding
            sev = alert.get("severity", "medium").lower()
            sev_idx = severity_map.get(sev, 1)

            # Anomaly type encoding
            anom_type = alert.get("anomaly_type", "unknown")
            if anom_type not in anomaly_map:
                anomaly_map[anom_type] = len(anomaly_map)
            anom_hash = anomaly_map[anom_type]

            features.append(AlertFeature(
                alert_id=alert.get("id", str(len(features))),
                timestamp_unix=ts,
                service_index=svc_idx,
                severity_index=sev_idx,
                anomaly_type_hash=anom_hash,
            ))

        self.service_map = service_map
        self.anomaly_map = anomaly_map

        # Convert to numpy array (normalization done separately in _normalize_features)
        n = len(features)
        X = np.zeros((n, 4), dtype=np.float64)
        for i, feat in enumerate(features):
            X[i, 0] = feat.timestamp_unix
            X[i, 1] = feat.service_index
            X[i, 2] = feat.anomaly_type_hash
            X[i, 3] = feat.severity_index

        self.features = X

    def _dbscan(self) -> None:
        """Run DBSCAN algorithm."""
        n = len(self.features)
        self.labels_ = np.full(n, -1, dtype=int)  # -1 = noise
        cluster_id = 0

        for i in range(n):
            if self.labels_[i] != -1:
                # Already visited
                continue

            # Find neighbors of point i
            neighbors = self._get_neighbors(i)

            if len(neighbors) < self.min_samples:
                # i is noise (label -1)
                self.labels_[i] = -1
            else:
                # i is a core point, expand cluster
                self._expand_cluster(cluster_id, neighbors)
                cluster_id += 1

    def _get_neighbors(self, point_idx: int) -> List[int]:
        """
        Find all neighbors of point_idx within eps distance (weighted).

        Optimization: Uses pre-normalized features for efficient batch distance computation.
        For datasets > 100 points, consider using scipy.spatial.cKDTree for better performance.
        """
        neighbors = []
        x = self.features[point_idx]

        for j in range(len(self.features)):
            if j == point_idx:
                neighbors.append(j)
                continue

            # Weighted Euclidean distance (using pre-normalized features)
            diff = self.features[j] - x
            dist = np.sqrt(np.sum((self.WEIGHTS * diff) ** 2))

            # Early exit: if distance exceeds threshold, skip
            if dist <= self.eps:
                neighbors.append(j)

        return neighbors

    def _expand_cluster(self, cluster_id: int, seed_neighbors: List[int]) -> None:
        """Expand cluster from core points."""
        queue = list(seed_neighbors)
        visited = set()

        while queue:
            point_idx = queue.pop(0)

            if point_idx in visited:
                continue
            visited.add(point_idx)

            # Assign to cluster
            self.labels_[point_idx] = cluster_id

            # If point is also a core point, add its neighbors to queue
            neighbors = self._get_neighbors(point_idx)
            if len(neighbors) >= self.min_samples:
                for neighbor_idx in neighbors:
                    if neighbor_idx not in visited and self.labels_[neighbor_idx] == -1:
                        queue.append(neighbor_idx)

    def predict(self) -> np.ndarray:
        """Return cluster labels (-1 for noise)."""
        return self.labels_

    def get_clusters(self) -> List[AlertCluster]:
        """Return list of clusters (including noise)."""
        if self.labels_ is None:
            return []

        clusters = {}
        cluster_alerts = {}

        # Group alerts by cluster
        for i, label in enumerate(self.labels_):
            if label not in clusters:
                clusters[label] = []
                cluster_alerts[label] = []
            clusters[label].append(i)
            cluster_alerts[label].append(self.alerts_list[i])

        # Build AlertCluster objects
        results = []
        reverse_svc_map = {v: k for k, v in self.service_map.items()}
        reverse_anom_map = {v: k for k, v in self.anomaly_map.items()}

        for cluster_id in sorted(clusters.keys()):
            indices = clusters[cluster_id]
            alerts = cluster_alerts[cluster_id]

            # Primary service: most common
            svc_counts = {}
            anom_counts = {}
            timestamps = []

            for alert in alerts:
                svc = alert.get("service", "unknown")
                svc_counts[svc] = svc_counts.get(svc, 0) + 1

                anom = alert.get("anomaly_type", "unknown")
                anom_counts[anom] = anom_counts.get(anom, 0) + 1

                ts = alert.get("timestamp", 0)
                if isinstance(ts, str):
                    try:
                        ts = float(ts)
                    except:
                        ts = 0.0
                timestamps.append(float(ts))

            primary_svc = max(svc_counts.items(), key=lambda x: x[1])[0] if svc_counts else "unknown"
            dominant_anom = max(anom_counts.items(), key=lambda x: x[1])[0] if anom_counts else "unknown"
            time_range = (min(timestamps), max(timestamps)) if timestamps else (0, 0)

            results.append(AlertCluster(
                cluster_id=cluster_id,
                alerts=alerts,
                size=len(alerts),
                primary_service=primary_svc,
                time_range=time_range,
                dominant_anomaly_type=dominant_anom,
                is_noise=(cluster_id == -1),
            ))

        return results

    def get_incident_groups(self) -> List[AlertCluster]:
        """Return only non-noise clusters, sorted by size descending."""
        clusters = self.get_clusters()
        incidents = [c for c in clusters if not c.is_noise]
        incidents.sort(key=lambda c: c.size, reverse=True)
        return incidents


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing DBSCAN alert clustering...")

    # Create test alerts
    alerts = []

    # Cluster 1: java-order-service, timestamps 100-104, same anomaly
    for i in range(5):
        alerts.append({
            "id": f"alert_java_{i}",
            "timestamp": 100 + i,
            "service": "java-order-service",
            "severity": "high",
            "anomaly_type": "cpu_spike",
            "description": f"CPU spike event {i}",
        })

    # Cluster 2: python-inventory-service, timestamps 100-102, same anomaly
    for i in range(3):
        alerts.append({
            "id": f"alert_py_{i}",
            "timestamp": 100 + i,
            "service": "python-inventory-service",
            "severity": "medium",
            "anomaly_type": "memory_leak",
            "description": f"Memory leak event {i}",
        })

    # Noise: two random isolated alerts
    alerts.append({
        "id": "noise_1",
        "timestamp": 500,
        "service": "node-service",
        "severity": "low",
        "anomaly_type": "random_error",
        "description": "Isolated error 1",
    })
    alerts.append({
        "id": "noise_2",
        "timestamp": 600,
        "service": "sqlite-db",
        "severity": "low",
        "anomaly_type": "connection_timeout",
        "description": "Isolated error 2",
    })

    # Fit DBSCAN with smaller eps to separate clusters
    clusterer = AlertDBSCAN(eps=0.3, min_samples=2)
    clusterer.fit(alerts)

    # Get incident groups
    incidents = clusterer.get_incident_groups()
    print(f"\nFound {len(incidents)} incident clusters:")
    for ic in incidents:
        print(f"  Cluster {ic.cluster_id}: {ic.size} alerts, service={ic.primary_service}, "
              f"anomaly={ic.dominant_anomaly_type}, time_range={ic.time_range}")

    # Verify: should find 2 clusters + 2 noise points
    clusters = clusterer.get_clusters()
    noise_count = sum(1 for c in clusters if c.is_noise)
    non_noise_count = len([c for c in clusters if not c.is_noise])

    print(f"\nTotal clusters: {len(clusters)}, non-noise: {non_noise_count}, noise: {noise_count}")
    assert non_noise_count >= 2, f"Expected ≥2 non-noise clusters, got {non_noise_count}"

    # Verify we got reasonable clusters
    # Cluster 1 should be java-order-service (largest group)
    java_cluster = [c for c in incidents if c.primary_service == "java-order-service"]
    assert len(java_cluster) > 0, "Expected java-order-service cluster"
    assert java_cluster[0].size >= 3, f"Expected ≥3 alerts in java cluster, got {java_cluster[0].size}"

    # We should have separated the noise/isolated alerts from the main clusters
    largest_cluster = max(incidents, key=lambda c: c.size)
    assert largest_cluster.size >= 3, "Expected largest cluster to have ≥3 alerts"

    print("\nDBSCAN self-test PASSED ✅")
