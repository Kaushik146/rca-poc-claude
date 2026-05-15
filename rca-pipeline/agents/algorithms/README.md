# Algorithms

Two-tier layout. Skills and agents only ever import from the live tier.

## Live (used by skills)

- `cusum.py` — CUSUM change-point detection. Used by `time-window-selector`.
- `bm25.py` — Okapi BM25 ranking. Used by `bm25-rerank`.
- `anomaly_detector.py` — Autoencoder + Isolation Forest ensemble. Used by `anomaly-ensemble`.
- `isolation_forest.py` — Standalone, but imported by `anomaly_detector.py` as a component.

Each has direct unit test coverage under `scripts/tests/test_algorithms_*.py`.

## Experimental (not connected)

Under `experimental/`. These are scaffolding from earlier explorations and
are not wired into any skill or agent. Kept in the repo as references
for future work; removed from the live import surface so reviewers don't
have to guess which algorithms are real.

- `bayesian.py`
- `dbscan.py`
- `log_parser.py`
- `pagerank.py`
- `similarity_engine.py`
- `temporal_lstm.py`
- `trace_analyzer.py`

If any of these are promoted to live, they need: a calling skill, fixture
coverage in `.claude/fixtures/`, and direct unit tests under `scripts/tests/`.
