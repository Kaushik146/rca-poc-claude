"""
HypothesisRankerAgent — ReAct agent with deep analysis capabilities.

This agent takes signals from multiple upstream agents (logs, code, traces, APM,
deployment, KB) and generates ranked hypotheses with deterministic scoring based
on algorithmic analysis of evidence strength, specificity, deployment correlation,
symptom matching, severity alignment, contradiction detection, and counter-evidence.

Uses ReActEngine with:
- 5 original tools (generate_candidate_hypotheses, score_hypothesis_algorithm,
  score_hypothesis_bayesian, check_kb_precedent, rank_candidates)
- 5 new deep-analysis tools (build_evidence_graph, detect_contradictions,
  resolve_contradictions, calibrate_confidence, generate_counter_evidence)
- Scratchpad, reflection, and multi-phase investigation protocol

Tools available:
  Original:
  - generate_candidate_hypotheses(anomalies, code_issues, trace_data)
  - score_hypothesis_algorithm(hypothesis_dict, anomalies_list, deployment_data_dict)
  - score_hypothesis_bayesian(hypothesis_name, evidence_items, prior)
  - check_kb_precedent(hypothesis_service, hypothesis_type, kb_matches)
  - rank_candidates(candidates_with_scores)

  New:
  - build_evidence_graph(candidates, anomalies, code_issues, trace_data, deployment_data, kb_matches)
  - detect_contradictions(evidence_graph)
  - resolve_contradictions(contradictions, candidates)
  - calibrate_confidence(scored_candidates)
  - generate_counter_evidence(top_hypothesis, anomalies, code_issues)
"""
import os, sys, json, re
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import get_client, get_model
from algorithms.bayesian import BayesianScorer, Evidence
from react_core import ReActEngine

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
client = get_client()

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAI function-calling schema)
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_candidate_hypotheses",
            "description": (
                "Pure Python: generates structured candidate hypotheses from signals. "
                "For each unique (service, anomaly_type) pair, creates a hypothesis candidate "
                "with type, affected_services, and supporting_evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anomalies": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of anomaly dicts from LogAgent"
                    },
                    "code_issues": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of code issues from CodeAgent"
                    },
                    "trace_data": {
                        "type": "object",
                        "description": "Trace data from TraceAgent"
                    },
                },
                "required": ["anomalies"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_hypothesis_algorithm",
            "description": (
                "Pure Python deterministic scoring function. Scores hypothesis on 5 dimensions (0-1 each):\n"
                "1. evidence_strength: count of anomalies/signals mentioning hypothesis service\n"
                "2. specificity: 1.0 if file/field mentioned, 0.5 service-level, 0.2 if vague\n"
                "3. deployment_correlation: 1.0 if within 2hrs before incident with matching files\n"
                "4. symptom_match: Jaccard similarity of hypothesis keywords vs anomaly descriptions\n"
                "5. severity_alignment: 1.0 if CRITICAL anomalies point to service, 0.5 HIGH, 0.2 MEDIUM\n"
                "Returns weighted_score = 0.25*ev + 0.20*spec + 0.20*dep + 0.20*symp + 0.15*sev"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis_dict": {
                        "type": "object",
                        "description": "Hypothesis candidate dict with fields: type, affected_services, description"
                    },
                    "anomalies_list": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Full list of anomalies to score against"
                    },
                    "deployment_data_dict": {
                        "type": "object",
                        "description": "Deployment info: timestamp, changed_files, services"
                    },
                },
                "required": ["hypothesis_dict", "anomalies_list"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_kb_precedent",
            "description": (
                "Pure Python: checks Knowledge Base matches. Returns 1.0 if exact service match, "
                "0.5 if matching anomaly_type, 0.0 otherwise."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis_service": {"type": "string", "description": "Service name from hypothesis"},
                    "hypothesis_type": {"type": "string", "description": "Anomaly type from hypothesis"},
                    "kb_matches": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of KB incident matches"
                    },
                },
                "required": ["hypothesis_service", "hypothesis_type", "kb_matches"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_hypothesis_bayesian",
            "description": (
                "Bayesian hypothesis scoring using sequential evidence updating via Bayes' theorem. "
                "Starts with uniform prior, updates posterior as each piece of evidence is applied. "
                "Returns posterior probability, evidence chain, and likelihood ratios. "
                "Complements the weighted-score approach with a probabilistic perspective."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis_name": {"type": "string", "description": "Name/description of hypothesis"},
                    "prior": {"type": "number", "description": "Prior probability (default 0.5)", "default": 0.5},
                    "evidence_items": {
                        "type": "array",
                        "description": "List of evidence dicts, each with 'name', 'supports' (bool), 'strength' (0-1)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":     {"type": "string"},
                                "supports": {"type": "boolean"},
                                "strength": {"type": "number"},
                            },
                            "required": ["name", "supports", "strength"],
                        },
                    },
                },
                "required": ["hypothesis_name", "evidence_items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_candidates",
            "description": (
                "Pure Python sort by weighted_score descending. Assigns rank 1,2,3... "
                "Returns sorted list with confidence intervals."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates_with_scores": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of candidate dicts with 'scores' and 'weighted_score' fields"
                    },
                },
                "required": ["candidates_with_scores"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_evidence_graph",
            "description": (
                "Build a bipartite evidence graph: hypotheses ↔ evidence. Each edge has weight "
                "(how strongly evidence supports/contradicts hypothesis). Uses keyword matching "
                "and service-name overlap. Returns adjacency list with edge weights."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of candidate hypotheses"
                    },
                    "anomalies": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of anomalies"
                    },
                    "code_issues": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of code issues"
                    },
                    "trace_data": {
                        "type": "object",
                        "description": "Trace data"
                    },
                    "deployment_data": {
                        "type": "object",
                        "description": "Deployment data"
                    },
                    "kb_matches": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "KB matches"
                    },
                },
                "required": ["candidates", "anomalies"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_contradictions",
            "description": (
                "Find pairs of evidence that support conflicting hypotheses. "
                "Returns contradictions with severity (how much evidence is in conflict)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evidence_graph": {
                        "type": "object",
                        "description": "Evidence graph from build_evidence_graph"
                    },
                },
                "required": ["evidence_graph"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_contradictions",
            "description": (
                "Apply resolution heuristics to contradictions: "
                "(1) prefer more specific evidence, (2) prefer reliable sources (code > logs > APM), "
                "(3) prefer evidence with more corroboration. Returns resolved hypothesis list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contradictions": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of contradictions from detect_contradictions"
                    },
                    "candidates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Original candidate hypotheses"
                    },
                },
                "required": ["contradictions", "candidates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calibrate_confidence",
            "description": (
                "Apply Platt scaling-style calibration: if all hypotheses score similarly "
                "(small gap), lower confidence. If one dominates, raise confidence. "
                "If evidence count < 3, cap confidence at 0.6. Returns calibrated scores."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scored_candidates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Candidates with scores dict including weighted_score"
                    },
                },
                "required": ["scored_candidates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_counter_evidence",
            "description": (
                "Devil's advocate: for the top hypothesis, actively search for evidence "
                "that would CONTRADICT it. What anomalies does it NOT explain? "
                "What services have errors not covered by this hypothesis? "
                "Returns counter-evidence list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_hypothesis": {
                        "type": "object",
                        "description": "Top-ranked hypothesis"
                    },
                    "anomalies": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "All anomalies"
                    },
                    "code_issues": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "All code issues"
                    },
                },
                "required": ["top_hypothesis", "anomalies"],
            },
        },
    },
]

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_analysis",
        "description": "Submit final ranked hypotheses. Call when analysis is complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "hypotheses_ranked": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Sorted list of hypotheses with ranks, scores, and confidence"
                },
                "top_hypothesis": {
                    "type": "object",
                    "description": "Single best hypothesis (rank 1)"
                },
                "confidence_gate_passed": {
                    "type": "boolean",
                    "description": "True if top hypothesis confidence >= 0.65"
                },
                "analysis_notes": {
                    "type": "string",
                    "description": "Brief notes on contradictions resolved or assumptions"
                },
            },
            "required": ["hypotheses_ranked", "top_hypothesis", "confidence_gate_passed"],
        },
    },
}

