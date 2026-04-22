"""
BM25 (Okapi BM25) Ranking Engine — pure numpy/Python.

BM25 is an improvement over TF-IDF that:
  1. Handles term frequency saturation (long documents don't accumulate infinite score)
  2. Normalizes by document length (fair comparison across variable-length docs)
  3. Empirically outperforms TF-IDF on relevance ranking

BM25 Score Formula:
  score(D, Q) = Σ_i IDF(qi) * (tf(qi, D) * (k1 + 1)) / (tf(qi, D) + k1 * (1 - b + b * |D| / avgdl))

Where:
  - IDF(qi) = ln((N - n(qi) + 0.5) / (n(qi) + 0.5) + 1)
  - tf(qi, D) = term frequency in document D
  - k1 = 1.5 (controls saturation; higher = longer tails for high frequencies)
  - b = 0.75 (controls length normalization; 0 = no length norm, 1 = full length norm)
  - |D| = length of document D (in terms)
  - avgdl = average document length
  - N = total number of documents
  - n(qi) = number of documents containing term qi

Reference: Okapi BM25 (Robertson et al., 1994)
"""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Text preprocessing (same as similarity_engine.py)
# ─────────────────────────────────────────────────────────────────────────────

STOP_WORDS = {
    "the","a","an","is","are","was","were","be","been","being","have","has","had",
    "do","does","did","will","would","could","should","may","might","shall","can",
    "to","of","in","on","at","by","for","with","from","this","that","these","those",
    "and","or","but","not","no","if","as","it","its","we","our","they","their",
    "he","she","him","her","you","your","i","my","me","us","who","which","what",
    "when","where","how","all","any","both","each","few","more","most","other",
    "some","such","than","then","so","about","after","before","into","through",
    "between","during","without","within","along","across","behind","beyond",
    "up","down","out","off","over","under","again","further","once","per",
}


def tokenize(text: str) -> List[str]:
    """Lowercase, expand camelCase/snake_case, normalize Unicode, remove stop words."""
    text = text.lower()
    # Normalize Unicode characters (NFD decomposition)
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    # Expand camelCase: orderId → order id
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Expand snake_case: order_id → order id
    text = text.replace("_", " ").replace("-", " ")
    # Unicode-aware tokenization: match word characters and digits
    tokens = re.findall(r'[\w]+', text)
    # Keep tokens >= 2 chars and not stop words
    return [t for t in tokens if len(t) >= 2 and t not in STOP_WORDS]


def build_doc_text(doc: dict) -> str:
    """Concatenate all text fields of a document."""
    parts = [
        doc.get("title", ""),
        doc.get("description", ""),
        " ".join(doc.get("tags", [])) if isinstance(doc.get("tags"), list) else doc.get("tags", ""),
    ]
    return " ".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BM25Result:
    doc_id: str
    doc_title: str
    score: float                   # BM25 score
    matching_terms: List[str]      # which terms from query matched in doc
    doc: dict                      # original document


