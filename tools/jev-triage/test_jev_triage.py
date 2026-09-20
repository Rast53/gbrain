#!/usr/bin/env python3
"""Offline tests for jev_triage (no network, no database required).

Run::

    python3 -m unittest discover -s tools/jev-triage -p 'test_*.py' -v

The ``ReplayPilotTest`` cases run only when the 2026-09-19 pilot artifacts are
present (they are on the tw/chuwi host, not in the public repo); the synthetic
fixtures under ``fixtures/`` cover the same decision paths in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import jev_triage as jt  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
PAIRS_FIX = os.path.join(FIXTURES, "replay_pairs")
PRUNE_FIX = os.path.join(FIXTURES, "replay_prune")
PILOT = "/opt/hermes/work/typesafe-pilot"
PILOT_PAIRS = os.path.join(PILOT, "results_pairs.jsonl")


def _answers(same_ru, same_en, action_ru, action_en, conf=0.9):
    return {
        "same_ru": {"type": "noul", "noul": same_ru},
        "same_en": {"type": "noul", "noul": same_en},
        "action_ru": {"type": "choice", "choice": action_ru,
                      "probabilities": {action_ru: conf}, "confidence": conf},
        "action_en": {"type": "choice", "choice": action_en,
                      "probabilities": {action_en: conf}, "confidence": conf},
    }


def _page(slug, source, text, kind="markdown", ptype="note"):
    return {"slug": slug, "source_id": source, "page_kind": kind,
            "type": ptype, "title": slug, "text": text}


# ---------------------------------------------------------------------------
# Decision synthesis
# ---------------------------------------------------------------------------

class DecisionTest(unittest.TestCase):
    def test_identical_text_is_deterministic_merge(self):
        result = jt.synthesize_pair_decision({}, identical=True, similarity=1.0)
        self.assertEqual(result["decision"], "merge")
        self.assertIn("identical-text", result["flags"])

    def test_facet_pairs_keep(self):
        result = jt.synthesize_pair_decision(
            _answers(0.05, 0.06, "keep", "keep"), facets="repos vs projects")
        self.assertEqual(result["decision"], "keep")

    def test_cross_layer_readme_keep(self):
        result = jt.synthesize_pair_decision(
            _answers(0.08, 0.07, "keep", "keep"), facets="layer-a vs layer-b")
        self.assertEqual(result["decision"], "keep")

    def test_confident_merge(self):
        result = jt.synthesize_pair_decision(_answers(0.95, 0.94, "merge", "merge"))
        self.assertEqual(result["decision"], "merge")

    def test_weak_signal_goes_to_review(self):
        result = jt.synthesize_pair_decision(_answers(0.5, 0.5, "keep", "keep"))
        self.assertEqual(result["decision"], "review")
        self.assertIn("weak-signal", result["flags"])

    def test_ru_en_divergence_goes_to_review(self):
        result = jt.synthesize_pair_decision(_answers(0.9, 0.1, "merge", "keep"))
        self.assertEqual(result["decision"], "review")
        self.assertIn("ru-en-same-divergence", result["flags"])
        self.assertIn("ru-en-action-divergence", result["flags"])

    def test_prune_signal_on_probe(self):
        answers = _answers(0.9, 0.9, "keep", "keep")
        answers["value_ru"] = {"score": 0}
        answers["value_en"] = {"score": 0}
        answers["prune_ru"] = {"type": "noul", "noul": 0.92}
        answers["prune_en"] = {"type": "noul", "noul": 0.90}
        page = _page("test/example-probe", "example-canonical", "probe page")
        result = jt.synthesize_prune_decision(answers, page)
        self.assertEqual(result["decision"], "prune")

    def test_value_keep_on_entity(self):
        answers = _answers(0.02, 0.03, "keep", "keep")
        answers["value_ru"] = {"score": 2}
        answers["value_en"] = {"score": 2}
        answers["prune_ru"] = {"type": "noul", "noul": 0.02}
        answers["prune_en"] = {"type": "noul", "noul": 0.03}
        page = _page("services/example-live", "example-canonical", "rich page " * 20)
        result = jt.synthesize_prune_decision(answers, page)
        self.assertEqual(result["decision"], "keep")


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

class CandidateTest(unittest.TestCase):
    def test_identical_text_two_slugs_is_candidate(self):
        text = "identical synthetic body text for two example slugs"
        pages = [
            _page("services/example-a", "example-canonical", text),
            _page("services/example-b", "example-canonical", text),
        ]
        candidates = jt.generate_pair_candidates(pages)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["identical"])

    def test_facet_same_basename_is_candidate(self):
        pages = [
            _page("repos/example-app", "example-canonical", "repository entity", ptype="repo"),
            _page("projects/example-app", "example-canonical", "project entity", ptype="project"),
        ]
        candidates = jt.generate_pair_candidates(pages)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["facets"], "repos vs projects")

    def test_unrelated_pages_are_not_candidates(self):
        pages = [
            _page("services/alpha", "example-canonical", "alpha service runtime evidence"),
            _page("people/bravo", "example-canonical", "bravo person profile notes"),
        ]
        self.assertEqual(jt.generate_pair_candidates(pages), [])


# ---------------------------------------------------------------------------
# Prune hard exclusions
# ---------------------------------------------------------------------------

class PruneExclusionTest(unittest.TestCase):
    def test_code_index_source_is_excluded(self):
        page = _page("index/module", "raclaw-task-mcp", "module index", kind="code")
        reason = jt.prune_exclusion(page)
        self.assertIsNotNone(reason)
        self.assertIn("code-index", reason[0])

    def test_code_kind_is_excluded_regardless_of_source(self):
        page = _page("anything", "example-canonical", "x", kind="code")
        self.assertIsNotNone(jt.prune_exclusion(page))

    def test_agent_floor_source_is_excluded(self):
        page = _page("memory/note", "rahermes", "agent floor note")
        reason = jt.prune_exclusion(page)
        self.assertIsNotNone(reason)
        self.assertIn("agent-floor", reason[0])

    def test_real_probe_and_empty_template_are_not_excluded(self):
        probe = _page("test/example-probe", "example-canonical", "probe page")
        empty = _page("docs/tasks/example-empty/plan", "example-task-mcp",
                      "# TASK — Plan\n\n## Phase 1: …")
        self.assertIsNone(jt.prune_exclusion(probe))
        self.assertIsNone(jt.prune_exclusion(empty))


# ---------------------------------------------------------------------------
# Synthetic replay (offline)
# ---------------------------------------------------------------------------

class ReplaySyntheticTest(unittest.TestCase):
    def test_pairs_replay_blocks(self):
        report = jt.run_pairs_replay(
            os.path.join(PAIRS_FIX, "results.jsonl"),
            os.path.join(PAIRS_FIX, "jobs.jsonl"),
            os.path.join(PAIRS_FIX, "labels.json"),
        )
        by_id = {rec["id"]: rec["decision"] for rec in report["decisions"]}
        self.assertEqual(
            by_id["pair::services/example-dup-a|services/example-dup-b"], "merge")
        self.assertEqual(by_id["pair::repos/example-app|projects/example-app"], "keep")
        self.assertEqual(
            by_id["pair::readme|example-layer-a|example-layer-b"], "keep")
        self.assertEqual(
            by_id["pair::services/example-old|services/example-new"], "review")
        self.assertEqual(
            by_id["pair::projects/example-split|repos/example-split"], "review")
        check = report["label_check"]
        self.assertEqual(check["compared"], 5)
        self.assertGreaterEqual(check["same_accuracy"], 0.6)

    def test_prune_replay_excludes_code_and_signals_stubs(self):
        report = jt.run_prune_replay(
            os.path.join(PRUNE_FIX, "results.jsonl"),
            os.path.join(PRUNE_FIX, "jobs.jsonl"),
            os.path.join(PRUNE_FIX, "labels.json"),
        )
        prune_ids = [rec["id"] for rec in report["prune"]]
        excluded_ids = [rec["id"] for rec in report["excluded"]]
        self.assertIn("page::test/example-probe|example-canonical", prune_ids)
        self.assertIn("page::docs/tasks/example-empty/plan|example-task-mcp", prune_ids)
        self.assertIn("page::index/example-module|example-task-mcp", excluded_ids)
        self.assertNotIn("page::index/example-module|example-task-mcp", prune_ids)
        self.assertEqual(report["label_check"]["prune_accuracy"], 1.0)


@unittest.skipUnless(os.path.exists(PILOT_PAIRS), "pilot artifacts not present")
class ReplayPilotTest(unittest.TestCase):
    def test_pairs_replay_matches_pilot_labels(self):
        report = jt.run_pairs_replay(
            PILOT_PAIRS,
            os.path.join(PILOT, "jobs_pairs.jsonl"),
            os.path.join(PILOT, "labels_pairs.json"),
        )
        by_id = {rec["id"]: rec["decision"] for rec in report["decisions"]}
        self.assertEqual(
            by_id["pair::services/nginx-london|services/openrouter-proxy-london"],
            "merge")
        self.assertEqual(
            by_id["pair::readme|raclaw-canonical|raclaw-proposed"], "keep")
        self.assertEqual(
            by_id["pair::repos/vpn-dashboard|projects/vpn-dashboard"], "keep")
        check = report["label_check"]
        self.assertGreaterEqual(check["same_accuracy"], 0.90)
        self.assertGreaterEqual(check["action_accuracy"], 0.85)
        # The two known borderline cases stay visible as mismatches.
        self.assertEqual(check["compared"], 17)
        self.assertEqual(len(check["mismatches"]), 2)

    def test_prune_replay_no_code_index_signals(self):
        report = jt.run_prune_replay(
            os.path.join(PILOT, "results_pages.jsonl"),
            os.path.join(PILOT, "jobs_pages.jsonl"),
            os.path.join(PILOT, "labels_pages.json"),
        )
        prune_ids = [rec["id"] for rec in report["prune"]]
        self.assertFalse(any("__init__-py" in rid for rid in prune_ids))
        self.assertTrue(any("write-path-extract-probe" in rid for rid in prune_ids))
        self.assertGreaterEqual(report["label_check"]["prune_accuracy"], 0.90)


# ---------------------------------------------------------------------------
# SQL guard / read-only
# ---------------------------------------------------------------------------

class SqlGuardTest(unittest.TestCase):
    def test_every_sql_literal_is_select(self):
        sql_attrs = [(name, value) for name, value in vars(jt).items()
                     if name.startswith("SQL_") and isinstance(value, str)]
        self.assertTrue(sql_attrs)
        for name, sql in sql_attrs:
            self.assertTrue(sql.strip().upper().startswith("SELECT"),
                            "%s must start with SELECT" % name)

    def test_no_forbidden_sql_keywords(self):
        import re
        blob = " ".join(value for name, value in vars(jt).items()
                        if name.startswith("SQL_") and isinstance(value, str)).upper()
        for keyword in jt.FORBIDDEN_SQL_KEYWORDS:
            self.assertIsNone(re.search(r"\b%s\b" % re.escape(keyword), blob),
                              "read-only guard tripped on %r" % keyword)

    def test_module_never_imports_a_write_capable_engine(self):
        with open(jt.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for token in ("psycopg", "sqlalchemy", "cursor.execute",
                      "engine.put_page", "putPage", "delete_page"):
            self.assertNotIn(token, source)


# ---------------------------------------------------------------------------
# Secrets / skip notes / HTTP failure
# ---------------------------------------------------------------------------

class SecretsTest(unittest.TestCase):
    FIXTURE_DENYLIST = (
        "raclaw-canonical", "raclaw-task-mcp", "rahermes",
        "vpn-dashboard", "mattermost", "nginx-london",
        "openrouter-proxy-london", "readme|raclaw",
    )

    def test_fixtures_contain_only_synthetic_text(self):
        for root, _dirs, files in os.walk(FIXTURES):
            for name in files:
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    blob = handle.read().lower()
                for token in self.FIXTURE_DENYLIST:
                    self.assertNotIn(token.lower(), blob,
                                     "%s leaked real pilot text (%s)" % (path, token))

    def test_no_api_key_literal_in_source(self):
        with open(jt.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("sk-or-", source)
        self.assertNotIn("Bearer sk-", source)

    def test_missing_key_is_skip_exit_zero(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": HERE}
        env.pop("OPENROUTER_API_KEY", None)
        env.pop("GBRAIN_DATABASE_URL", None)
        proc = subprocess.run(
            [sys.executable, jt.__file__, "--mode", "pairs", "--json"],
            capture_output=True, text=True, env=env, cwd=HERE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "no-openrouter-key")

    def test_missing_database_is_skip_exit_zero(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": HERE,
               "OPENROUTER_API_KEY": "test-key-not-real"}
        env.pop("GBRAIN_DATABASE_URL", None)
        proc = subprocess.run(
            [sys.executable, jt.__file__, "--mode", "prune", "--json"],
            capture_output=True, text=True, env=env, cwd=HERE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["reason"], "no-database")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class JevMockTest(unittest.TestCase):
    def test_http_500_is_reported_not_raised(self):
        def opener(_req, timeout=None):
            raise urllib.error.HTTPError("http://x", 500, "boom", {}, None)

        result = jt.call_jev("state", jt.pair_questions(), "k",
                             retries=1, opener=opener)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http"], 500)

    def test_live_pairs_with_mocked_answers(self):
        pages = [
            _page("repos/example-app", "example-canonical", "repository entity", ptype="repo"),
            _page("projects/example-app", "example-canonical", "project entity", ptype="project"),
        ]

        def opener(_req, timeout=None):
            return _FakeResponse({"answers": _answers(0.05, 0.06, "keep", "keep")})

        report = jt.run_pairs_live(pages, "k", jt.MODEL, 0.60, 10, 10,
                                   retries=1, opener=opener)
        self.assertEqual(report["counts"]["keep"], 1)
        self.assertEqual(report["skips"], [])

    def test_live_prune_never_calls_jev_for_code_pages(self):
        calls = {"n": 0}

        def opener(_req, timeout=None):
            calls["n"] += 1
            answers = _answers(0.9, 0.9, "keep", "keep")
            answers["value_ru"] = {"score": 0}
            answers["value_en"] = {"score": 0}
            answers["prune_ru"] = {"type": "noul", "noul": 0.92}
            answers["prune_en"] = {"type": "noul", "noul": 0.90}
            return _FakeResponse({"answers": answers})

        pages = [
            _page("index/module", "raclaw-task-mcp", "module index", kind="code"),
            _page("test/probe", "example-canonical", "probe page"),
        ]
        report = jt.run_prune_live(pages, "k", jt.MODEL, 10, retries=1, opener=opener)
        self.assertEqual(calls["n"], 1)  # only the probe page
        self.assertEqual(len(report["excluded"]), 1)
        self.assertEqual(report["excluded"][0]["page_kind"], "code")


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------

class BudgetTest(unittest.TestCase):
    def test_full_run_estimate_is_under_budget(self):
        estimate = jt.estimate_report(500 + 60)
        self.assertLess(estimate["usd"], 0.05)
        self.assertTrue(estimate["under_budget"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
