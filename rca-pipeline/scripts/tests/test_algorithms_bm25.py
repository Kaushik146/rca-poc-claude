"""Direct unit coverage for agents/algorithms/bm25.py."""
from __future__ import annotations
import sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT / "agents"))
from algorithms.bm25 import BM25Engine, tokenize  # type: ignore


class Tokenizer(unittest.TestCase):
    def test_lowercases(self):
        self.assertIn("checkout", tokenize("Checkout"))
    def test_expands_snake_case(self):
        toks = tokenize("order_id")
        self.assertIn("order", toks)
        self.assertIn("id", toks)
    def test_drops_stop_words(self):
        toks = tokenize("the inventory service")
        self.assertNotIn("the", toks)
        self.assertIn("inventory", toks)
    def test_drops_single_char_tokens(self):
        toks = tokenize("a b cd ef")
        self.assertNotIn("a", toks)
        self.assertIn("cd", toks)


class BM25Ranking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = [
            {"id": "PM-42", "title": "Inventory reserve off-by-one last unit",
             "description": "Reserve endpoint rejects last unit due to off-by-one boundary in reserve()"},
            {"id": "PM-17", "title": "Notification timeout under load",
             "description": "Node notification service times out when order volume spikes above 100rps"},
            {"id": "PM-91", "title": "Currency converter wrong rate",
             "description": "EUR rate set to 0.108 instead of 1.08 — orders 10x underpriced"},
            {"id": "PM-3",  "title": "Auth token expiry",
             "description": "Session tokens not refreshed before expiry causing checkout 401s"},
        ]
        cls.engine = BM25Engine().fit(cls.corpus)

    def test_inventory_query_ranks_inventory_postmortem_first(self):
        results = self.engine.search("inventory reserve fails last unit", top_k=3)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].doc_id, "PM-42")

    def test_notification_query_ranks_notification_postmortem_first(self):
        results = self.engine.search("notification service timeout high load", top_k=3)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].doc_id, "PM-17")

    def test_currency_query_ranks_currency_postmortem_first(self):
        results = self.engine.search("EUR currency conversion wrong rate", top_k=3)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].doc_id, "PM-91")

    def test_results_sorted_descending_by_score(self):
        results = self.engine.search("inventory reserve fails", top_k=4)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_matching_terms_populated_correctly(self):
        results = self.engine.search("inventory unit", top_k=1)
        self.assertGreater(len(results), 0)
        self.assertTrue(set(results[0].matching_terms) & {"inventory", "unit"})

    def test_min_score_filters_low_relevance_hits(self):
        self.assertEqual(self.engine.search("quantum mechanics", top_k=5, min_score=5.0), [])


class BM25EmptyCorpus(unittest.TestCase):
    def test_empty_corpus_is_fitted_safely(self):
        self.assertTrue(BM25Engine().fit([])._fitted)
    def test_empty_query_returns_empty_results(self):
        engine = BM25Engine().fit([{"id": "X", "title": "t", "description": "t"}])
        self.assertEqual(engine.search(""), [])
    def test_search_before_fit_raises(self):
        with self.assertRaises(RuntimeError):
            BM25Engine().search("anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
