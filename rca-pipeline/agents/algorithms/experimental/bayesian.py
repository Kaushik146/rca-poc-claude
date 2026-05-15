"""
Bayesian hypothesis scoring for root cause ranking.

Uses Bayes' theorem to update prior probabilities of hypotheses based on evidence.

How it works:
  Prior: initial belief about each hypothesis (e.g., uniform distribution)
  Evidence: observations that support or contradict hypotheses
  Posterior: updated belief after incorporating evidence

  Bayes' theorem:
    P(H|E) = P(E|H) * P(H) / P(E)

  where:
    P(H|E)     = posterior probability (what we want)
    P(E|H)     = likelihood (how likely evidence is if hypothesis true)
    P(H)       = prior probability
    P(E)       = marginal likelihood = Σ P(E|Hi) * P(Hi)

  Updates are sequential: after each evidence, the posterior becomes the prior
  for the next evidence update.
"""

from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """Single piece of evidence for or against hypotheses."""
    name: str                           # e.g. "python_anomaly"
    observation: bool | str             # what we observed
    likelihood_if_true: float           # P(E|H=True) ∈ [0,1]
    likelihood_if_false: float          # P(E|H=False) ∈ [0,1]


@dataclass
class HypothesisScore:
    """Result of Bayesian scoring for a single hypothesis."""
    hypothesis: str
    posterior: float                    # P(H|all_evidence) ∈ [0,1]
    prior: float                        # P(H) from initialization
    evidence_count: int                 # number of evidence updates applied
    top_supporting_evidence: List[str]  # names of evidence that boosted this hypothesis
    top_contradicting_evidence: List[str]  # names of evidence that hurt this hypothesis


class BayesianScorer:
    """
    Bayesian hypothesis scorer.
    """

    def __init__(self, hypotheses: List[str], priors: Optional[Dict[str, float]] = None):
        """
        Initialize Bayesian scorer.
        hypotheses: list of hypothesis names
        priors: optional dict of {hypothesis: prior_prob}. If None, use uniform.
        """
        self.hypotheses = hypotheses
        self.n = len(hypotheses)

        # Set priors
        if priors is None:
            # Uniform prior
            self.priors = {h: 1.0 / self.n for h in hypotheses}
        else:
            # Normalize provided priors
            total = sum(priors.values()) or 1.0
            self.priors = {h: priors.get(h, 0.0) / total for h in hypotheses}

        # Current beliefs (initially = priors)
        self.posteriors = dict(self.priors)
        self.evidence_count = 0
        self.applied_evidence: List[Evidence] = []

    def update(self, evidence: Evidence) -> None:
        """Apply single evidence update using Bayes' theorem."""
        if evidence is None:
            return
        # In a real system, evidence would be linked to specific hypotheses.
        # For simplicity, here we assume:
        #   - Evidence supports hypotheses where likelihood_if_true > likelihood_if_false
        #   - Evidence contradicts hypotheses where likelihood_if_true < likelihood_if_false
        #   - Evidence is neutral otherwise

        # Assign likelihoods based on evidence name and hypothesis name correlation
        likelihoods = {}
        evidence_name_lower = evidence.name.lower()

        for h in self.hypotheses:
            h_lower = h.lower()

            # If evidence name matches hypothesis, use likelihood_if_true
            # Otherwise use a blended likelihood
            if any(keyword in evidence_name_lower for keyword in ["python", "field"]):
                if "python" in h_lower or "field" in h_lower:
                    likelihoods[h] = evidence.likelihood_if_true
                else:
                    likelihoods[h] = evidence.likelihood_if_false
            elif any(keyword in evidence_name_lower for keyword in ["java", "config", "deployment"]):
                if "java" in h_lower or "config" in h_lower:
                    likelihoods[h] = evidence.likelihood_if_true
                else:
                    likelihoods[h] = evidence.likelihood_if_false
            elif any(keyword in evidence_name_lower for keyword in ["network"]):
                if "network" in h_lower:
                    likelihoods[h] = evidence.likelihood_if_true
                else:
                    likelihoods[h] = evidence.likelihood_if_false
            else:
                # Neutral evidence
                likelihoods[h] = 0.5

            # Clamp to prevent underflow
            likelihoods[h] = np.clip(likelihoods[h], 1e-10, 1.0 - 1e-10)

        # Marginal likelihood: P(E) = Σ P(E|Hi) * P(Hi)
        marginal = sum(
            likelihoods[h] * self.posteriors[h]
            for h in self.hypotheses
        )
        # Add numerical stability: clamp marginal to prevent division by zero
        marginal = max(marginal, 1e-10)

        # Update posteriors: P(H|E) = P(E|H) * P(H) / P(E)
        for h in self.hypotheses:
            self.posteriors[h] = (
                likelihoods[h] * self.posteriors[h] / marginal
            )

        # First normalize
        total = sum(self.posteriors.values())
        for h in self.hypotheses:
            self.posteriors[h] = self.posteriors[h] / max(total, 1e-10)
        # Clip
        for h in self.hypotheses:
            self.posteriors[h] = max(self.posteriors[h], 1e-10)
        # Re-normalize to ensure sum = 1
        clip_total = sum(self.posteriors.values())
        for h in self.hypotheses:
            self.posteriors[h] /= clip_total

        self.evidence_count += 1
        self.applied_evidence.append(evidence)

    def update_batch(self, evidences: List[Evidence]) -> None:
        """Apply multiple evidence updates sequentially."""
        for evidence in evidences:
            self.update(evidence)

    def get_posteriors(self) -> Dict[str, float]:
        """Return posteriors sorted by probability descending."""
        sorted_hyps = sorted(self.posteriors.items(), key=lambda x: x[1], reverse=True)
        return dict(sorted_hyps)

    def get_ranking(self) -> List[HypothesisScore]:
        """
        Return ranked list of HypothesisScore.
        Includes top supporting and contradicting evidence for each hypothesis.
        """
        results = []

        for h in self.hypotheses:
            # Identify supporting and contradicting evidence
            # Evidence supports H if: likelihood_if_true > likelihood_if_false
            # Evidence contradicts H if: likelihood_if_true < likelihood_if_false

            supporting = []
            contradicting = []

            for ev in self.applied_evidence:
                if ev.likelihood_if_true > ev.likelihood_if_false:
                    supporting.append(ev.name)
                elif ev.likelihood_if_true < ev.likelihood_if_false:
                    contradicting.append(ev.name)

            results.append(HypothesisScore(
                hypothesis=h,
                posterior=float(self.posteriors[h]),
                prior=float(self.priors[h]),
                evidence_count=self.evidence_count,
                top_supporting_evidence=supporting[:3],
                top_contradicting_evidence=contradicting[:3],
            ))

        # Sort by posterior descending
        results.sort(key=lambda x: x.posterior, reverse=True)
        return results

    def reset(self) -> None:
        """Reset to initial priors."""
        self.posteriors = dict(self.priors)
        self.evidence_count = 0
        self.applied_evidence = []


