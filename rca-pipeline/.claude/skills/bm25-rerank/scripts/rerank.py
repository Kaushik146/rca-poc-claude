#!/usr/bin/env python3
"""
bm25-rerank — rerank prior-incident candidates returned by MCP search.

Input (stdin JSON):
{
  "query": "...",
  "candidates": [{"id": "...", "title": "...", "text": "..."}, ...],
  "top_k": 5
}

Output (stdout JSON): { "reranked": [{id, title, score, original_rank, new_rank}, ...] }

Wraps agents/algorithms/bm25.BM25Engine so ranking is identical to the
non-Claude-Code path. BM25Engine expects documents with title + description
fields; we fold each candidate's "text" into "description".
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[4]
# Match the project convention: agents/ is on PYTHONPATH, algorithms/ is top-level.
sys.path.insert(0, str(REPO_ROOT / "agents"))

from algorithms.bm25 import BM25Engine  # type: ignore


LINE_NO = re.compile(r":\d+\b")                   # file.py:42   -> file.py
HEX_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")       # memory addresses
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def scrub(s: str) -> str:
    """Strip tokens that would dominate BM25 term-frequency spuriously."""
    if not s: return ""
    s = LINE_NO.sub("", s)
    s = HEX_ADDR.sub("", s)
    s = UUID_RE.sub("", s)
    return s


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "no_input_received"})); return
    payload = json.loads(raw)
    query = scrub(payload.get("query", ""))
    candidates = payload.get("candidates", []) or []
    top_k = int(payload.get("top_k", 5))

    if not candidates:
        print(json.dumps({"reranked": []})); return

    # Build BM25Engine-compatible docs. Keep the original index so we can
    # report `original_rank` after reranking.
    docs = []
    for i, c in enumerate(candidates):
        docs.append({
            "id": c.get("id", str(i)),
            "title": scrub(c.get("title", "")),
            "description": scrub(c.get("text", "")),
            "_orig_index": i,
        })

    engine = BM25Engine().fit(docs)
    results = engine.search(query, top_k=top_k, min_score=0.0)

    reranked = []
    for new_rank, r in enumerate(results):
        reranked.append({
            "id": r.doc_id,
            "title": r.doc_title,
            "score": round(float(r.score), 4),
            "matching_terms": r.matching_terms,
            "original_rank": r.doc.get("_orig_index"),
            "new_rank": new_rank,
        })
    print(json.dumps({"reranked": reranked}, indent=2))


if __name__ == "__main__":
    main()
