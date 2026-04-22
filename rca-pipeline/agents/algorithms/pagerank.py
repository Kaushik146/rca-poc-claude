"""
PageRank for service dependency blame attribution.

Given a weighted directed graph of service dependencies (discovered from traces and code),
run PageRank to find which services are most "blamed" for an incident.

How it works:
  Services are nodes in a directed graph. An edge from A → B with weight W means:
  "A calls B, with weight W (error count or latency impact)".

  PageRank iteratively computes a "blame score" for each service based on:
  1. How many incoming dependencies point to it (from other services)
  2. How much "blame" those upstream services carry

  Damping factor (default 0.85) models the assumption that blame "flows" to dependencies
  with some random walk probability.

  Formula per iteration:
    r_new[i] = (1-d)/N + d * Σ(r[j] * A[i,j])
  where A[i,j] = weight(j→i) / Σ_out(j)  (normalized incoming weights)

  Error bias: initial distribution can be skewed toward high-error services,
  so they start with more blame that propagates outward.
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BlameScore:
    """Result of PageRank scoring for a single service."""
    service: str
    score: float                    # blame score (0-1 typical)
    rank: int                       # rank by score (1 = highest)
    incoming_blame_from: List[Tuple[str, float]]  # [(service, weight), ...]
    outgoing_blame_to: List[Tuple[str, float]]    # [(service, weight), ...]


class ServiceGraph:
    """
    Weighted directed graph of service dependencies.
    Nodes: services (by name). Edges: dependencies with weights and call types.
    """

    def __init__(self):
        self.nodes: Dict[str, dict] = {}  # service_name → {metadata}
        self.edges: Dict[str, Dict[str, float]] = {}  # from_svc → {to_svc: weight}
        self.edge_types: Dict[Tuple[str, str], str] = {}  # (from, to) → call_type

    def add_service(self, name: str, metadata: Optional[dict] = None) -> None:
        """Add a service node to the graph."""
        if name not in self.nodes:
            self.nodes[name] = metadata or {}
            self.edges[name] = {}

    def add_dependency(self, from_service: str, to_service: str, weight: float = 1.0,
                       call_type: str = "http") -> None:
        """Add a weighted directed edge from from_service → to_service."""
        self.add_service(from_service)
        self.add_service(to_service)
        if to_service not in self.edges[from_service]:
            self.edges[from_service][to_service] = 0.0
        self.edges[from_service][to_service] += weight
        self.edge_types[(from_service, to_service)] = call_type

    def from_trace_data(self, trace_result: dict) -> "ServiceGraph":
        """
        Build graph from trace analyzer output.
        Expects trace_result with keys: services_involved, call_chain
        Parent→child spans become edges, weighted by error count or latency.
        """
        if "services_involved" not in trace_result:
            return self

        # Add all services
        for svc in trace_result.get("services_involved", []):
            self.add_service(svc)

        # Build edges from call chain (parent spans call child spans)
        call_chain = trace_result.get("call_chain", [])
        service_to_spans = {}
        for i, span in enumerate(call_chain):
            svc = span.get("service")
            if svc:
                if svc not in service_to_spans:
                    service_to_spans[svc] = []
                service_to_spans[svc].append(i)

        # Parent-child relationships → edges
        for i, span in enumerate(call_chain):
            svc = span.get("service")
            status = span.get("status", "ok")
            # Weight by error presence and latency
            weight = 1.0
            if status == "error":
                weight += 2.0
            latency = span.get("duration_ms", 0)
            weight += max(0.0, min(1.0, latency / 1000.0))  # normalize latency impact

            # Find children (heuristic: next spans at deeper indent)
            # For simplicity, link to immediately next span from different service
            for j in range(i + 1, min(i + 5, len(call_chain))):
                child_svc = call_chain[j].get("service")
                if child_svc and child_svc != svc:
                    self.add_dependency(svc, child_svc, weight=weight, call_type="http")
                    break

        return self

    def from_code_analysis(self, code_issues: list) -> "ServiceGraph":
        """
        Build graph from code agent output (cross-service bugs).
        Expects code_issues as list of dicts with 'service', 'called_service', 'issue_type'.
        """
        for issue in code_issues:
            from_svc = issue.get("service")
            to_svc = issue.get("called_service")
            if from_svc and to_svc:
                weight = 1.0
                if "error" in issue.get("issue_type", "").lower():
                    weight = 2.0
                self.add_dependency(from_svc, to_svc, weight=weight, call_type="rpc")

        return self

    def merge(self, other: "ServiceGraph") -> "ServiceGraph":
        """Combine two graphs, summing edge weights."""
        for svc, metadata in other.nodes.items():
            if svc not in self.nodes:
                self.add_service(svc, metadata)

        for from_svc in other.edges:
            for to_svc, weight in other.edges[from_svc].items():
                if to_svc in self.edges.get(from_svc, {}):
                    self.edges[from_svc][to_svc] += weight
                else:
                    self.add_dependency(from_svc, to_svc, weight=weight)

        return self

    def to_adjacency_matrix(self) -> Tuple[np.ndarray, List[str]]:
        """
        Convert to normalized adjacency matrix.
        A[i, j] = weight(j → i) / sum_outgoing(j)
        Returns: (matrix, service_list)
        """
        svc_list = sorted(self.nodes.keys())
        n = len(svc_list)
        svc_idx = {s: i for i, s in enumerate(svc_list)}

        A = np.zeros((n, n), dtype=np.float64)

        # Build adjacency: A[i, j] = incoming weight to i from j, normalized
        for from_svc in self.edges:
            out_sum = sum(self.edges[from_svc].values())
            if out_sum == 0:
                out_sum = 1.0

            for to_svc, weight in self.edges[from_svc].items():
                j = svc_idx[from_svc]
                i = svc_idx[to_svc]
                A[i, j] = weight / out_sum

        return A, svc_list


class PageRankScorer:
    """
    PageRank scorer for service blame attribution.
    """

    def __init__(self, damping: float = 0.85, max_iter: int = 100, tol: float = 1e-6):
        """
        Initialize scorer.
        damping: probability of following an edge (vs random jump). Default 0.85.
        max_iter: max iterations for convergence. Default 100.
        tol: convergence tolerance. Default 1e-6.
        """
        self.damping = damping
        self.max_iter = max_iter
        self.tol = tol

    def rank(self, graph: ServiceGraph) -> List[BlameScore]:
        """
        Compute PageRank blame scores via power iteration.
        Returns list of BlameScore, sorted by score descending.
        """
        if graph is None or (hasattr(graph, 'nodes') and len(graph.nodes) == 0):
            return []
        A, svc_list = graph.to_adjacency_matrix()
        n = len(svc_list)
        svc_idx = {s: i for i, s in enumerate(svc_list)}

        # Power iteration
        r = np.ones(n) / n  # uniform initial distribution
        for _ in range(self.max_iter):
            r_new = (1.0 - self.damping) / n + self.damping * (A @ r)
            if np.linalg.norm(r_new - r) < self.tol:
                break
            r = r_new

        # Normalize to [0, 1]
        r = np.clip(r, 0, 1)
        if r.max() > 0:
            r = r / r.max()

        # Build result list
        results = []
        for i, svc in enumerate(svc_list):
            incoming = [(from_svc, graph.edges[from_svc][svc])
                        for from_svc in svc_list
                        if from_svc in graph.edges and svc in graph.edges[from_svc]]
            outgoing = [(to_svc, weight) for to_svc, weight in graph.edges.get(svc, {}).items()]

            results.append(BlameScore(
                service=svc,
                score=float(r[i]),
                rank=0,  # set below
                incoming_blame_from=incoming,
                outgoing_blame_to=outgoing,
            ))

        # Sort by score, assign ranks
        results.sort(key=lambda x: x.score, reverse=True)
        for i, bs in enumerate(results):
            bs.rank = i + 1

        return results

    def rank_with_error_bias(self, graph: ServiceGraph,
                              error_counts: Dict[str, int]) -> List[BlameScore]:
        """
        Modified PageRank where initial distribution is biased toward error-heavy services.
        error_counts: {service_name: error_count}
        """
        if graph is None or error_counts is None or (hasattr(graph, 'nodes') and len(graph.nodes) == 0):
            return []
        A, svc_list = graph.to_adjacency_matrix()
        n = len(svc_list)

        # Biased initial distribution: services with more errors start with more blame
        r = np.zeros(n)
        for i, svc in enumerate(svc_list):
            r[i] = 1.0 + error_counts.get(svc, 0)
        r = r / r.sum()

        # Store the biased restart distribution
        r_bias = np.copy(r)

        # Power iteration with biased restart: emphasis on error-heavy services
        for _ in range(self.max_iter):
            r_new = (1.0 - self.damping) * r_bias + self.damping * (A @ r)
            if np.linalg.norm(r_new - r) < self.tol:
                break
            r = r_new

        # Normalize to [0, 1]
        r = np.clip(r, 0, 1)
        if r.max() > 0:
            r = r / r.max()

        # Build result list
        results = []
        for i, svc in enumerate(svc_list):
            incoming = [(from_svc, graph.edges[from_svc][svc])
                        for from_svc in svc_list
                        if from_svc in graph.edges and svc in graph.edges[from_svc]]
            outgoing = [(to_svc, weight) for to_svc, weight in graph.edges.get(svc, {}).items()]

            results.append(BlameScore(
                service=svc,
                score=float(r[i]),
                rank=0,
                incoming_blame_from=incoming,
                outgoing_blame_to=outgoing,
            ))

        results.sort(key=lambda x: x.score, reverse=True)
        for i, bs in enumerate(results):
            bs.rank = i + 1

        return results


def find_blast_radius(graph: ServiceGraph, root_service: str) -> Dict[str, int]:
    """
    BFS from root_service, returns all reachable downstream services with hop count.
    Shows "if X fails, who's affected?"
    """
    if root_service not in graph.nodes:
        return {}

    visited = {root_service: 0}
    queue = [root_service]

    while queue:
        svc = queue.pop(0)
        for to_svc in graph.edges.get(svc, {}):
            if to_svc not in visited:
                visited[to_svc] = visited[svc] + 1
                queue.append(to_svc)

    return visited


def find_critical_dependencies(graph: ServiceGraph) -> List[str]:
    """
    Find articulation points (services that, if removed, disconnect the graph).
    Uses Tarjan's algorithm for articulation points.
    """
    if graph is None or (hasattr(graph, 'nodes') and len(graph.nodes) == 0):
        return []

    nodes = list(graph.nodes.keys())
    if len(nodes) <= 1:
        return []

    articulation_points = set()
    visited = set()
    disc = {}
    low = {}
    parent = {}
    timer = [0]

    def dfs(u):
        visited.add(u)
        disc[u] = low[u] = timer[0]
        timer[0] += 1
        children = 0

        for v in graph.edges.get(u, {}):
            if v not in visited:
                children += 1
                parent[v] = u
                dfs(v)
                low[u] = min(low[u], low[v])

                # u is articulation if:
                # (1) u is root and has 2+ children
                if parent.get(u) is None and children > 1:
                    articulation_points.add(u)
                # (2) u is not root and low[v] >= disc[u]
                if parent.get(u) is not None and low.get(v, float('inf')) >= disc.get(u, float('inf')):
                    articulation_points.add(u)

            elif v != parent.get(u):
                low[u] = min(low[u], disc.get(v, float('inf')))

    # Run DFS from each unvisited node
    for u in nodes:
        if u not in visited:
            parent[u] = None
            dfs(u)

    return sorted(list(articulation_points))


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing PageRank service blame attribution...")

    # Build test graph
    graph = ServiceGraph()
    graph.add_service("java-order-service")
    graph.add_service("python-inventory-service")
    graph.add_service("node-notification-service")
    graph.add_service("sqlite-db")

    # Dependencies
    graph.add_dependency("java-order-service", "python-inventory-service", weight=5.0)
    graph.add_dependency("java-order-service", "node-notification-service", weight=2.0)
    graph.add_dependency("java-order-service", "sqlite-db", weight=1.0)
    graph.add_dependency("python-inventory-service", "sqlite-db", weight=3.0)

    # Error bias: python-inventory has 50 errors, node has 10
    error_counts = {
        "python-inventory-service": 50,
        "node-notification-service": 10,
        "java-order-service": 5,
        "sqlite-db": 2,
    }

    # Score with error bias
    scorer = PageRankScorer(damping=0.85, max_iter=100)
    results = scorer.rank_with_error_bias(graph, error_counts)

    print("\nPageRank scores (error-biased):")
    for bs in results:
        print(f"  {bs.rank}. {bs.service:30s} score={bs.score:.4f}")

    # Verify python-inventory ranks highest
    assert results[0].service == "python-inventory-service", \
        f"Expected python-inventory-service at rank 1, got {results[0].service}"
    assert results[0].score > 0.3, f"Expected high score, got {results[0].score}"

    # Test blast radius
    blast = find_blast_radius(graph, "java-order-service")
    print(f"\nBlast radius from java-order-service: {blast}")
    assert "python-inventory-service" in blast
    assert "sqlite-db" in blast

    # Test critical dependencies
    critical = find_critical_dependencies(graph)
    print(f"Critical dependencies (articulation points): {critical}")

    print("\nPageRank self-test PASSED ✅")