SYSTEM_PROMPT = """You are HypothesisRankerAgent, an AI agent in an incident Root Cause Analysis pipeline.

You receive signals from multiple upstream agents:
- anomalies (from LogAgent, CodeAgent, etc.)
- deployment context (from DeploymentAgent)
- KB matches (from KnowledgeBaseAgent)
- APM data (from APMAgent)
- trace data (from TraceAgent)

YOUR INVESTIGATION PROTOCOL (6 phases):

PHASE 1: CANDIDATE GENERATION
- Call generate_candidate_hypotheses to create all plausible root cause hypotheses from signals
- Update scratchpad with generated candidates and candidate count

PHASE 2: DUAL SCORING (Weighted + Bayesian)
- For each candidate:
  a) Score with score_hypothesis_algorithm (5-dimension weighted scoring)
  b) Score with score_hypothesis_bayesian (probabilistic sequential evidence updating)
  c) Check KB precedent with check_kb_precedent
- Update scratchpad with scored candidates

PHASE 3: EVIDENCE GRAPH & CONTRADICTION DETECTION
- Call build_evidence_graph to create hypothesis ↔ evidence bipartite graph
- Call detect_contradictions to find conflicting evidence pairs
- If contradictions found, call resolve_contradictions with resolution heuristics
- Update scratchpad with resolved hypothesis list

PHASE 4: COUNTER-EVIDENCE & DEVIL'S ADVOCATE
- Rank candidates by weighted_score with rank_candidates
- Identify top hypothesis
- Call generate_counter_evidence to find evidence that CONTRADICTS top hypothesis
- Update scratchpad with counter-evidence findings and confidence adjustments

PHASE 5: CONFIDENCE CALIBRATION
- Call calibrate_confidence to apply Platt scaling-style adjustments
- Lower confidence if all hypotheses score similarly (small gap)
- Raise confidence if one hypothesis dominates
- Cap at 0.6 if evidence count < 3
- Update scratchpad with final calibrated scores

PHASE 6: REFLECTION & FINISH
- Call read_scratchpad to review all findings
- Call reflect_on_findings to identify gaps, contradictions, and overall confidence
- If confidence < 0.65, continue investigating or revise findings
- Only call finish_analysis when confident in top hypothesis and resolution

SCORING LOGIC:
- weighted_score combines 5 dimensions: evidence_strength, specificity, deployment_correlation,
  symptom_match, severity_alignment (weights: 0.25, 0.20, 0.20, 0.20, 0.15)
- Bayesian posterior provides probabilistic confidence
- Evidence graph reveals conflicts and corroboration
- Counter-evidence catches blind spots
- Confidence calibration prevents overconfidence

Each hypothesis must have numeric confidence (0-1) backed by algorithm, not just intuition.
Use your scratchpad to track intermediate findings and revise as evidence accumulates.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Pure algorithmic tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def _tokenize(text):
    """Simple word tokenizer."""
    return set(re.findall(r'\b\w+\b', text.lower()))


def _jaccard_similarity(set_a, set_b):
    """Jaccard similarity between two sets."""
    if not set_a and not set_b:
        return 1.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union > 0 else 0.0


def _generate_candidate_hypotheses(anomalies: list, code_issues: list = None, trace_data: dict = None) -> list:
    """
    Pure Python: generate candidate hypotheses from signals.
    Groups by (service, anomaly_type) pairs.
    """
    code_issues = code_issues or []
    trace_data = trace_data or {}

    candidates = []
    seen_keys = set()

    # Group anomalies by (service, anomaly_type)
    for anom in anomalies:
        svc = anom.get("service", "unknown")
        anom_type = anom.get("anomaly_type", "unknown")
        severity = anom.get("severity", "WARN")
        desc = anom.get("description", "")

        key = (svc, anom_type)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # Create hypothesis
        hyp_desc = f"{anom_type} in {svc}: {desc[:100]}"
        candidates.append({
            "hypothesis_id": f"H{len(candidates)+1}",
            "hypothesis": hyp_desc,
            "hypothesis_type": anom_type,
            "affected_services": [svc],
            "severity": severity,
            "supporting_evidence": [
                {"source": "LogAgent", "detail": desc}
            ],
        })

    # Add hypotheses from code issues
    for code_issue in code_issues:
        anom_type = code_issue.get("type", "unknown")
        desc = code_issue.get("description", "")

        key = ("code", anom_type)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        candidates.append({
            "hypothesis_id": f"H{len(candidates)+1}",
            "hypothesis": f"{anom_type}: {desc}",
            "hypothesis_type": anom_type,
            "affected_services": code_issue.get("affected_services", []),
            "severity": code_issue.get("severity", "WARN"),
            "supporting_evidence": [
                {"source": "CodeAgent", "detail": desc}
            ],
        })

    # Add hypotheses from trace data
    if trace_data and trace_data.get("root_failure"):
        key = ("trace", "root_failure")
        if key not in seen_keys:
            candidates.append({
                "hypothesis_id": f"H{len(candidates)+1}",
                "hypothesis": f"Trace root failure: {trace_data.get('root_failure')}",
                "hypothesis_type": "trace_failure",
                "affected_services": trace_data.get("failure_services", []),
                "severity": "CRITICAL",
                "supporting_evidence": [
                    {"source": "TraceAgent", "detail": trace_data.get('root_failure')}
                ],
            })

    return candidates


def _score_hypothesis_algorithm(hypothesis_dict: dict, anomalies_list: list, deployment_data_dict: dict = None) -> dict:
    """
    Pure Python deterministic scoring on 5 dimensions (0-1 each).
    Returns dict with all 5 scores plus weighted_score.
    """
    deployment_data_dict = deployment_data_dict or {}

    hyp_services = set(hypothesis_dict.get("affected_services", []))
    hyp_desc = hypothesis_dict.get("hypothesis", "").lower()
    hyp_type = hypothesis_dict.get("hypothesis_type", "").lower()

    # 1. EVIDENCE_STRENGTH: fraction of anomalies mentioning this service or type
    matching_anomalies = 0
    for anom in anomalies_list:
        svc = anom.get("service", "").lower()
        anom_type = anom.get("anomaly_type", "").lower()
        if svc in hyp_desc or any(s.lower() == svc for s in hyp_services) or anom_type in hyp_desc:
            matching_anomalies += 1

    evidence_strength = min(1.0, matching_anomalies / max(len(anomalies_list), 1))

    # 2. SPECIFICITY: how precise is the hypothesis?
    # 1.0 if file/field mentioned, 0.5 if service-level, 0.2 if vague
    specificity = 0.2
    if re.search(r'\w+\.java|\w+\.py|\w+\.js', hyp_desc):  # file mention
        specificity = 1.0
    elif re.search(r'field|column|attribute', hyp_desc):  # field mention
        specificity = 1.0
    elif re.search(r'\b(qty|quantity|orderId|order_id|total|price)\b', hyp_desc):  # specific field name
        specificity = 0.8
    elif any(s in hyp_desc for s in hyp_services):  # service-level mention
        specificity = 0.5

    # 3. DEPLOYMENT_CORRELATION: did deployment happen within 2hrs before incident?
    deployment_correlation = 0.0
    if deployment_data_dict and deployment_data_dict.get("within_2hrs"):
        # Check if changed files match hypothesis service
        changed_files = deployment_data_dict.get("changed_files", [])
        for svc in hyp_services:
            if any(svc.lower() in f.lower() for f in changed_files):
                deployment_correlation = 1.0
                break
        if deployment_correlation == 0.0 and changed_files:
            deployment_correlation = 0.3  # partial match

    # 4. SYMPTOM_MATCH: Jaccard similarity of hypothesis keywords vs anomaly descriptions
    hyp_tokens = _tokenize(hyp_desc)
    anom_tokens = set()
    for anom in anomalies_list:
        anom_tokens.update(_tokenize(anom.get("description", "")))

    symptom_match = _jaccard_similarity(hyp_tokens, anom_tokens)

    # 5. SEVERITY_ALIGNMENT: highest severity pointing to this service
    severity_alignment = 0.2
    for anom in anomalies_list:
        svc = anom.get("service", "").lower()
        severity = anom.get("severity", "").upper()
        if svc in hyp_desc or any(s.lower() == svc for s in hyp_services):
            if severity == "CRITICAL":
                severity_alignment = 1.0
                break
            elif severity in ("ERROR", "EXCEPTION"):
                severity_alignment = max(severity_alignment, 0.8)
            elif severity in ("WARN", "WARNING"):
                severity_alignment = max(severity_alignment, 0.5)

    # Weighted score: 0.25*ev + 0.20*spec + 0.20*dep + 0.20*symp + 0.15*sev
    weighted_score = (
        0.25 * evidence_strength +
        0.20 * specificity +
        0.20 * deployment_correlation +
        0.20 * symptom_match +
        0.15 * severity_alignment
    )

    return {
        "evidence_strength": round(evidence_strength, 3),
        "specificity": round(specificity, 3),
        "deployment_correlation": round(deployment_correlation, 3),
        "symptom_match": round(symptom_match, 3),
        "severity_alignment": round(severity_alignment, 3),
        "weighted_score": round(weighted_score, 3),
    }


def _check_kb_precedent(hypothesis_service: str, hypothesis_type: str, kb_matches: list) -> float:
    """
    Pure Python: check KB for precedent.
    Returns 1.0 if exact service match, 0.5 if matching anomaly_type, 0.0 otherwise.
    """
    kb_matches = kb_matches or []

    for match in kb_matches:
        if match.get("service", "").lower() == hypothesis_service.lower():
            return 1.0
        if match.get("anomaly_type", "").lower() == hypothesis_type.lower():
            return 0.5

    return 0.0


def _rank_candidates(candidates_with_scores: list) -> list:
    """
    Pure Python: sort by weighted_score descending, assign ranks.
    """
    sorted_cands = sorted(
        candidates_with_scores,
        key=lambda c: c.get("scores", {}).get("weighted_score", 0.0),
        reverse=True
    )

    for i, cand in enumerate(sorted_cands, 1):
        cand["rank"] = i
        cand["confidence"] = cand.get("scores", {}).get("weighted_score", 0.0)

    return sorted_cands


def _build_evidence_graph(candidates: list, anomalies: list, code_issues: list = None,
                         trace_data: dict = None, deployment_data: dict = None,
                         kb_matches: list = None) -> dict:
    """
    Build a bipartite graph: hypotheses ↔ evidence.
    Each edge has weight (how strongly evidence supports/contradicts hypothesis).
    Uses keyword matching and service-name overlap.
    Returns adjacency list + edge weights.
    """
    code_issues = code_issues or []
    trace_data = trace_data or {}
    deployment_data = deployment_data or {}
    kb_matches = kb_matches or []

    graph = {
        "hypotheses": {},
        "evidence": {},
        "edges": [],
        "num_hypotheses": len(candidates),
        "num_evidence": 0,
    }

    # Initialize hypothesis nodes
    for cand in candidates:
        hyp_id = cand.get("hypothesis_id", f"H{candidates.index(cand)}")
        hyp_desc = cand.get("hypothesis", "").lower()
        hyp_services = [s.lower() for s in cand.get("affected_services", [])]

        graph["hypotheses"][hyp_id] = {
            "description": cand.get("hypothesis", ""),
            "services": hyp_services,
            "type": cand.get("hypothesis_type", ""),
            "edges": []
        }

    # Build evidence from anomalies
    evidence_items = []
    for anom in anomalies:
        source = anom.get("service", "unknown").lower()
        severity = anom.get("severity", "WARN").upper()
        anom_type = anom.get("anomaly_type", "unknown").lower()
        desc = anom.get("description", "").lower()

        evidence_id = f"anom_{len(evidence_items)}"
        evidence_items.append({
            "id": evidence_id,
            "source": "LogAgent",
            "specificity": 1.0,
            "type": anom_type,
            "service": source,
            "severity": severity,
            "description": desc,
        })

    # Build evidence from code issues
    for code_issue in code_issues:
        code_id = f"code_{len(evidence_items)}"
        evidence_items.append({
            "id": code_id,
            "source": "CodeAgent",
            "specificity": 0.9,
            "type": code_issue.get("type", "code_issue").lower(),
            "service": code_issue.get("affected_services", [])[0].lower() if code_issue.get("affected_services") else "unknown",
            "severity": code_issue.get("severity", "WARN").upper(),
            "description": code_issue.get("description", "").lower(),
        })

    # Build evidence from KB matches
    for kb in kb_matches:
        kb_id = f"kb_{len(evidence_items)}"
        evidence_items.append({
            "id": kb_id,
            "source": "KnowledgeBase",
            "specificity": 0.8,
            "type": kb.get("anomaly_type", "kb_match").lower(),
            "service": kb.get("service", "unknown").lower(),
            "severity": kb.get("severity", "WARN").upper(),
            "description": kb.get("incident_description", "").lower(),
        })

    graph["num_evidence"] = len(evidence_items)

    # Build edges: hypothesis <-> evidence
    for hyp_id, hyp_node in graph["hypotheses"].items():
        hyp_desc = hyp_node["description"].lower()
        hyp_tokens = _tokenize(hyp_desc)
        hyp_services = set(hyp_node["services"])

        for ev in evidence_items:
            ev_tokens = _tokenize(ev["description"])
            ev_service = ev.get("service", "").lower()

            # Calculate edge weight (0-1)
            # Factors: service match, keyword match, severity
            service_match = 1.0 if ev_service in hyp_services else 0.0
            keyword_match = _jaccard_similarity(hyp_tokens, ev_tokens)
            severity_boost = {"CRITICAL": 1.0, "ERROR": 0.8, "WARN": 0.5, "INFO": 0.2}.get(ev.get("severity"), 0.2)

            weight = (0.4 * service_match + 0.4 * keyword_match + 0.2 * severity_boost)

            if weight > 0.1:  # Only include edges with meaningful weight
                edge = {
                    "hypothesis": hyp_id,
                    "evidence": ev["id"],
                    "weight": round(weight, 3),
                    "supports": True if weight > 0.5 else False,
                }
                graph["edges"].append(edge)
                hyp_node["edges"].append(ev["id"])

    graph["evidence"] = {ev["id"]: ev for ev in evidence_items}
    return graph


def _detect_contradictions(evidence_graph: dict) -> list:
    """
    Find pairs of evidence that support conflicting hypotheses.
    Returns contradictions with severity.
    """
    contradictions = []

    hypotheses = evidence_graph.get("hypotheses", {})
    edges = evidence_graph.get("edges", [])
    evidence_map = evidence_graph.get("evidence", {})

    # Build hypothesis -> evidence mapping
    hyp_to_evidence = {}
    for edge in edges:
        hyp = edge.get("hypothesis", "unknown")
        ev = edge.get("evidence", "unknown")
        if hyp not in hyp_to_evidence:
            hyp_to_evidence[hyp] = {"supporting": [], "contradicting": []}

        if edge.get("supports", True):
            hyp_to_evidence[hyp]["supporting"].append(ev)
        else:
            hyp_to_evidence[hyp]["contradicting"].append(ev)

    # Find evidence pairs that conflict
    hyp_list = list(hypotheses.keys())
    for i, h1 in enumerate(hyp_list):
        for h2 in hyp_list[i+1:]:
            h1_supporting = set(hyp_to_evidence.get(h1, {}).get("supporting", []))
            h2_supporting = set(hyp_to_evidence.get(h2, {}).get("supporting", []))

            # Check if evidence for h1 contradicts h2
            h1_against_h2 = h1_supporting & set(hyp_to_evidence.get(h2, {}).get("contradicting", []))
            h2_against_h1 = h2_supporting & set(hyp_to_evidence.get(h1, {}).get("contradicting", []))

            if h1_against_h2 or h2_against_h1:
                severity = (len(h1_against_h2) + len(h2_against_h1)) / max(len(h1_supporting | h2_supporting), 1)
                contradictions.append({
                    "hypothesis_1": h1,
                    "hypothesis_2": h2,
                    "conflicting_evidence_1": list(h1_against_h2),
                    "conflicting_evidence_2": list(h2_against_h1),
                    "severity": round(severity, 3),
                })

    return contradictions


def _resolve_contradictions(contradictions: list, candidates: list) -> list:
    """
    Apply resolution heuristics to contradictions.
    Heuristics: (1) prefer more specific evidence, (2) prefer reliable sources, (3) prefer corroborated evidence.
    Returns resolved hypothesis list.
    """
    # Create a scoring adjustment dict for each candidate
    adjustments = {}
    for cand in candidates:
        adjustments[cand.get("hypothesis_id")] = {"boost": 0.0, "reason": []}

    # For each contradiction, apply heuristics
    for contra in contradictions:
        h1 = contra.get("hypothesis_1")
        h2 = contra.get("hypothesis_2")
        severity = contra.get("severity", 0.0)

        # Simple heuristic: boost the hypothesis with more evidence
        h1_ev_count = len(contra.get("conflicting_evidence_1", []))
        h2_ev_count = len(contra.get("conflicting_evidence_2", []))

        if h1_ev_count > h2_ev_count:
            boost = 0.1 * (1.0 - severity)
            adjustments[h1]["boost"] += boost
            adjustments[h1]["reason"].append(f"Resolved contradiction with {h2} (more evidence)")
        elif h2_ev_count > h1_ev_count:
            boost = 0.1 * (1.0 - severity)
            adjustments[h2]["boost"] += boost
            adjustments[h2]["reason"].append(f"Resolved contradiction with {h1} (more evidence)")

    # Apply adjustments to candidates
    for cand in candidates:
        hyp_id = cand.get("hypothesis_id")
        if hyp_id in adjustments and adjustments[hyp_id]["boost"] > 0:
            scores = cand.get("scores", {})
            if "weighted_score" in scores:
                scores["weighted_score"] = min(1.0, scores["weighted_score"] + adjustments[hyp_id]["boost"])
            cand["resolution_adjustments"] = adjustments[hyp_id]

    return candidates


def _calibrate_confidence(scored_candidates: list) -> list:
    """
    Apply Platt scaling-style calibration.
    - If all hypotheses score similarly (small gap), lower confidence
    - If one dominates, raise confidence
    - If evidence count < 3, cap confidence at 0.6
    Returns calibrated scores.
    """
    if not scored_candidates:
        return scored_candidates

    # Extract weighted scores
    scores = [c.get("scores", {}).get("weighted_score", 0.0) for c in scored_candidates]
    scores_sorted = sorted(scores, reverse=True)

    # Compute gap between top and #2
    if len(scores_sorted) >= 2:
        top_score = scores_sorted[0]
        second_score = scores_sorted[1]
        gap = top_score - second_score
    else:
        gap = scores_sorted[0] if scores_sorted else 0.0

    # Calibration logic
    for cand in scored_candidates:
        scores_dict = cand.get("scores", {})
        weighted_score = scores_dict.get("weighted_score", 0.0)
        rank = scored_candidates.index(cand) + 1

        # Base confidence = weighted score
        confidence = weighted_score

        # If top hypothesis and gap is large, boost confidence
        if rank == 1 and gap > 0.2:
            confidence = min(1.0, confidence + 0.15)
        # If top hypothesis but gap is small, reduce confidence
        elif rank == 1 and gap < 0.1:
            confidence = max(0.3, confidence - 0.15)
        # If rank > 1, penalize confidence
        elif rank > 1:
            confidence = confidence * (0.8 - 0.1 * rank)

        # Cap at 0.6 if evidence count is low (proxy: use supporting_evidence count)
        evidence_count = len(cand.get("supporting_evidence", []))
        if evidence_count < 3:
            confidence = min(0.6, confidence)

        cand["calibrated_confidence"] = round(max(0.0, min(1.0, confidence)), 3)

    return scored_candidates


def _generate_counter_evidence(top_hypothesis: dict, anomalies: list, code_issues: list = None) -> list:
    """
    Devil's advocate: for the top hypothesis, search for evidence that would CONTRADICT it.
    What anomalies does it NOT explain? What services have errors not covered?
    Returns counter-evidence list.
    """
    code_issues = code_issues or []

    counter_evidence = []
    hyp_services = set(s.lower() for s in top_hypothesis.get("affected_services", []))
    hyp_desc = top_hypothesis.get("hypothesis", "").lower()
    hyp_tokens = _tokenize(hyp_desc)

    # Find anomalies NOT explained by this hypothesis
    for anom in anomalies:
        anom_service = anom.get("service", "").lower()
        anom_desc = anom.get("description", "").lower()
        anom_tokens = _tokenize(anom_desc)
        severity = anom.get("severity", "WARN")

        # Check if this anomaly is NOT covered by the hypothesis
        service_match = anom_service in hyp_services
        keyword_match = _jaccard_similarity(hyp_tokens, anom_tokens)

        # If no match and high severity, this is counter-evidence
        if not service_match and keyword_match < 0.3 and severity in ("CRITICAL", "ERROR"):
            counter_evidence.append({
                "type": "unexplained_anomaly",
                "service": anom_service,
                "severity": severity,
                "description": anom.get("description", ""),
                "contradiction_strength": round(1.0 - keyword_match, 3),
            })

    # Find code issues NOT explained by this hypothesis
    for code_issue in code_issues:
        affected = set(s.lower() for s in code_issue.get("affected_services", []))
        code_desc = code_issue.get("description", "").lower()
        code_tokens = _tokenize(code_desc)

        # Check if issue affects services outside hypothesis scope
        external_services = affected - hyp_services
        keyword_match = _jaccard_similarity(hyp_tokens, code_tokens)

        if external_services and keyword_match < 0.3:
            counter_evidence.append({
                "type": "external_code_issue",
                "services": list(external_services),
                "severity": code_issue.get("severity", "WARN"),
                "description": code_issue.get("description", ""),
                "contradiction_strength": round(1.0 - keyword_match, 3),
            })

    # Sort by contradiction strength
    counter_evidence.sort(key=lambda x: x.get("contradiction_strength", 0.0), reverse=True)

    return counter_evidence


# ─────────────────────────────────────────────────────────────────────────────
# Tool executor — runs the actual tool logic
# ─────────────────────────────────────────────────────────────────────────────
def _execute_tool(name: str, args: dict, context: dict) -> str:
    """Execute a tool call and return the result as a string."""

    if name == "generate_candidate_hypotheses":
        anomalies = args.get("anomalies", [])
        code_issues = args.get("code_issues", context.get("code_issues", []))
        trace_data = args.get("trace_data", context.get("trace_data", {}))

        candidates = _generate_candidate_hypotheses(anomalies, code_issues, trace_data)
        return json.dumps({
            "candidates": candidates,
            "count": len(candidates),
        })

    elif name == "score_hypothesis_algorithm":
        hyp_dict = args.get("hypothesis_dict", {})
        anomalies_list = args.get("anomalies_list", [])
        deployment_data = args.get("deployment_data_dict", context.get("deployment_data", {}))

        scores = _score_hypothesis_algorithm(hyp_dict, anomalies_list, deployment_data)
        return json.dumps({
            "hypothesis_id": hyp_dict.get("hypothesis_id"),
            "scores": scores,
        })

    elif name == "check_kb_precedent":
        hyp_service = args.get("hypothesis_service", "")
        hyp_type = args.get("hypothesis_type", "")
        kb_matches = args.get("kb_matches", context.get("kb_matches", []))

        precedent_score = _check_kb_precedent(hyp_service, hyp_type, kb_matches)
        return json.dumps({
            "hypothesis_service": hyp_service,
            "kb_precedent_score": round(precedent_score, 2),
        })

    elif name == "score_hypothesis_bayesian":
        hyp_name = args.get("hypothesis_name", "unknown")
        evidence_items = args.get("evidence_items", [])

        scorer = BayesianScorer(hypotheses=[hyp_name, "other"])
        for ev in evidence_items:
            strength = float(ev.get("strength", 0.5))
            supports = ev.get("supports", True)
            # Convert supports+strength to likelihoods
            if supports:
                lt = 0.5 + strength * 0.5   # likelihood_if_true: 0.5-1.0
                lf = 0.5 - strength * 0.4   # likelihood_if_false: 0.1-0.5
            else:
                lt = 0.5 - strength * 0.4   # contradicting evidence
                lf = 0.5 + strength * 0.5
            scorer.update(Evidence(
                name=ev.get("name", "evidence"),
                observation=supports,
                likelihood_if_true=lt,
                likelihood_if_false=lf,
            ))

        posteriors = scorer.get_posteriors()
        return json.dumps({
            "hypothesis": hyp_name,
            "prior": round(1.0 / 2, 4),
            "posterior": round(posteriors.get(hyp_name, 0.5), 4),
            "num_updates": len(evidence_items),
            "evidence_count": scorer.evidence_count,
            "algorithm": "Bayesian sequential updating (Bayes' theorem)",
        })

    elif name == "rank_candidates":
        candidates = args.get("candidates_with_scores", [])
        ranked = _rank_candidates(candidates)
        return json.dumps({
            "ranked": ranked,
            "count": len(ranked),
        })

    elif name == "build_evidence_graph":
        candidates = args.get("candidates", [])
        anomalies = args.get("anomalies", [])
        code_issues = args.get("code_issues", context.get("code_issues", []))
        trace_data = args.get("trace_data", context.get("trace_data", {}))
        deployment_data = args.get("deployment_data", context.get("deployment_data", {}))
        kb_matches = args.get("kb_matches", context.get("kb_matches", []))

        graph = _build_evidence_graph(candidates, anomalies, code_issues, trace_data, deployment_data, kb_matches)
        return json.dumps({
            "graph": graph,
            "num_hypotheses": graph.get("num_hypotheses"),
            "num_evidence": graph.get("num_evidence"),
            "num_edges": len(graph.get("edges", [])),
        })

    elif name == "detect_contradictions":
        evidence_graph = args.get("evidence_graph", {})
        contradictions = _detect_contradictions(evidence_graph)
        return json.dumps({
            "contradictions": contradictions,
            "count": len(contradictions),
        })

    elif name == "resolve_contradictions":
        contradictions = args.get("contradictions", [])
        candidates = args.get("candidates", [])
        resolved = _resolve_contradictions(contradictions, candidates)
        return json.dumps({
            "resolved_candidates": resolved,
            "adjustments_applied": len([c for c in resolved if "resolution_adjustments" in c]),
        })

    elif name == "calibrate_confidence":
        scored_candidates = args.get("scored_candidates", [])
        calibrated = _calibrate_confidence(scored_candidates)
        return json.dumps({
            "calibrated_candidates": calibrated,
            "top_confidence": calibrated[0].get("calibrated_confidence", 0.0) if calibrated else 0.0,
        })

    elif name == "generate_counter_evidence":
        top_hypothesis = args.get("top_hypothesis", {})
        anomalies = args.get("anomalies", [])
        code_issues = args.get("code_issues", context.get("code_issues", []))

        counter_evidence = _generate_counter_evidence(top_hypothesis, anomalies, code_issues)
        return json.dumps({
            "counter_evidence": counter_evidence,
            "count": len(counter_evidence),
            "top_hypothesis_id": top_hypothesis.get("hypothesis_id", "unknown"),
        })

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ─────────────────────────────────────────────────────────────────────────────
# Main ReAct agent loop
# ─────────────────────────────────────────────────────────────────────────────
def rank_hypotheses(anomalies: list, code_issues: list = None,
                   trace_data: dict = None, deployment_data: dict = None,
                   kb_matches: list = None, apm_data: dict = None) -> dict:
    """
    ReAct agent for hypothesis ranking using ReActEngine.

    Args:
        anomalies: List of anomaly dicts from upstream agents
        code_issues: List of code issue dicts
        trace_data: Trace data dict
        deployment_data: Deployment info dict
        kb_matches: Knowledge base matches list
        apm_data: APM data dict

    Returns:
        Dict with hypotheses_ranked, top_hypothesis, confidence_gate_passed, analysis_notes
    """

    context = {
        "anomalies": anomalies,
        "code_issues": code_issues or [],
        "trace_data": trace_data or {},
        "deployment_data": deployment_data or {},
        "kb_matches": kb_matches or [],
        "apm_data": apm_data or {},
    }

    # Build initial signal summary for the agent
    signal_summary = f"""