def build_evidence_from_signals(
    anomalies: Optional[List[dict]] = None,
    trace_data: Optional[dict] = None,
    deployment_data: Optional[dict] = None,
    code_issues: Optional[List[dict]] = None,
    kb_matches: Optional[List[dict]] = None,
) -> List[Evidence]:
    """
    Helper: convert RCA pipeline signals into Evidence objects.
    """
    evidences = []

    # From anomalies: match service names to hypotheses
    if anomalies:
        for anom in anomalies:
            service = anom.get("service", "").lower()
            anomaly_type = anom.get("anomaly_type", "").lower()

            if "python" in service or "inventory" in service:
                # Evidence for python_field_mismatch hypothesis
                evidences.append(Evidence(
                    name="python_anomaly_signal",
                    observation=True,
                    likelihood_if_true=0.8,
                    likelihood_if_false=0.2,
                ))

            if "cpu" in anomaly_type or "memory" in anomaly_type:
                # Evidence for config_error hypothesis
                evidences.append(Evidence(
                    name="resource_anomaly",
                    observation=True,
                    likelihood_if_true=0.6,
                    likelihood_if_false=0.3,
                ))

            if "timeout" in anomaly_type or "network" in anomaly_type:
                # Evidence for network_issue hypothesis
                evidences.append(Evidence(
                    name="network_anomaly",
                    observation=True,
                    likelihood_if_true=0.75,
                    likelihood_if_false=0.2,
                ))

    # From trace data: root failure service
    if trace_data:
        root_failure = trace_data.get("root_failure")
        if root_failure:
            service = root_failure.get("service", "").lower()

            if "python" in service:
                evidences.append(Evidence(
                    name="trace_root_python",
                    observation=True,
                    likelihood_if_true=0.9,
                    likelihood_if_false=0.1,
                ))

            if "java" in service:
                evidences.append(Evidence(
                    name="trace_root_java",
                    observation=True,
                    likelihood_if_true=0.85,
                    likelihood_if_false=0.15,
                ))

    # From deployment data: recent deployment
    if deployment_data:
        deployment_time = deployment_data.get("deployment_time_unix", 0)
        incident_time = deployment_data.get("incident_time_unix", 0)
        time_delta = incident_time - deployment_time

        if 0 < time_delta < 2 * 3600:  # within 2 hours
            evidences.append(Evidence(
                name="recent_deployment",
                observation=True,
                likelihood_if_true=0.7,
                likelihood_if_false=0.3,
            ))
        else:
            # No recent deployment (disfavors config_error hypothesis)
            evidences.append(Evidence(
                name="no_recent_deployment",
                observation=True,
                likelihood_if_true=0.3,
                likelihood_if_false=0.7,
            ))

    # From code issues: type mismatches, field errors
    if code_issues:
        for issue in code_issues:
            issue_type = issue.get("issue_type", "").lower()
            service = issue.get("service", "").lower()

            if "field" in issue_type or "attribute" in issue_type:
                if "python" in service:
                    evidences.append(Evidence(
                        name="code_field_mismatch",
                        observation=True,
                        likelihood_if_true=0.85,
                        likelihood_if_false=0.15,
                    ))

            if "config" in issue_type:
                evidences.append(Evidence(
                    name="code_config_error",
                    observation=True,
                    likelihood_if_true=0.75,
                    likelihood_if_false=0.2,
                ))

    # From KB: precedent incidents
    if kb_matches:
        for match in kb_matches:
            description = match.get("description", "").lower()

            if "field" in description:
                evidences.append(Evidence(
                    name="kb_field_precedent",
                    observation=True,
                    likelihood_if_true=0.75,
                    likelihood_if_false=0.25,
                ))

            if "config" in description:
                evidences.append(Evidence(
                    name="kb_config_precedent",
                    observation=True,
                    likelihood_if_true=0.7,
                    likelihood_if_false=0.3,
                ))

            if "network" in description:
                evidences.append(Evidence(
                    name="kb_network_precedent",
                    observation=True,
                    likelihood_if_true=0.8,
                    likelihood_if_false=0.2,
                ))

    return evidences


