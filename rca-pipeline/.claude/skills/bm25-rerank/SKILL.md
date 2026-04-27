---
name: bm25-rerank
description: Reranks prior-incident candidates (Confluence postmortems, PagerDuty resolved incidents, ADO tickets) using BM25. Confluence's native search optimizes for freshness, not incident similarity - this skill fixes that on the read path without building a vector store.
---

# BM25 Rerank

## Why this exists

Confluence and ADO search are optimized for document freshness and keyword recall — not for ranking postmortems by how similar they are to the current incident. Rather than build and maintain a vector store of past postmortems (Jothi rejected that path — storage cost, staleness, maintenance), we do the rerank on the read path: pull top-20 candidates via MCP, rerank with BM25 locally, return top-5.

## When to invoke

- `prior-incident` agent calls this skill after pulling candidates from Atlassian / ADO / PagerDuty MCP.

## Input

```json
{
  "query": "database connection pool exhausted checkout-service",
  "candidates": [
    {"id": "CONF-123", "title": "...", "text": "...full page text..."},
    ...
  ],
  "top_k": 5
}
```

## What to do

Run `scripts/rerank.py` via `Bash`. The script:

1. Tokenizes query and candidates (lowercase, alphanumeric split, stopword-stripped).
2. Scores each candidate against the query using Okapi BM25 (`agents/algorithms/bm25.py`).
3. Returns the top-k candidates with scores.

## Output

```json
{
  "reranked": [
    {"id": "CONF-123", "title": "...", "score": 0.87, "original_rank": 14, "new_rank": 1},
    ...
  ]
}
```

## Guardrails

- **Never rewrite candidate content.** Return it as received from the MCP.
- **Strip stack-trace line numbers from the query before scoring.** Same bug, different run, different line numbers. We do not want line numbers dominating BM25 term frequency.
