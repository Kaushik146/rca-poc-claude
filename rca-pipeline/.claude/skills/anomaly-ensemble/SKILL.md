---
name: anomaly-ensemble
description: Fallback anomaly detection for when Datadog Watchdog / Dynatrace Davis returns nothing. Autoencoder + Isolation Forest ensemble over APM feature vectors. Invoked by the signals agent ONLY when `vendor_anomalies` is empty for every candidate service — vendor AIOps is always authoritative when present.
---

# Anomaly Ensemble

## Why this exists — and when NOT to use it

Datadog Watchdog and Dynatrace Davis are trained on operator-labelled signals at enterprise scale. For 95% of incidents they will already surface the right anomaly. **Always prefer the vendor flags.**

This skill exists for the 5% where:
- The customer uses a Datadog/Dynatrace plan that doesn't include the AIOps tier
- The vendor's AIOps returns no flags despite obvious human-visible anomalies (rare, but happens on new services without enough history)
- The customer's observability stack is raw Loki/Prometheus without AIOps on top

## Precedence rule (hard contract)

The `signals` agent emits a `vendor_anomalies` array as a first-class field of
its output. That array is the authoritative anomaly signal. This skill MUST
NOT run when `vendor_anomalies` is non-empty for any candidate service in the
window — not as a "second opinion," not as "belt and braces." Two reasons:

1. Re-deriving Watchdog/Davis output with a generic ensemble downgrades a
   trained, labelled signal to an unlabelled one. That's strictly worse.
2. The time-window-selector skill already fuses `vendor_anomalies` into the
   posterior as the highest-weight prior. Running the ensemble on top would
   double-count the same underlying observation.

When the ensemble *does* fire (vendor array empty), the signals agent MUST
set `ensemble_fallback_used: true` on its output so the final report names
which signal the pipeline actually depended on. This is the honesty flag
Jothi asked for when reviewing with enterprise buyers — they want to know
whether a conclusion came from their paid AIOps tier or from our fallback.

## When to invoke

- The `signals` agent falls back to this skill **only** when `vendor_anomalies` is empty for all series in the window.

## Input

```json
{
  "features_by_service": {
    "<service>": [
      {"t": "2026-04-18T08:47Z", "cpu_pct": 0.82, "error_rate": 0.14,
       "latency_p99": 2400, "throughput": 120, "mem_pct": 0.71, "db_query_ms": 340},
      ...
    ]
  }
}
```

## What to do

Run `scripts/detect.py` via `Bash`. The script:

1. Runs `agents/algorithms/anomaly_detector.py` — autoencoder + isolation forest ensemble (0.55 / 0.45 weighted).
2. Returns per-sample anomaly scores with severity labels.

## Output

```json
{
  "anomalies": [
    {"service": "...", "t": "...", "score": 0.81,
     "severity": "HIGH", "top_feature": "error_rate"}
  ]
}
```

## Guardrails

- **Do not emit a score without a feature attribution.** `top_feature` must be set so the hypothesis agent can trace the anomaly back to a concrete metric.
- **Declare it as fallback in the report.** The final report must say "vendor AIOps returned nothing; fell back to ensemble" when this skill fired, and the signals agent's output must have `ensemble_fallback_used: true`. This preserves the "use vendor first" story when reviewing with Jothi.
- **Refuse to run when vendor anomalies exist.** If the caller passes in features while `vendor_anomalies` was non-empty upstream, that's a wiring bug — the signals agent should never have invoked this skill. The script should short-circuit with an explicit error rather than silently producing scores that will be double-counted downstream.