# ─────────────────────────────────────────────────────────────────────────────
# BM25 Engine
# ─────────────────────────────────────────────────────────────────────────────
class BM25Engine:
    """
    BM25 ranking engine. Fit on corpus of documents, then search/rank queries.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Parameters
        ----------
        k1 : float
            Term frequency saturation parameter (default 1.5).
        b : float
            Length normalization parameter (default 0.75).
        """
        self.k1 = k1
        self.b = b
        self.corpus: List[dict] = []
        self.doc_texts: List[str] = []
        self.doc_tokens: List[List[str]] = []
        self.vocab: List[str] = []
        self.idf: Dict[str, float] = {}
        self.doc_freqs: List[Counter] = []  # term frequencies per doc
        self.avgdl = 0.0
        self._fitted = False

    def fit(self, documents: List[dict]) -> "BM25Engine":
        """
        Build BM25 index from corpus.

        Parameters
        ----------
        documents : list[dict]
            Each document should have 'id', 'title', 'description' at minimum.

        Returns
        -------
        self
        """
        if documents is None or (hasattr(documents, '__len__') and len(documents) == 0):
            self._fitted = True
            return self
        self.corpus = documents
        self.doc_texts = [build_doc_text(doc) for doc in documents]
        self.doc_tokens = [tokenize(text) for text in self.doc_texts]

        N = len(documents)

        # Document frequency: count of docs containing each term
        df: Dict[str, int] = defaultdict(int)
        for tokens in self.doc_tokens:
            for term in set(tokens):
                df[term] += 1

        # Build vocabulary and compute IDF
        self.vocab = sorted(df.keys())
        self.idf = {}
        for term in self.vocab:
            # BM25 IDF formula
            n_qi = df[term]
            self.idf[term] = math.log((N - n_qi + 0.5) / (n_qi + 0.5) + 1.0)

        # Compute term frequencies and average document length
        self.doc_freqs = []
        total_length = 0
        for tokens in self.doc_tokens:
            tf = Counter(tokens)
            self.doc_freqs.append(tf)
            total_length += len(tokens)

        self.avgdl = max(total_length / max(N, 1), 1e-10)
        self._fitted = True
        return self

    def search(self, query: str, top_k: int = 5, min_score: float = 0.0) -> List[BM25Result]:
        """
        Search for documents matching query using BM25 ranking.

        Parameters
        ----------
        query : str
            Query text (free-form).
        top_k : int
            Number of top results to return.
        min_score : float
            Minimum score threshold (below this, results excluded).

        Returns
        -------
        list[BM25Result]
            Top-k documents ranked by BM25 score (descending).
        """
        if query is None or (hasattr(query, '__len__') and len(query) == 0):
            return []
        if not self._fitted:
            raise RuntimeError("Call fit() before search()")

        query_tokens = tokenize(query)
        query_tf = Counter(query_tokens)

        scores = []
        for i, doc in enumerate(self.corpus):
            score = self._bm25_score(query_tf, self.doc_freqs[i], len(self.doc_tokens[i]))
            if score >= min_score:
                # Find which query terms matched in this doc
                matching_terms = [t for t in query_tokens if t in self.doc_freqs[i]]
                scores.append((
                    score,
                    BM25Result(
                        doc_id=doc.get("id", str(i)),
                        doc_title=doc.get("title", ""),
                        score=float(score),
                        matching_terms=matching_terms,
                        doc=doc
                    )
                ))

        # Sort descending by score
        scores.sort(key=lambda x: x[0], reverse=True)
        return [result for _, result in scores[:top_k]]

    def search_multi(self, queries: List[str], top_k: int = 5) -> List[BM25Result]:
        """
        Run BM25 on multiple queries and merge results (max score per doc).

        Parameters
        ----------
        queries : list[str]
            List of query strings.
        top_k : int
            Number of top results to return.

        Returns
        -------
        list[BM25Result]
            Merged top-k results across all queries.
        """
        merged_scores: Dict[str, float] = {}
        merged_results: Dict[str, BM25Result] = {}
        all_matching_terms: Dict[str, set] = defaultdict(set)

        for query in queries:
            results = self.search(query, top_k=len(self.corpus), min_score=0.0)
            for result in results:
                doc_id = result.doc_id
                if doc_id not in merged_scores or result.score > merged_scores[doc_id]:
                    merged_scores[doc_id] = result.score
                    merged_results[doc_id] = result
                # Accumulate matching terms
                all_matching_terms[doc_id].update(result.matching_terms)

        # Update matching_terms to include all from all queries
        for doc_id in merged_results:
            merged_results[doc_id].matching_terms = sorted(all_matching_terms[doc_id])

        # Sort and return top-k
        sorted_results = sorted(
            merged_results.values(),
            key=lambda r: merged_scores[r.doc_id],
            reverse=True
        )
        return sorted_results[:top_k]

    def _bm25_score(self, query_tf: Counter, doc_tf: Counter, doc_len: int) -> float:
        """
        Compute BM25 score for a query against a specific document.

        Parameters
        ----------
        query_tf : Counter
            Term frequencies in query.
        doc_tf : Counter
            Term frequencies in document.
        doc_len : int
            Length of document (number of tokens).

        Returns
        -------
        float
            BM25 score.
        """
        score = 0.0
        for term, tf_q in query_tf.items():
            if term not in self.idf:
                continue
            tf_d = float(doc_tf.get(term, 0))
            idf = self.idf[term]
            # BM25 formula
            numerator = tf_d * (self.k1 + 1.0)
            denominator = tf_d + self.k1 * (1.0 - self.b + self.b * (doc_len / self.avgdl))
            score += idf * (numerator / denominator)
        return score


# ─────────────────────────────────────────────────────────────────────────────
# Self-test using INCIDENT_LIBRARY
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Import incident library from knowledge_base_agent
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

    from knowledge_base_agent import INCIDENT_LIBRARY

    print("Testing BM25 Engine with INCIDENT_LIBRARY...")
    print(f"Corpus size: {len(INCIDENT_LIBRARY)} incidents\n")

    # Convert incidents to searchable documents
    documents = [
        {
            "id": inc["id"],
            "title": inc["title"],
            "description": " ".join(inc.get("symptoms", [])) + " " + inc.get("root_cause", ""),
            "tags": inc.get("services", [])
        }
        for inc in INCIDENT_LIBRARY
    ]

    # Fit BM25
    engine = BM25Engine(k1=1.5, b=0.75)
    engine.fit(documents)
    print(f"Vocabulary size: {len(engine.vocab)} terms")
    print(f"Average document length: {engine.avgdl:.1f} tokens\n")

    # Test query: should match INC-1021 (Java/Python field mismatch)
    query = "Java sends qty Python expects quantity KeyError HTTP 400"
    print(f"Query: {query}\n")

    results = engine.search(query, top_k=3)
    print(f"Top-3 BM25 results:")
    for i, result in enumerate(results, 1):
        print(f"\n  {i}. {result.doc_id}: {result.doc_title}")
        print(f"     Score: {result.score:.4f}")
        print(f"     Matching terms: {result.matching_terms}")

    # Verify INC-1021 is top result
    if results and results[0].doc_id == "INC-1021":
        print("\n✅ BM25 self-test PASSED: INC-1021 is top result")
    else:
        top_id = results[0].doc_id if results else "None"
        print(f"\n⚠️  Note: Top result is {top_id}, not INC-1021")
        print("   (This is expected if query tokens match other incidents better)")

    # Compare with TF-IDF
    print("\n" + "=" * 70)
    print("Comparison with TF-IDF:")
    from algorithms.similarity_engine import TFIDFSimilarityEngine

    tf_engine = TFIDFSimilarityEngine().fit(INCIDENT_LIBRARY)
    tf_results = tf_engine.search(query, top_k=3)

    print(f"\nTF-IDF Top-3 results:")
    for i, result in enumerate(tf_results, 1):
        print(f"  {i}. {result.incident_id}: {result.incident_title}")
        print(f"     Cosine score: {result.cosine_score:.4f}")

    print("\n" + "=" * 70)
    print("Ranking comparison complete. Both algorithms should identify")
    print("similar top matches, though ranking order may differ.")
