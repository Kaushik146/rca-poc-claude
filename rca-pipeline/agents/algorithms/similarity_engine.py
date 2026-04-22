"""
SimilarityEngine — TF-IDF + cosine similarity for incident matching.
NO LLM. Pure vector math.

Used by KnowledgeBaseAgent to rank past incidents against current anomalies
before GPT interprets only the top-k matches (not the whole library).

Algorithm:
  1. Build TF-IDF matrix from incident corpus (symptoms + root cause + title)
  2. Vectorise query (current anomaly descriptions)
  3. Cosine similarity between query vector and each incident vector
  4. Return ranked matches with score, matching terms, and matched incidents

TF-IDF from scratch:
  TF  = term_count / total_terms_in_doc
  IDF = log(N / (1 + df)) + 1   (smoothed)
  TF-IDF = TF * IDF
  Cosine = dot(a, b) / (||a|| * ||b||)
"""

import re
import math
from collections import Counter, defaultdict
from dataclasses import dataclass


# ── Text preprocessing ────────────────────────────────────────────────────────

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

def tokenise(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace, remove stop words."""
    text = text.lower()
    # Expand camelCase: orderId → order id
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Expand snake_case: order_id → order id
    text = text.replace("_", " ").replace("-", " ")
    tokens = re.findall(r'[a-z0-9]+', text)
    # Keep tokens >= 2 chars and not stop words
    return [t for t in tokens if len(t) >= 2 and t not in STOP_WORDS]

def build_doc_text(incident: dict) -> str:
    """Concatenate all text fields of an incident into one document."""
    parts = [
        incident.get("title", ""),
        " ".join(incident.get("symptoms", [])),
        incident.get("root_cause", ""),
        incident.get("fix", ""),
        " ".join(incident.get("services", [])),
    ]
    return " ".join(p for p in parts if p)


# ── TF-IDF engine ─────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    incident_id: str
    incident_title: str
    cosine_score: float             # 0-1
    matching_terms: list[str]
    term_overlap_pct: float         # % of query terms found in incident
    incident: dict


class TFIDFSimilarityEngine:
    """
    Builds a TF-IDF index over a corpus of incidents.
    Supports cosine similarity search against free-form query text.
    """

    def __init__(self):
        self.corpus: list[dict] = []
        self.doc_texts: list[str] = []
        self.doc_tokens: list[list[str]] = []
        self.vocab: list[str] = []
        self.idf: dict[str, float] = {}
        self.tfidf_matrix: list[dict[str, float]] = []   # list of {term: tfidf} per doc
        self._built = False

    def fit(self, incidents: list[dict]) -> "TFIDFSimilarityEngine":
        """Build TF-IDF index from incident corpus."""
        self.corpus = incidents
        self.doc_texts = [build_doc_text(inc) for inc in incidents]
        self.doc_tokens = [tokenise(text) for text in self.doc_texts]

        N = len(incidents)

        # Document frequency: how many docs contain each term
        df: dict[str, int] = defaultdict(int)
        for tokens in self.doc_tokens:
            for term in set(tokens):
                df[term] += 1

        # IDF (smoothed log)
        self.vocab = sorted(df.keys())
        self.idf = {term: math.log(N / (1 + df[term])) + 1.0
                    for term in self.vocab}

        # TF-IDF per document
        self.tfidf_matrix = []
        for tokens in self.doc_tokens:
            tf = Counter(tokens)
            total = len(tokens) or 1
            vec = {}
            for term in set(tokens):
                vec[term] = (tf[term] / total) * self.idf.get(term, 1.0)
            self.tfidf_matrix.append(vec)

        self._built = True
        return self

    def _vectorise(self, tokens: list[str]) -> dict[str, float]:
        """TF-IDF vector for a query."""
        tf = Counter(tokens)
        total = len(tokens) or 1
        vec = {}
        for term in set(tokens):
            if term in self.idf:
                vec[term] = (tf[term] / total) * self.idf[term]
        return vec

    def _cosine(self, a: dict[str, float], b: dict[str, float]) -> float:
        """Cosine similarity between two TF-IDF sparse vectors."""
        common = set(a) & set(b)
        if not common:
            return 0.0
        dot    = sum(a[t] * b[t] for t in common)
        norm_a = math.sqrt(sum(v * v for v in a.values()))
        norm_b = math.sqrt(sum(v * v for v in b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def search(self, query: str, top_k: int = 5, min_score: float = 0.05) -> list[SearchResult]:
        """
        Cosine similarity search. Returns top-k incidents ranked by score.
        """
        if not self._built:
            raise RuntimeError("Call fit() before search()")

        q_tokens = tokenise(query)
        q_vec    = self._vectorise(q_tokens)
        q_terms  = set(q_tokens)

        results = []
        for i, (doc_vec, incident) in enumerate(zip(self.tfidf_matrix, self.corpus)):
            score = self._cosine(q_vec, doc_vec)
            if score < min_score:
                continue

            doc_terms = set(self.doc_tokens[i])
            matching  = sorted(q_terms & doc_terms)
            overlap   = len(matching) / len(q_terms) if q_terms else 0.0

            results.append(SearchResult(
                incident_id=incident.get("id", "?"),
                incident_title=incident.get("title", "?"),
                cosine_score=round(score, 4),
                matching_terms=matching,
                term_overlap_pct=round(overlap * 100, 1),
                incident=incident
            ))

        results.sort(key=lambda r: r.cosine_score, reverse=True)
        return results[:top_k]

    def search_multi(self, queries: list[str], top_k: int = 5,
                     min_score: float = 0.05) -> list[SearchResult]:
        """
        Search with multiple query strings (e.g. one per anomaly description).
        Merges results, keeping the best score per incident.
        """
        merged: dict[str, SearchResult] = {}
        for q in queries:
            for r in self.search(q, top_k=top_k * 2, min_score=min_score):
                if r.incident_id not in merged or r.cosine_score > merged[r.incident_id].cosine_score:
                    merged[r.incident_id] = r

        ranked = sorted(merged.values(), key=lambda r: r.cosine_score, reverse=True)
        return ranked[:top_k]


if __name__ == "__main__":
    # Test with the actual incident library
    from knowledge_base_agent import INCIDENT_LIBRARY   # type: ignore
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agents.knowledge_base_agent import INCIDENT_LIBRARY

    engine = TFIDFSimilarityEngine().fit(INCIDENT_LIBRARY)

    queries = [
        "Java sends qty but Python expects quantity KeyError missing field HTTP 400",
        "order total stored as integer 99 instead of 99.99 int cast truncation SQLite",
        "order_id sent orderId expected camelCase snake_case notification service",
    ]

    print("=== TF-IDF Similarity Engine ===\n")
    for q in queries:
        print(f"Query: {q[:70]}...")
        results = engine.search(q, top_k=3)
        for r in results:
            print(f"  [{r.cosine_score:.3f}] {r.incident_id}: {r.incident_title}")
            print(f"           matching terms: {', '.join(r.matching_terms[:8])}")
            print(f"           overlap: {r.term_overlap_pct}%")
        print()