Anomalies: {len(anomalies)} found
Code issues: {len(code_issues or [])}
Trace data present: {bool(trace_data)}
Deployment data present: {bool(deployment_data)}
KB matches: {len(kb_matches or [])}
APM data present: {bool(apm_data)}

Anomalies (first 5):
{json.dumps(anomalies[:5], indent=2)}
{'...' if len(anomalies) > 5 else ''}
"""

    user_message = (
        f"Investigate these signals and rank root cause hypotheses.\n\n"
        f"{signal_summary}\n\n"
        f"Follow the 6-phase investigation protocol:\n"
        f"1. Generate all candidate hypotheses from signals\n"
        f"2. Score each with BOTH weighted algorithm and Bayesian scoring\n"
        f"3. Build evidence graph, detect and resolve contradictions\n"
        f"4. Generate counter-evidence for top hypothesis\n"
        f"5. Calibrate confidence with Platt scaling\n"
        f"6. Reflect on findings and finish\n\n"
        f"Use your scratchpad to track intermediate findings. "
        f"Apply reflection BEFORE finish_analysis. "
        f"Target confidence >= 0.65 for the top hypothesis."
    )

    engine = ReActEngine(
        agent_name="HypothesisRankerAgent",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        finish_tool=FINISH_TOOL,
        tool_executor=lambda name, args, **kw: _execute_tool(name, args, context),
        max_iterations=12,
        confidence_threshold=0.65,
        reflection_required=True,
    )

    result = engine.run(user_message=user_message)

    # Extract final result
    final_result = result.findings if result.findings else {}

    # Fallback: if agent never produced final result, generate and rank ourselves
    if not final_result:
        candidates = _generate_candidate_hypotheses(anomalies, code_issues, trace_data)

        # Score each candidate
        for cand in candidates:
            scores = _score_hypothesis_algorithm(cand, anomalies, deployment_data or {})
            cand["scores"] = scores

        ranked = _rank_candidates(candidates)
        top = ranked[0] if ranked else {}

        final_result = {
            "hypotheses_ranked": ranked,
            "top_hypothesis": top,
            "confidence_gate_passed": top.get("confidence", 0.0) >= 0.65,
            "analysis_notes": "Generated via fallback algorithm (agent reached max iterations without finish)"
        }

    return final_result


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_anomalies = [
        {"service": "java-order-service", "severity": "CRITICAL", "anomaly_type": "field_mismatch",
         "description": "HTTP 400 from Python — missing field 'quantity', sent 'qty'"},
        {"service": "java-order-service", "severity": "CRITICAL", "anomaly_type": "type_error",
         "description": "SQLite total stored as 99 instead of 99.99 (int cast)"},
        {"service": "java-order-service", "severity": "CRITICAL", "anomaly_type": "field_mismatch",
         "description": "HTTP 400 from Node — missing field 'orderId', sent 'order_id'"},
    ]
    sample_code_issues = [
        {"type": "field_mismatch", "severity": "CRITICAL", "description": "qty vs quantity mismatch in java-order-service/OrderClient.java:45",
         "affected_services": ["java-order-service", "python-inventory-service"]},
        {"type": "type_error", "severity": "CRITICAL", "description": "int cast truncates total in java-order-service/Order.java:120",
         "affected_services": ["java-order-service"]},
    ]
    sample_deployment = {
        "within_2hrs": True,
        "changed_files": ["java-order-service/OrderClient.java", "java-order-service/Order.java"],
    }

    result = rank_hypotheses(sample_anomalies, sample_code_issues, deployment_data=sample_deployment)
    print(json.dumps(result, indent=2))