def compute_confidence_interval(posterior: float, n_evidence: int) -> Tuple[float, float]:
    """
    Compute confidence interval for posterior using Beta distribution approximation.
    More evidence → tighter interval.
    """
    if n_evidence == 0:
        return (0.0, 1.0)

    # Beta distribution parameters
    # posterior is the mean of Beta(alpha, beta)
    # We use a simple heuristic: more evidence → higher confidence
    alpha = posterior * (n_evidence + 2)
    beta = (1.0 - posterior) * (n_evidence + 2)

    # Approximate 95% CI using normal approximation to Beta
    mean = alpha / (alpha + beta)
    variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
    std = np.sqrt(variance)

    lower = max(0.0, mean - 1.96 * std)
    upper = min(1.0, mean + 1.96 * std)

    return (float(lower), float(upper))


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing Bayesian hypothesis scoring...")

    # Three hypotheses
    hypotheses = ["field_mismatch_python", "config_error_java", "network_issue"]

    # Initialize with uniform priors
    scorer = BayesianScorer(hypotheses)

    print(f"Initial priors: {scorer.get_posteriors()}")

    # Feed evidence
    evidences = [
        Evidence("python_anomaly", True, 0.8, 0.2),      # strongly favors H1
        Evidence("trace_root_python", True, 0.9, 0.1),   # very strongly favors H1
        Evidence("no_recent_deployment", True, 0.3, 0.7),  # disfavors H2
    ]

    scorer.update_batch(evidences)

    print(f"\nPosteriors after evidence:")
    posteriors = scorer.get_posteriors()
    for h, p in posteriors.items():
        print(f"  {h:30s} → {p:.4f}")

    # Verify field_mismatch_python ranks first
    ranking = scorer.get_ranking()
    assert ranking[0].hypothesis == "field_mismatch_python", \
        f"Expected field_mismatch_python at rank 1, got {ranking[0].hypothesis}"
    assert ranking[0].posterior > 0.6, \
        f"Expected posterior > 0.6, got {ranking[0].posterior}"

    print(f"\nTop hypothesis: {ranking[0].hypothesis} (posterior={ranking[0].posterior:.4f})")
    print(f"  Supporting evidence: {ranking[0].top_supporting_evidence}")
    print(f"  Contradicting evidence: {ranking[0].top_contradicting_evidence}")

    # Confidence interval
    lower, upper = compute_confidence_interval(ranking[0].posterior, scorer.evidence_count)
    print(f"  95% CI: [{lower:.4f}, {upper:.4f}]")

    # Test with build_evidence_from_signals
    print("\nTesting evidence builder...")
    signals_evidence = build_evidence_from_signals(
        anomalies=[
            {"service": "python-inventory", "anomaly_type": "cpu_spike"},
        ],
        trace_data={
            "root_failure": {"service": "python-inventory", "operation": "process_order"},
        },
        deployment_data={
            "deployment_time_unix": 1000,
            "incident_time_unix": 3600,  # 1 hour later
        },
        code_issues=[
            {"service": "python-inventory", "issue_type": "field_mismatch"},
        ],
    )

    scorer2 = BayesianScorer(hypotheses)
    scorer2.update_batch(signals_evidence)
    ranking2 = scorer2.get_ranking()

    print(f"Top hypothesis (signal-based): {ranking2[0].hypothesis} (posterior={ranking2[0].posterior:.4f})")
    assert ranking2[0].hypothesis == "field_mismatch_python", \
        f"Expected field_mismatch_python, got {ranking2[0].hypothesis}"

    print("\nBayesian self-test PASSED ✅")
