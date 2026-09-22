#!/usr/bin/env python3
"""Offline tests for decision_stand (no network, no API key, no spend).

Run::

    python3 -m unittest discover -s tools/decision-stand -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import decision_stand as ds  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "dataset_sample.json")


def fixture_rows():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["rows"]


class GoldSupportTest(unittest.TestCase):
    def setUp(self):
        self.rows = fixture_rows()

    def test_choice_support_follows_criteria_order(self):
        row = self.rows[0]
        labels, dist = ds.gold_support(row["questions"]["action"], row["gold"]["action"])
        self.assertEqual(labels, ["continue", "human_review", "stop"])
        self.assertAlmostEqual(sum(dist), 1.0, places=6)

    def test_noul_support_from_probabilities(self):
        row = self.rows[0]
        labels, dist = ds.gold_support(row["questions"]["needs_review"], row["gold"]["needs_review"])
        self.assertEqual(labels, ["false", "true"])
        self.assertAlmostEqual(dist[1], 0.25, places=6)

    def test_noul_falls_back_to_noul_field(self):
        labels, dist = ds.gold_support({"type": "noul"}, {"type": "noul", "noul": 0.8})
        self.assertAlmostEqual(dist[1], 0.8, places=6)

    def test_score_support_from_criteria_length(self):
        row = self.rows[1]
        labels, dist = ds.gold_support(row["questions"]["urgency"], row["gold"]["urgency"])
        self.assertEqual(labels, ["0", "1", "2"])
        self.assertAlmostEqual(dist[2], 0.4, places=6)

    def test_missing_distribution_is_none(self):
        self.assertIsNone(ds.gold_support({"type": "choice", "criteria": {}}, {"type": "choice"}))


class GoldIndexTest(unittest.TestCase):
    def setUp(self):
        self.rows = fixture_rows()

    def test_choice_label(self):
        row = self.rows[0]
        labels, _ = ds.gold_support(row["questions"]["action"], row["gold"]["action"])
        self.assertEqual(ds.gold_index(row["questions"]["action"], row["gold"]["action"], labels), 0)

    def test_noul_false(self):
        row = self.rows[0]
        labels, _ = ds.gold_support(row["questions"]["needs_review"], row["gold"]["needs_review"])
        self.assertEqual(ds.gold_index(row["questions"]["needs_review"], row["gold"]["needs_review"], labels), 0)

    def test_score_string_label(self):
        row = self.rows[1]
        labels, _ = ds.gold_support(row["questions"]["urgency"], row["gold"]["urgency"])
        self.assertEqual(ds.gold_index(row["questions"]["urgency"], row["gold"]["urgency"], labels), 1)


class ModelDistTest(unittest.TestCase):
    def test_dict_order_does_not_matter(self):
        ans = {"probabilities": {"stop": 0.1, "continue": 0.6, "human_review": 0.3}}
        dist = ds.model_dist(ans, ["continue", "human_review", "stop"])
        self.assertEqual(dist, [0.6, 0.3, 0.1])

    def test_missing_keys_become_zero(self):
        self.assertEqual(ds.model_dist({"probabilities": {"continue": 1.0}},
                                       ["continue", "human_review", "stop"]), [1.0, 0.0, 0.0])

    def test_noul_field_one_hot(self):
        self.assertEqual(ds.model_dist({"noul": 0.9}, ["false", "true"]), [0.0, 1.0])

    def test_score_one_hot(self):
        self.assertEqual(ds.model_dist({"score": 1.6}, ["0", "1", "2"]), [0.0, 0.0, 1.0])


class MetricsTest(unittest.TestCase):
    def test_perfect_run_scores_1_0(self):
        rows = fixture_rows()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results_perfect.jsonl")
            answers = {
                rows[0]["id"]: {k: v for k, v in rows[0]["gold"].items()},
                rows[1]["id"]: {k: v for k, v in rows[1]["gold"].items()},
            }
            with open(path, "w", encoding="utf-8") as fh:
                for rid, ans in answers.items():
                    fh.write(json.dumps({"id": rid, "ok": True, "answers": ans}) + "\n")
            report = ds.evaluate(path, rows)
        self.assertEqual(report["overall"]["n"], 4)
        self.assertEqual(report["overall"]["accuracy"], 1.0)
        self.assertAlmostEqual(report["overall"]["brier"], 0.0, places=9)
        # Gold is soft, so a gold-matching model is still "under-confident"
        # against argmax correctness -- ECE is not 0 by construction here.
        self.assertGreater(report["overall"]["ece"], 0.0)

    def test_wrong_run_scores_zero(self):
        rows = fixture_rows()
        wrong = {
            rows[0]["id"]: {
                "action": {"probabilities": {"continue": 0.1, "human_review": 0.1, "stop": 0.8}},
                "needs_review": {"probabilities": {"false": 0.2, "true": 0.8}},
            },
            rows[1]["id"]: {
                "disposition": {"probabilities": {"pay": 0.8, "hold": 0.1, "reject": 0.1}},
                "urgency": {"probabilities": {"0": 0.8, "1": 0.1, "2": 0.1}, "score": 0.2},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results_wrong.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for rid, ans in wrong.items():
                    fh.write(json.dumps({"id": rid, "ok": True, "answers": ans}) + "\n")
            report = ds.evaluate(path, rows)
        self.assertEqual(report["overall"]["n"], 4)
        self.assertEqual(report["overall"]["accuracy"], 0.0)
        self.assertIn("score_mae", report["overall"])

    def test_bad_records_are_skipped_not_fatal(self):
        rows = fixture_rows()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results_partial.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": rows[0]["id"], "ok": False, "error": "http-500"}) + "\n")
                fh.write(json.dumps({"id": "unknown_id", "ok": True, "answers": {"x": {}}}) + "\n")
                fh.write(json.dumps({"id": rows[1]["id"], "ok": True, "answers": rows[1]["gold"]}) + "\n")
            report = ds.evaluate(path, rows)
        self.assertEqual(report["records_skipped"], 2)
        self.assertEqual(report["overall"]["n"], 2)


class EceTest(unittest.TestCase):
    def test_confident_and_correct_is_zero(self):
        self.assertAlmostEqual(ds.ece([(1.0, True)] * 20), 0.0, places=6)

    def test_confident_and_wrong_is_high(self):
        self.assertAlmostEqual(ds.ece([(0.95, False)] * 20), 0.95, places=6)


class JsonlTest(unittest.TestCase):
    def test_resume_skips_done_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results_x.jsonl")
            ds.append_jsonl(path, {"id": "a", "ok": True})
            ds.append_jsonl(path, {"id": "b", "ok": False})
            self.assertEqual(ds.done_ids(path), {"a"})
            rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
            pending = [r["id"] for r in rows if r["id"] not in ds.done_ids(path)]
            self.assertEqual(pending, ["b", "c"])

    def test_records_are_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results_y.jsonl")
            ds.append_jsonl(path, {"id": "a", "ok": True, "answers": {"q": {"choice": "x"}}})
            got = ds.read_jsonl(path)
            self.assertEqual(got[0]["answers"]["q"]["choice"], "x")


class EstimateTest(unittest.TestCase):
    def test_tokens_and_budget(self):
        rows = fixture_rows()
        tokens = ds.estimate_tokens(rows)
        self.assertGreater(tokens, 0)
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "dataset.json")
            ds.save_dataset(data, rows)
            args = ds.build_parser().parse_args(["--mode", "estimate", "--data", data])
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ds.mode_estimate(args), 0)


class TransportTest(unittest.TestCase):
    class _Resp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_call_jev_returns_payload_without_key(self):
        calls = {}

        def opener(req, timeout=None):
            calls["auth"] = req.headers.get("Authorization")
            return self._Resp({"answers": {"q": {"noul": 0.5}}, "usage": {"cost": 0.00001}})

        rec = ds.call_jev({"m": "x"}, {"q": {"type": "noul"}}, "sk-or-secret",
                          opener=opener)
        self.assertTrue(rec["ok"])
        self.assertEqual(calls["auth"], "Bearer sk-or-secret")
        self.assertNotIn("sk-or-secret", json.dumps(rec))
        self.assertAlmostEqual(ds.usage_cost(rec), 0.00001, places=9)
        # the JSONL record shape carries usage at the top level
        self.assertAlmostEqual(
            ds.usage_cost({"id": "a", "ok": True, "usage": {"cost": 0.00002}}), 0.00002, places=9)

    def test_call_jev_retries_on_429_then_succeeds(self):
        state = {"attempts": 0}

        def opener(req, timeout=None):
            state["attempts"] += 1
            if state["attempts"] < 2:
                raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)
            return self._Resp({"answers": {}})

        rec = ds.call_jev({"m": "x"}, {}, "k", retries=3, opener=opener)
        self.assertTrue(rec["ok"])
        self.assertEqual(state["attempts"], 2)

    def test_call_jev_gives_up_after_retries(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "down", {}, None)

        rec = ds.call_jev({"m": "x"}, {}, "k", retries=2, opener=opener)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["http"], 503)

    def test_api_key_prefers_env_then_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, ".or_env")
            with open(env_file, "w", encoding="utf-8") as fh:
                fh.write("OPENROUTER_API_KEY=from-file\n")
            old = os.environ.pop("OPENROUTER_API_KEY", None)
            try:
                self.assertEqual(ds.api_key(env_file), "from-file")
                os.environ["OPENROUTER_API_KEY"] = "from-env"
                self.assertEqual(ds.api_key(env_file), "from-env")
            finally:
                os.environ.pop("OPENROUTER_API_KEY", None)
                if old is not None:
                    os.environ["OPENROUTER_API_KEY"] = old


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_order_insensitive_to_content(self):
        rows = fixture_rows()
        self.assertEqual(ds.dataset_fingerprint(rows), ds.dataset_fingerprint(list(rows)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
