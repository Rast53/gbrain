#!/usr/bin/env python3
"""jev-triage — read-only gbrain duplicate/stale/prune report queue.

Pipeline:
    difflib candidates (>= 0.60) -> Jev "same/action" verdict (RU+EN question
    pairs, layer conventions baked into the criteria) -> ranked JSON + Markdown
    report queue (merge / keep / review).  A second mode scans for stubs and
    empty templates ("prune signals") with hard doctrinal exclusions.

Guarantees:
  * READ-ONLY: every SQL statement is a ``SELECT``.  No page is ever created,
    updated, deleted or merged by this tool.
  * The OpenRouter key is read from ``OPENROUTER_API_KEY`` only; it is never
    logged, printed or written to the report.
  * Missing key / missing DB / missing psql / HTTP 5xx -> a skip note and
    exit code 0 (the tool degrades, it does not crash).

Jev is pinned to ``typesafe/jev-1.13-20260917`` on the OpenRouter alpha
endpoint ``https://openrouter.ai/api/alpha/decisions``.

Usage::

    # live (tw operator): candidates from the DB + Jev verdicts
    python3 tools/jev-triage/jev_triage.py --mode pairs --json

    # offline replay of recorded pilot answers (no DB, no key)
    python3 tools/jev-triage/jev_triage.py --mode pairs --replay \
        --results /opt/hermes/work/typesafe-pilot/results_pairs.jsonl \
        --jobs    /opt/hermes/work/typesafe-pilot/jobs_pairs.jsonl \
        --labels  /opt/hermes/work/typesafe-pilot/labels_pairs.json --json

    # offline replay of the prune mode
    python3 tools/jev-triage/jev_triage.py --mode prune --replay \
        --results /opt/hermes/work/typesafe-pilot/results_pages.jsonl \
        --jobs    /opt/hermes/work/typesafe-pilot/jobs_pages.jsonl \
        --labels  /opt/hermes/work/typesafe-pilot/labels_pages.json --json
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL = "jev-triage"
TOOL_VERSION = "0.1.0"

MODEL = "typesafe/jev-1.13-20260917"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

DIFFLIB_THRESHOLD = 0.60
TEXT_JACCARD_PREFILTER = 0.50
NOUL_THRESHOLD = 0.50
WEAK_LO, WEAK_HI = 0.35, 0.65
MAX_TEXT_CHARS = 2600
DEFAULT_MAX_PAIRS = 120
DEFAULT_MAX_CALLS = 600
DEFAULT_MAX_BUDGET_USD = 0.05
# Conservative per-decision upper bound derived from the 2026-09-19 pilot
# (median $0.000057 / call, worst observed $0.000071 / call).
USD_PER_CALL_ESTIMATE = 0.00008

DECISION_MERGE = "merge"
DECISION_KEEP = "keep"
DECISION_REVIEW = "review"

# Hard doctrinal prune exclusions (see README).
PRUNE_EXCLUDED_PAGE_KINDS = frozenset({"code"})
PRUNE_EXCLUDED_SOURCES = frozenset({"rahermes"})  # agent-floor: not canon, not pruned

# ---------------------------------------------------------------------------
# SQL — every statement is a SELECT.  Keep SQL_* literals starting with SELECT;
# test_jev_triage.py::SqlGuardTest enforces this.  Never add DML/DDL here.
# ---------------------------------------------------------------------------

SQL_PAGES = (
    "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)::text "
    "FROM ("
    "SELECT slug, source_id, page_kind, type, title, "
    "left(compiled_truth, 2600) AS text, content_hash, "
    "updated_at::text AS updated_at "
    "FROM pages WHERE deleted_at IS NULL "
    "ORDER BY source_id, slug"
    ") t"
)

SQL_COUNT_PAGES = "SELECT count(*) FROM pages WHERE deleted_at IS NULL"

SQL_VERIFY_UNCHANGED = (
    "SELECT count(*) FILTER (WHERE updated_at > :'since'::timestamptz) FROM pages"
)

FORBIDDEN_SQL_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE",
    "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX",
)

# ---------------------------------------------------------------------------
# Layer conventions baked into the Jev questions (from the pilot + gbrain
# content-hygiene doctrine).  Kept as module constants so tests can assert
# they are present in the generated questions.
# ---------------------------------------------------------------------------

PAIR_SAME_RU = (
    "Описывают ли эти две страницы одну и ту же сущность так, что одна из них "
    "может считаться дубликатом или устаревшей версией другой? "
    "Конвенции слоёв: разные грани одной системы (репозиторий vs сервис vs "
    "проект) — это «нет»; одноимённые страницы разных слоёв (canonical vs "
    "proposed vs agent-floor) — это «нет», если тексты описывают разные роли; "
    "устаревший фрагмент удалённого контура, оставшийся без пометки об "
    "удалении, — это «да» (устаревшая версия)."
)
PAIR_SAME_EN = (
    "Do these two pages describe the same entity such that one could be a "
    "duplicate or a stale version of the other? Layer conventions: different "
    "facets of one system (repository vs service vs project) count as no; "
    "same-name pages from different layers (canonical vs proposed vs "
    "agent-floor) count as no when they describe different roles; a stale "
    "fragment of a removed contour left without a deletion marker counts as "
    "yes (a stale version)."
)
PAIR_ACTION_RU = "Что следует сделать с этой парой страниц?"
PAIR_ACTION_EN = "What should be done with this pair of pages?"

PRUNE_RU = (
    "Безопасно ли удалить эту страницу без потери полезного знания? Отвечайте "
    "«да» только если страница — служебный артефакт, проба, пустой шаблон или "
    "дубликат, и её удаление ничего не разрушит. Конвенции: страницы-индексы "
    "кода (page_kind=code) — навигационный слой, не мусор, «нет»; заметки "
    "agent-floor (source rahermes) — не канон, но их не чистим, «нет»; "
    "канонические сущности — «нет»."
)
PRUNE_EN = (
    "Is it safe to delete this page without losing useful knowledge? Answer yes "
    "only if the page is a service artifact, probe, empty template or duplicate "
    "and deleting it destroys nothing. Conventions: code-index pages "
    "(page_kind=code) are a navigation layer, not junk — answer no; agent-floor "
    "notes (source rahermes) are not canon but are not pruned — answer no; "
    "canonical entities — answer no."
)

PAIR_ACTION_CRITERIA_RU = {
    "merge": (
        "одна страница дублирует или устарела — свести в одну, старую удалить "
        "(включая устаревший фрагмент removed-контура)"
    ),
    "keep": "оставить обе страницы как есть",
    "review": "нужен разбор человеком — неясно",
}
PAIR_ACTION_CRITERIA_EN = {
    "merge": (
        "one page duplicates or is stale — consolidate into one and delete the "
        "old (including a stale removed-contour fragment)"
    ),
    "keep": "keep both pages as they are",
    "review": "a human should look — unclear",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def binarize(value) -> bool:
    """noul (0..1) -> bool with the pilot threshold; None -> False."""
    try:
        return float(value) >= NOUL_THRESHOLD
    except (TypeError, ValueError):
        return False


def truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def normalize_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    s = re.sub(r"\.(md|markdown|txt|py|ts|tsx|js|jsx|json|ya?ml)$", "", s)
    s = re.sub(r"[^0-9a-zа-яё/_-]+", "-", s)
    return s


def slug_base(slug: str) -> str:
    return normalize_slug(slug).strip("/").split("/")[-1]


def slug_facet(slug: str) -> str:
    parts = [p for p in (slug or "").split("/") if p]
    return parts[0] if len(parts) > 1 else ""


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def token_set(text: str) -> frozenset:
    return frozenset(re.findall(r"[0-9a-zа-яё]{3,}", (text or "").lower()))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


def text_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def normalize_text_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


# ---------------------------------------------------------------------------
# DB layer (psql + GBRAIN_DATABASE_URL) — read-only
# ---------------------------------------------------------------------------

def db_url_from_env(env: dict) -> str:
    return (env or {}).get("GBRAIN_DATABASE_URL", "") or ""


def run_psql(url: str, sql: str, extra_args=None, psql_bin: str | None = None):
    """Run a single read-only statement.  Returns (stdout, None) or (None, code).

    Error codes are deliberately opaque: stderr can echo the connection URL,
    which may carry credentials, so it is never surfaced.
    """
    psql_bin = psql_bin or shutil.which("psql")
    if not psql_bin:
        return None, "psql-not-found"
    if not url:
        return None, "no-db-url"
    cmd = [psql_bin, url, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
    if extra_args:
        cmd += list(extra_args)
    cmd += ["-c", sql]
    try:
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit -- psql_bin is a fixed filesystem path and cmd is an argv LIST built from a constant SQL string plus an ISO timestamp; shell=False by default, no shell interpolation
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # pragma: no cover - defensive
        return None, "psql-exec-%s" % type(exc).__name__
    if proc.returncode != 0:
        return None, "psql-exit-%d" % proc.returncode
    return proc.stdout, None


def fetch_pages(url: str, psql_bin: str | None = None, limit: int | None = None):
    out, err = run_psql(url, SQL_PAGES, psql_bin=psql_bin)
    if err:
        return None, err
    try:
        rows = json.loads((out or "").strip() or "[]")
    except ValueError:
        return None, "psql-json-parse"
    if not isinstance(rows, list):
        return None, "psql-json-shape"
    if limit is not None:
        rows = rows[:limit]
    return rows, None


def verify_read_only(url: str, since_iso: str, psql_bin: str | None = None):
    """Return the number of pages updated after ``since_iso`` (must stay 0)."""
    out, err = run_psql(
        url, SQL_VERIFY_UNCHANGED,
        extra_args=["-v", "since=%s" % since_iso], psql_bin=psql_bin,
    )
    if err:
        return None, err
    try:
        return int((out or "0").strip().splitlines()[0]), None
    except (ValueError, IndexError):
        return None, "verify-parse"


# ---------------------------------------------------------------------------
# Jev layer (OpenRouter alpha/decisions)
# ---------------------------------------------------------------------------

def api_key_from_env(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("OPENROUTER_API_KEY", "") or "").strip()


def call_jev(state, questions, key, model=MODEL, endpoint=ENDPOINT,
             timeout=150, retries=4, opener=None):
    """One decisions call.  Never logs or returns the key.

    ``opener`` is injectable for offline tests (defaults to urlopen).
    """
    opener = opener or urllib.request.urlopen
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    last = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(endpoint, data=body, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
        started = time.time()
        try:
            with opener(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return {"ok": True, "resp": data,
                    "latency_ms": int((time.time() - started) * 1000)}
        except urllib.error.HTTPError as exc:
            last = {"ok": False, "http": exc.code, "err": "http-%d" % exc.code,
                    "latency_ms": int((time.time() - started) * 1000)}
            if exc.code in (429, 500, 502, 503, 504, 529):
                time.sleep(min(2 * (attempt + 1), 8))
                continue
            return last
        except Exception as exc:  # noqa: BLE001 - defensive, keep the loop alive
            last = {"ok": False, "err": "network-%s" % type(exc).__name__,
                    "latency_ms": int((time.time() - started) * 1000)}
            time.sleep(min(1.5 * (attempt + 1), 6))
    return last


def answers_from_record(record: dict) -> dict | None:
    if not record or not record.get("ok"):
        return None
    resp = record.get("resp") or {}
    answers = resp.get("answers")
    return answers if isinstance(answers, dict) else None


# ---------------------------------------------------------------------------
# Question builders
# ---------------------------------------------------------------------------

def pair_questions() -> dict:
    return {
        "same_ru": {
            "type": "noul",
            "instructions": PAIR_SAME_RU,
            "criteria": {
                "true": "одна страница дублирует или устарела относительно другой",
                "false": "разные грани или разные объекты — обе нужны",
            },
        },
        "same_en": {
            "type": "noul",
            "instructions": PAIR_SAME_EN,
            "criteria": {
                "true": "one page duplicates or is a stale version of the other",
                "false": "different facets or different objects — both are needed",
            },
        },
        "action_ru": {
            "type": "choice",
            "instructions": PAIR_ACTION_RU,
            "criteria": dict(PAIR_ACTION_CRITERIA_RU),
        },
        "action_en": {
            "type": "choice",
            "instructions": PAIR_ACTION_EN,
            "criteria": dict(PAIR_ACTION_CRITERIA_EN),
        },
    }


def prune_questions() -> dict:
    return {
        "value_ru": {
            "type": "score",
            "instructions": "Какова ценность этой страницы для долговременной памяти проекта?",
            "criteria": [
                "0 — служебный артефакт или заглушка без самостоятельной ценности (проба, пустой шаблон, дубликат)",
                "1 — рабочая заметка или исторический контекст (инцидент, план, слой агента)",
                "2 — долговременное знание (каноническая сущность или устойчивый факт)",
            ],
        },
        "value_en": {
            "type": "score",
            "instructions": "What is the value of this page for the project's long-term memory?",
            "criteria": [
                "0 — service artifact or stub with no standalone value (probe, empty template, duplicate)",
                "1 — working note or historical context (incident, plan, agent layer)",
                "2 — durable knowledge (canonical entity or stable fact)",
            ],
        },
        "prune_ru": {
            "type": "noul",
            "instructions": PRUNE_RU,
            "criteria": {"true": "да, безопасно удалить", "false": "нет, в ней есть ценность"},
        },
        "prune_en": {
            "type": "noul",
            "instructions": PRUNE_EN,
            "criteria": {"true": "yes, safe to delete", "false": "no, it holds value"},
        },
    }


def build_pair_state(page_a: dict, page_b: dict, similarity: float | None = None) -> str:
    parts = []
    for label, page in (("PAGE A", page_a), ("PAGE B", page_b)):
        parts.append(
            "%s\nslug: %s\nsource: %s\ntype: %s\npage_kind: %s\n\n%s" % (
                label,
                page.get("slug", ""),
                page.get("source_id", page.get("source", "")),
                page.get("type", ""),
                page.get("page_kind", "markdown"),
                truncate(page.get("text", "")),
            )
        )
    state = "\n\n=====\n\n".join(parts)
    if similarity is not None:
        state += "\n\n(difflib candidate similarity: %.3f)" % similarity
    return state


def build_page_state(page: dict) -> str:
    return "**slug:** %s\n**source:** %s\n**type:** %s\n\ntext[:1200]: %s\n\n%s" % (
        page.get("slug", ""),
        page.get("source_id", page.get("source", "")),
        page.get("type", ""),
        truncate(page.get("text", ""), MAX_TEXT_CHARS),
        "",
    )


# ---------------------------------------------------------------------------
# Decision synthesis
# ---------------------------------------------------------------------------

def _noul(answers: dict, qid: str):
    entry = (answers or {}).get(qid) or {}
    return entry.get("noul")


def _choice(answers: dict, qid: str):
    entry = (answers or {}).get(qid) or {}
    return entry.get("choice")


def _choice_confidence(answers: dict, qid: str) -> float:
    entry = (answers or {}).get(qid) or {}
    conf = entry.get("confidence")
    if conf is None:
        probs = entry.get("probabilities") or {}
        conf = max(probs.values()) if probs else None
    try:
        return float(conf)
    except (TypeError, ValueError):
        return 0.5


def _noul_values(answers: dict) -> list:
    return [
        _noul(answers, "same_ru"), _noul(answers, "same_en"),
        _noul(answers, "prune_ru"), _noul(answers, "prune_en"),
    ]


def weak_signals(answers: dict) -> list:
    out = []
    for value in _noul_values(answers):
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if WEAK_LO <= f <= WEAK_HI:
            out.append(f)
    return out


def synthesize_pair_decision(answers: dict, identical: bool = False,
                             similarity: float | None = None,
                             facets: str = "") -> dict:
    """Turn one Jev answer set into {decision, score, reasons, flags, signals}."""
    reasons: list = []
    flags: list = []

    same_ru, same_en = _noul(answers, "same_ru"), _noul(answers, "same_en")
    act_ru, act_en = _choice(answers, "action_ru"), _choice(answers, "action_en")
    conf = min(_choice_confidence(answers, "action_ru"),
               _choice_confidence(answers, "action_en"))

    signals = {
        "same_ru": same_ru, "same_en": same_en,
        "action_ru": act_ru, "action_en": act_en,
        "action_confidence": round(conf, 3),
        "similarity": None if similarity is None else round(similarity, 3),
        "facets": facets or None,
        "identical_text": bool(identical),
    }

    if identical:
        reasons.append("идентичный текст под двумя слагами → детерминированный merge")
        return {"decision": DECISION_MERGE, "score": 1.0,
                "reasons": reasons, "flags": ["identical-text"], "signals": signals}

    weak = weak_signals(answers)
    same_diverges = binarize(same_ru) != binarize(same_en)
    action_diverges = (act_ru != act_en) or act_ru is None or act_en is None

    if same_diverges:
        flags.append("ru-en-same-divergence")
        reasons.append(
            "расхождение RU/EN по same_entity (ru=%.2f, en=%.2f) → review" % (
                float(same_ru or 0), float(same_en or 0))
        )
    if action_diverges:
        flags.append("ru-en-action-divergence")
        reasons.append("расхождение RU/EN по action (ru=%s, en=%s) → review" % (act_ru, act_en))
    if weak:
        flags.append("weak-signal")
        reasons.append("слабые сигналы noul (0.35–0.65): %s → review" % ", ".join(
            "%.2f" % v for v in weak))

    if same_diverges or action_diverges or weak:
        return {"decision": DECISION_REVIEW, "score": round(1.0 - conf, 3),
                "reasons": reasons, "flags": flags, "signals": signals}

    if act_ru == act_en == "merge" and binarize(same_ru) and binarize(same_en):
        reasons.append("обе страницы описывают одну сущность; Jev action=merge (ru/en)")
        if facets:
            reasons.append("одноимённые слаги разных граней (%s) — проверить вручную" % facets)
            flags.append("facet-merge")
        return {"decision": DECISION_MERGE, "score": round(conf, 3),
                "reasons": reasons, "flags": flags, "signals": signals}

    if act_ru == act_en == "keep":
        reasons.append("Jev action=keep (ru/en); same_entity=%s" % (
            "true" if binarize(same_ru) else "false"))
        if facets:
            reasons.append("разные грани одной системы (%s) → обе страницы нужны" % facets)
        return {"decision": DECISION_KEEP, "score": round(conf, 3),
                "reasons": reasons, "flags": flags, "signals": signals}

    reasons.append("Jev не дал уверенного merge/keep → review")
    return {"decision": DECISION_REVIEW, "score": round(1.0 - conf, 3),
            "reasons": reasons, "flags": flags, "signals": signals}


def synthesize_prune_decision(answers: dict, page: dict) -> dict:
    """Turn one page answer set into {decision, score, reasons, flags, signals}."""
    reasons: list = []
    flags: list = []

    value_ru = ((answers or {}).get("value_ru") or {}).get("score")
    value_en = ((answers or {}).get("value_en") or {}).get("score")
    prune_ru = _noul(answers, "prune_ru")
    prune_en = _noul(answers, "prune_en")

    signals = {
        "value_ru": value_ru, "value_en": value_en,
        "prune_ru": prune_ru, "prune_en": prune_en,
        "text_chars": len((page.get("text") or "").strip()),
        "page_kind": page.get("page_kind"),
        "source": page.get("source_id", page.get("source")),
    }

    weak = weak_signals(answers)
    prune_diverges = binarize(prune_ru) != binarize(prune_en)
    value_diverges = (
        value_ru is not None and value_en is not None
        and round(float(value_ru)) != round(float(value_en))
    )

    if prune_diverges:
        flags.append("ru-en-prune-divergence")
        reasons.append("расхождение RU/EN по prune (ru=%.2f, en=%.2f) → review" % (
            float(prune_ru or 0), float(prune_en or 0)))
    if value_diverges:
        flags.append("ru-en-value-divergence")
        reasons.append("расхождение RU/EN по value (%s/%s) → review" % (value_ru, value_en))
    if weak:
        flags.append("weak-signal")
        reasons.append("слабые сигналы noul (0.35–0.65): %s → review" % ", ".join(
            "%.2f" % v for v in weak))

    if prune_diverges or value_diverges or weak:
        return {"decision": DECISION_REVIEW, "score": 0.5,
                "reasons": reasons, "flags": flags, "signals": signals}

    both_prune = binarize(prune_ru) and binarize(prune_en)
    low_value = value_ru is not None and value_en is not None and \
        float(value_ru) <= 1.0 and float(value_en) <= 1.0

    if both_prune and low_value:
        reasons.append("Jev: безопасно удалить (prune=да ru/en), value<=1 → prune-сигнал")
        try:
            score = 1.0 - (float(value_ru) + float(value_en)) / 4.0
        except (TypeError, ValueError):
            score = 0.5
        return {"decision": "prune", "score": round(score, 3),
                "reasons": reasons, "flags": flags, "signals": signals}

    reasons.append("Jev: ценность есть или удаление небезопасно → keep")
    return {"decision": DECISION_KEEP, "score": 0.5,
            "reasons": reasons, "flags": flags, "signals": signals}


# ---------------------------------------------------------------------------
# Candidate generation (mode A)
# ---------------------------------------------------------------------------

def _facet_label(page_a: dict, page_b: dict) -> str:
    fa, fb = slug_facet(page_a.get("slug", "")), slug_facet(page_b.get("slug", ""))
    if slug_base(page_a.get("slug", "")) == slug_base(page_b.get("slug", "")) and fa and fb and fa != fb:
        return "%s vs %s" % (fa, fb)
    return ""


def generate_pair_candidates(pages: list, threshold: float = DIFFLIB_THRESHOLD,
                             text_jaccard: float = TEXT_JACCARD_PREFILTER,
                             max_pairs: int | None = None) -> list:
    """Return candidate page pairs with a difflib-based similarity score."""
    enriched = []
    for page in pages:
        text = page.get("text") or ""
        fingerprint = page.get("content_hash") or (
            text_fingerprint(normalize_text_for_compare(text)) if text.strip() else None)
        enriched.append({
            "page": page,
            "slug_norm": normalize_slug(page.get("slug", "")),
            "base": slug_base(page.get("slug", "")),
            "title_norm": normalize_slug(page.get("title", "")),
            "tokens": token_set(text),
            "fingerprint": fingerprint,
        })

    def same_fingerprint(a, b):
        return bool(a["fingerprint"]) and a["fingerprint"] == b["fingerprint"]

    candidates = {}
    n = len(enriched)

    def put(i, j, score):
        key = (i, j)
        if key not in candidates or score > candidates[key]:
            candidates[key] = score

    for i in range(n):
        a = enriched[i]
        for j in range(i + 1, n):
            b = enriched[j]
            slug_ratio = max(
                ratio(a["slug_norm"], b["slug_norm"]),
                ratio(a["base"], b["base"]),
                ratio(a["title_norm"], b["title_norm"]),
            )
            same_base = bool(a["base"]) and a["base"] == b["base"]
            same_fp = same_fingerprint(a, b)
            if not same_base and slug_ratio < threshold and not same_fp:
                # Cheap prefilter before any text difflib work.
                if jaccard(a["tokens"], b["tokens"]) < text_jaccard:
                    continue
            text_ratio = ratio(a["page"].get("text", ""), b["page"].get("text", ""))
            best = max(slug_ratio, text_ratio, 1.0 if same_fp else 0.0)
            if best >= threshold or same_base or same_fp:
                put(i, j, best)

    out = []
    for (i, j), score in candidates.items():
        a, b = enriched[i]["page"], enriched[j]["page"]
        identical = same_fingerprint(enriched[i], enriched[j]) and bool((a.get("text") or "").strip())
        out.append({
            "score": round(score, 4),
            "identical": identical,
            "facets": _facet_label(a, b),
            "page_a": a,
            "page_b": b,
        })
    out.sort(key=lambda c: (c["identical"], c["score"]), reverse=True)
    if max_pairs is not None:
        out = out[:max_pairs]
    return out


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

def estimate_report(calls: int) -> dict:
    usd = calls * USD_PER_CALL_ESTIMATE
    return {
        "calls": calls,
        "usd_per_call": USD_PER_CALL_ESTIMATE,
        "usd": round(usd, 5),
        "budget_usd": DEFAULT_MAX_BUDGET_USD,
        "under_budget": usd <= DEFAULT_MAX_BUDGET_USD,
        "model": MODEL,
        "note": "оценка по верхней границе пилота 2026-09-19 ($0.0000574/вызов)",
    }


def _pair_record(candidate: dict, decision: dict) -> dict:
    a, b = candidate["page_a"], candidate["page_b"]
    return {
        "id": "pair::%s|%s" % (a.get("slug", ""), b.get("slug", "")),
        "a": {"slug": a.get("slug"), "source": a.get("source_id", a.get("source")),
              "type": a.get("type"), "page_kind": a.get("page_kind")},
        "b": {"slug": b.get("slug"), "source": b.get("source_id", b.get("source")),
              "type": b.get("type"), "page_kind": b.get("page_kind")},
        "candidate_score": candidate["score"],
        "identical_text": candidate["identical"],
        "facets": candidate["facets"] or None,
        "decision": decision["decision"],
        "confidence": decision["score"],
        "reasons": decision["reasons"],
        "flags": decision["flags"],
        "signals": decision["signals"],
    }


def _prune_record(page: dict, decision: dict, excluded: tuple | None = None) -> dict:
    rec = {
        "id": "page::%s|%s" % (page.get("slug", ""),
                               page.get("source_id", page.get("source", ""))),
        "slug": page.get("slug"),
        "source": page.get("source_id", page.get("source")),
        "type": page.get("type"),
        "page_kind": page.get("page_kind"),
        "decision": decision["decision"],
        "confidence": decision["score"],
        "reasons": decision["reasons"],
        "flags": decision["flags"],
        "signals": decision["signals"],
    }
    if excluded:
        rec["excluded"] = excluded[0]
        rec["exclude_reason"] = excluded[1]
    return rec


def prune_exclusion(page: dict):
    """Hard doctrinal exclusions for the prune mode. Returns (reason, note)."""
    kind = (page.get("page_kind") or "markdown").lower()
    source = (page.get("source_id", page.get("source")) or "")
    if kind in PRUNE_EXCLUDED_PAGE_KINDS:
        return ("code-index page (page_kind=code) — navigation layer, not junk",
                "page_kind=%s" % kind)
    if source in PRUNE_EXCLUDED_SOURCES:
        return ("agent-floor source — not canon, not pruned",
                "source=%s" % source)
    return None


def _base_report(mode: str, generated_at: str, started_at: str) -> dict:
    return {
        "tool": TOOL,
        "version": TOOL_VERSION,
        "generated_at": generated_at,
        "started_at": started_at,
        "mode": mode,
        "model": MODEL,
        "read_only": True,
        "blocks": {DECISION_MERGE: [], DECISION_KEEP: [], DECISION_REVIEW: []},
        "excluded": [],
        "skips": [],
        "prune": [],
        "judged": [],
        "estimate": estimate_report(0),
        "counts": {"merge": 0, "keep": 0, "review": 0, "prune": 0, "excluded": 0},
    }


def _finalize_blocks(report: dict, sort_keys=("merge", "review", "keep")):
    for key in sort_keys:
        report["blocks"][key].sort(key=lambda r: r.get("confidence", 0), reverse=True)
        for rank, rec in enumerate(report["blocks"][key], start=1):
            rec["rank"] = rank
    report["prune"].sort(key=lambda r: (
        (r.get("signals") or {}).get("text_chars", 0), -r.get("confidence", 0)))
    for rank, rec in enumerate(report["prune"], start=1):
        rec["rank"] = rank
    report["counts"] = {
        DECISION_MERGE: len(report["blocks"][DECISION_MERGE]),
        DECISION_KEEP: len(report["blocks"][DECISION_KEEP]),
        DECISION_REVIEW: len(report["blocks"][DECISION_REVIEW]),
        "prune": len(report["prune"]),
        "excluded": len(report.get("excluded", [])),
    }


def run_pairs_live(pages: list, key: str, model: str, threshold: float,
                   max_pairs: int, max_calls: int, retries: int = 4,
                   timeout: int = 150, opener=None,
                   started_at: str | None = None) -> dict:
    started_at = started_at or utc_now_iso()
    report = _base_report("pairs", utc_now_iso(), started_at)

    candidates = generate_pair_candidates(pages, threshold=threshold, max_pairs=max_pairs)
    jev_calls = [c for c in candidates if not c["identical"]]
    report["estimate"] = estimate_report(len(jev_calls))
    report["candidates"] = len(candidates)

    if len(jev_calls) > max_calls:
        report["skips"].append({
            "reason": "estimate-over-max-calls",
            "note": "candidates=%d > max_calls=%d; raise --max-calls to run" % (
                len(jev_calls), max_calls),
        })
        report["decisions"] = []
        _finalize_blocks(report)
        return report

    questions = pair_questions()
    decisions = []
    for candidate in candidates:
        if candidate["identical"]:
            decision = synthesize_pair_decision({}, identical=True,
                                                similarity=candidate["score"],
                                                facets=candidate["facets"])
        else:
            answers = None
            state = build_pair_state(candidate["page_a"], candidate["page_b"],
                                     candidate["score"])
            for attempt in range(max(1, retries)):
                result = call_jev(state, questions, key, model=model,
                                  timeout=timeout, retries=1, opener=opener)
                answers = answers_from_record(result)
                if answers is not None:
                    break
                if attempt == 0:
                    time.sleep(min(1.5 * (attempt + 1), 4))
            if answers is None:
                report["skips"].append({
                    "reason": "jev-unavailable",
                    "note": "pair %s::%s skipped (HTTP/network)" % (
                        candidate["page_a"].get("slug"), candidate["page_b"].get("slug")),
                })
                continue
            decision = synthesize_pair_decision(answers,
                                                similarity=candidate["score"],
                                                facets=candidate["facets"])
        record = _pair_record(candidate, decision)
        decisions.append(record)
        report["blocks"][decision["decision"]].append(record)

    report["decisions"] = decisions
    _finalize_blocks(report)
    return report


def run_prune_live(pages: list, key: str, model: str, max_calls: int,
                   retries: int = 4, timeout: int = 150, opener=None,
                   started_at: str | None = None) -> dict:
    started_at = started_at or utc_now_iso()
    report = _base_report("prune", utc_now_iso(), started_at)

    eligible, excluded = [], []
    for page in pages:
        reason = prune_exclusion(page)
        if reason:
            rec = _prune_record(
                page, {"decision": "excluded", "score": 0.0, "reasons": [reason[0]],
                       "flags": ["hard-exclusion"], "signals": {}}, excluded=reason)
            excluded.append(rec)
            report["judged"].append(rec)
        else:
            eligible.append(page)

    report["excluded"] = excluded
    report["estimate"] = estimate_report(len(eligible))
    report["candidates"] = len(eligible)

    if len(eligible) > max_calls:
        report["skips"].append({
            "reason": "estimate-over-max-calls",
            "note": "eligible pages=%d > max_calls=%d; raise --max-calls to run" % (
                len(eligible), max_calls),
        })
        _finalize_blocks(report)
        return report

    questions = prune_questions()
    for page in eligible:
        state = build_page_state(page)
        result = call_jev(state, questions, key, model=model,
                          timeout=timeout, retries=retries, opener=opener)
        answers = answers_from_record(result)
        if answers is None:
            report["skips"].append({
                "reason": "jev-unavailable",
                "note": "page %s::%s skipped (HTTP/network)" % (
                    page.get("slug"), page.get("source_id", page.get("source"))),
            })
            continue
        decision = synthesize_prune_decision(answers, page)
        record = _prune_record(page, decision)
        report["judged"].append(record)
        if decision["decision"] == "prune":
            report["prune"].append(record)
        elif decision["decision"] == DECISION_KEEP:
            report["blocks"][DECISION_KEEP].append(record)
        else:
            report["blocks"][DECISION_REVIEW].append(record)

    _finalize_blocks(report)
    return report


# ---------------------------------------------------------------------------
# Replay (offline; recorded pilot answers)
# ---------------------------------------------------------------------------

def load_jsonl(path: str) -> list:
    out = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def load_jobs(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    return {rec.get("id"): rec for rec in load_jsonl(path) if rec.get("id")}


def split_pair_state(state: str):
    """Split a recorded pair state back into two pseudo-page dicts."""
    if "=====" not in state:
        return None, None
    raw_a, raw_b = state.split("=====", 1)

    def parse(block):
        slug = source = ptype = kind = ""
        lines = block.strip("\n").splitlines()
        body_start = 0
        for idx, line in enumerate(lines):
            low = line.strip().lower()
            if low.startswith("slug:"):
                slug = line.split(":", 1)[1].strip()
            elif low.startswith("source:"):
                source = line.split(":", 1)[1].strip()
            elif low.startswith("type:"):
                ptype = line.split(":", 1)[1].strip()
            elif low.startswith("page_kind:"):
                kind = line.split(":", 1)[1].strip()
            elif not line.strip():
                body_start = idx + 1
                break
        return {"slug": slug, "source_id": source, "type": ptype,
                "page_kind": kind or "markdown",
                "text": "\n".join(lines[body_start:]).strip()}

    return parse(raw_a), parse(raw_b)


def run_pairs_replay(results_path: str, jobs_path: str | None = None,
                     labels_path: str | None = None,
                     started_at: str | None = None) -> dict:
    started_at = started_at or utc_now_iso()
    report = _base_report("pairs", utc_now_iso(), started_at)
    report["replay"] = True
    report["input"] = {"results": results_path, "jobs": jobs_path, "labels": labels_path}
    records = load_jsonl(results_path)
    jobs = load_jobs(jobs_path)
    labels = _load_labels(labels_path)
    decisions = []
    for record in records:
        rid = record.get("id")
        answers = answers_from_record(record)
        meta = record.get("meta") or {}
        job = jobs.get(rid) or {}
        page_a, page_b = split_pair_state(job.get("state", ""))
        if page_a is None:
            page_a = {"slug": meta.get("a", ""), "source_id": meta.get("sa", ""),
                      "type": "", "page_kind": "markdown"}
            page_b = {"slug": meta.get("b", ""), "source_id": meta.get("sb", ""),
                      "type": "", "page_kind": "markdown"}
        identical = False
        similarity = None
        if page_a is not None and page_b is not None:
            text_a = normalize_text_for_compare(page_a.get("text", ""))
            text_b = normalize_text_for_compare(page_b.get("text", ""))
            identical = bool(text_a) and text_a == text_b
        facets = _facet_label(page_a, page_b)
        if answers is None:
            report["skips"].append({"reason": "result-not-ok",
                                    "note": "id=%s skipped (record ok=%s)" % (rid, record.get("ok"))})
            continue
        decision = synthesize_pair_decision(answers, identical=identical,
                                            similarity=similarity, facets=facets)
        candidate = {"score": 0.0, "identical": identical, "facets": facets,
                     "page_a": page_a, "page_b": page_b}
        rec = _pair_record(candidate, decision)
        rec["id"] = rid
        if labels.get(rid):
            rec["label"] = labels[rid]
        decisions.append(rec)
        report["blocks"][decision["decision"]].append(rec)
    report["decisions"] = decisions
    _finalize_blocks(report)
    if labels:
        report["label_check"] = compare_pair_labels(decisions, labels)
    report["estimate"] = estimate_report(0)
    return report


def run_prune_replay(results_path: str, jobs_path: str | None = None,
                     labels_path: str | None = None,
                     started_at: str | None = None) -> dict:
    started_at = started_at or utc_now_iso()
    report = _base_report("prune", utc_now_iso(), started_at)
    report["replay"] = True
    report["input"] = {"results": results_path, "jobs": jobs_path, "labels": labels_path}
    records = load_jsonl(results_path)
    jobs = load_jobs(jobs_path)
    labels = _load_labels(labels_path)
    for record in records:
        rid = record.get("id")
        answers = answers_from_record(record)
        job = jobs.get(rid) or {}
        meta = job.get("meta") or {}
        page = _page_from_job_state(job.get("state", ""), meta)
        reason = prune_exclusion(page)
        if reason:
            rec = _prune_record(
                page, {"decision": "excluded", "score": 0.0,
                       "reasons": [reason[0]], "flags": ["hard-exclusion"], "signals": {}},
                excluded=reason)
            rec["id"] = rid
            if labels.get(rid):
                rec["label"] = labels[rid]
            report["excluded"].append(rec)
            report["judged"].append(rec)
            continue
        if answers is None:
            report["skips"].append({"reason": "result-not-ok",
                                    "note": "id=%s skipped (record ok=%s)" % (rid, record.get("ok"))})
            continue
        decision = synthesize_prune_decision(answers, page)
        rec = _prune_record(page, decision)
        rec["id"] = rid
        if labels.get(rid):
            rec["label"] = labels[rid]
        report["judged"].append(rec)
        if decision["decision"] == "prune":
            report["prune"].append(rec)
        elif decision["decision"] == DECISION_KEEP:
            report["blocks"][DECISION_KEEP].append(rec)
        else:
            report["blocks"][DECISION_REVIEW].append(rec)
    _finalize_blocks(report)
    if labels:
        report["label_check"] = compare_prune_labels(report, labels)
    report["estimate"] = estimate_report(0)
    return report


def _page_from_job_state(state: str, meta: dict) -> dict:
    page = {"slug": meta.get("slug", ""), "source_id": meta.get("source", ""),
            "type": "", "page_kind": "markdown", "text": ""}
    if state:
        for line in state.splitlines():
            low = line.strip().lower()
            if low.startswith("**slug:**"):
                page["slug"] = line.split("**", 2)[-1].strip()
            elif low.startswith("**source:**"):
                page["source_id"] = line.split("**", 2)[-1].strip()
            elif low.startswith("**type:**"):
                page["type"] = line.split("**", 2)[-1].strip()
        if "\n\n" in state:
            page["text"] = state.split("\n\n", 1)[1]
    if (page.get("type") or "").lower() == "code":
        # The recorded pilot state exposes code pages through `type`, not
        # `page_kind`; map it so the hard code-index exclusion applies offline.
        page["page_kind"] = "code"
    return page


# ---------------------------------------------------------------------------
# Label comparison (pilot ground truth)
# ---------------------------------------------------------------------------

def _load_labels(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def compare_pair_labels(decisions: list, labels: dict) -> dict:
    mismatches = []
    same_ok = action_ok = compared = 0
    for rec in decisions:
        label = labels.get(rec["id"])
        if not label:
            continue
        compared += 1
        sig = rec.get("signals") or {}
        got_action = sig.get("action_ru")
        same_match = (binarize(sig.get("same_ru")) == label.get("same")
                      and binarize(sig.get("same_en")) == label.get("same"))
        action_match = (got_action == label.get("action")
                        and sig.get("action_en") == label.get("action"))
        same_ok += 1 if same_match else 0
        action_ok += 1 if action_match else 0
        if not (same_match and action_match):
            mismatches.append({
                "id": rec["id"],
                "label_same": label.get("same"),
                "label_action": label.get("action"), "got_action": got_action,
                "got_action_en": sig.get("action_en"),
                "tool_decision": rec["decision"],
                "label_note": label.get("note"),
            })
    return {
        "compared": compared,
        "same_match": same_ok,
        "action_match": action_ok,
        "same_accuracy": round(same_ok / compared, 3) if compared else None,
        "action_accuracy": round(action_ok / compared, 3) if compared else None,
        "mismatches": mismatches,
    }


def compare_prune_labels(report: dict, labels: dict) -> dict:
    mismatches = []
    compared = kept = 0
    by_id = {}
    for rec in report.get("judged", []):
        by_id[rec["id"]] = rec
    for rid, label in labels.items():
        key = rid if rid.startswith("page::") else "page::" + rid
        rec = by_id.get(key)
        if not rec:
            continue
        compared += 1
        prunable = bool(label.get("prunable"))
        got_prune = rec["decision"] == "prune"
        if prunable == got_prune:
            kept += 1
        else:
            mismatches.append({
                "id": rid, "label_prunable": prunable,
                "got_decision": rec["decision"],
                "label_note": label.get("note"),
                "flags": rec.get("flags"),
            })
    return {
        "compared": compared,
        "prune_match": kept,
        "prune_accuracy": round(kept / compared, 3) if compared else None,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

DECISION_LABEL_RU = {
    DECISION_MERGE: "merge — уверенные дубли/устаревшие фрагменты",
    DECISION_KEEP: "keep — оставить как есть",
    DECISION_REVIEW: "review — слабые сигналы / расхождения RU-EN / неясно",
    "prune": "prune — безопасно удалить (сигнал, не действие)",
}


def render_markdown(report: dict) -> str:
    lines = []
    mode = report.get("mode")
    lines.append("# Jev triage report — %s" % mode)
    lines.append("")
    lines.append("- tool: `%s` v%s" % (report["tool"], report["version"]))
    lines.append("- model: `%s`" % report["model"])
    lines.append("- generated_at: %s" % report["generated_at"])
    lines.append("- read-only: **%s** (no page was written)" % report["read_only"])
    if report.get("replay"):
        lines.append("- replay: yes (recorded answers; no DB / no key used)")
        lines.append("- input: `%s`" % json.dumps(report.get("input", {}), ensure_ascii=False))
    estimate = report.get("estimate") or {}
    lines.append("- estimate: %s calls x $%.5f ≈ **$%.5f** (budget $%.2f, under=%s)" % (
        estimate.get("calls", 0), estimate.get("usd_per_call", 0),
        estimate.get("usd", 0), estimate.get("budget_usd", 0),
        estimate.get("under_budget")))
    lines.append("")

    counts = report.get("counts") or {}
    lines.append("## Summary")
    lines.append("")
    lines.append("| block | count |")
    lines.append("|---|---|")
    for key in (DECISION_MERGE, DECISION_KEEP, DECISION_REVIEW, "prune", "excluded"):
        lines.append("| %s | %d |" % (key, counts.get(key, 0)))
    lines.append("")

    if report.get("label_check"):
        check = report["label_check"]
        lines.append("## Label cross-check (pilot ground truth)")
        lines.append("")
        if "same_accuracy" in check:
            lines.append("- compared: %d; same_entity match: %s (%.0f%%); action match: %s (%.0f%%)" % (
                check["compared"],
                check.get("same_match"), 100 * (check.get("same_accuracy") or 0),
                check.get("action_match"), 100 * (check.get("action_accuracy") or 0)))
        else:
            lines.append("- compared: %d; prune match: %s (%.0f%%)" % (
                check["compared"], check.get("prune_match"),
                100 * (check.get("prune_accuracy") or 0)))
        for mismatch in check.get("mismatches", []):
            lines.append("- mismatch: `%s` — %s" % (
                mismatch.get("id"), json.dumps(mismatch, ensure_ascii=False)))
        lines.append("")

    for key in (DECISION_MERGE, DECISION_KEEP, DECISION_REVIEW):
        block = report["blocks"].get(key, [])
        if not block:
            continue
        lines.append("## %s (%d)" % (DECISION_LABEL_RU[key], len(block)))
        lines.append("")
        for rec in block:
            lines.append("### %d. `%s` (conf %.2f)" % (rec["rank"], rec["id"], rec.get("confidence", 0)))
            lines.append("")
            if "a" in rec:
                lines.append("- A: `%s` (%s, %s)" % (
                    rec["a"].get("slug"), rec["a"].get("source"), rec["a"].get("type")))
                lines.append("- B: `%s` (%s, %s)" % (
                    rec["b"].get("slug"), rec["b"].get("source"), rec["b"].get("type")))
                lines.append("- candidate score: %s; identical_text=%s; facets=%s" % (
                    rec.get("candidate_score"), rec.get("identical_text"),
                    rec.get("facets")))
            else:
                lines.append("- slug: `%s` (%s, %s)" % (
                    rec.get("slug"), rec.get("source"), rec.get("type")))
            for reason in rec.get("reasons", []):
                lines.append("- reason: %s" % reason)
            lines.append("- signals: `%s`" % json.dumps(rec.get("signals", {}), ensure_ascii=False))
            lines.append("")

    if report.get("prune"):
        lines.append("## prune signals (%d)" % len(report["prune"]))
        lines.append("")
        for rec in report["prune"]:
            lines.append("- #%d `%s` (%s) decision=%s chars=%s" % (
                rec.get("rank"), rec.get("slug"), rec.get("source"),
                rec.get("decision"), (rec.get("signals") or {}).get("text_chars")))
            for reason in rec.get("reasons", []):
                lines.append("  - %s" % reason)
        lines.append("")

    if report.get("excluded"):
        lines.append("## hard exclusions (%d, never pruned)" % len(report["excluded"]))
        lines.append("")
        for rec in report["excluded"]:
            lines.append("- `%s` (%s): %s" % (
                rec.get("slug"), rec.get("source"), rec.get("exclude_reason")))
        lines.append("")

    weak = [r for r in report["blocks"].get(DECISION_REVIEW, []) if "weak-signal" in (r.get("flags") or [])]
    lines.append("## weak signals & RU/EN divergences (review block: %d)" % len(weak))
    lines.append("")
    for rec in weak:
        lines.append("- `%s` flags=%s signals=%s" % (
            rec["id"], rec.get("flags"), json.dumps(rec.get("signals", {}), ensure_ascii=False)))
    lines.append("")

    if report.get("skips"):
        lines.append("## skips (exit 0)")
        lines.append("")
        for skip in report["skips"]:
            lines.append("- %s: %s" % (skip.get("reason"), skip.get("note")))
        lines.append("")

    lines.append("## Read-only verification")
    lines.append("")
    verification = report.get("read_only_verification")
    if verification:
        lines.append("- `%s`" % verification.get("query"))
        lines.append("- pages changed since %s: **%s**" % (
            verification.get("since"), verification.get("changed_pages")))
    else:
        lines.append("- not run (replay/offline or DB unavailable): no write path exists in the tool")
    lines.append("")
    return "\n".join(lines)


def write_outputs(report: dict, out_dir: str | None, as_json: bool) -> None:
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        stem = "jev_triage_%s" % report.get("mode", "report")
        with open(os.path.join(out_dir, stem + ".json"), "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
        with open(os.path.join(out_dir, stem + ".md"), "w", encoding="utf-8") as handle:
            handle.write(render_markdown(report))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev_triage.py",
        description="Read-only gbrain duplicate/stale/prune triage report (Jev).",
    )
    parser.add_argument("--mode", choices=("pairs", "prune"), default="pairs")
    parser.add_argument("--json", action="store_true", help="print JSON report to stdout")
    parser.add_argument("--out", metavar="DIR", help="also write <mode>.json + <mode>.md")
    parser.add_argument("--replay", action="store_true",
                        help="replay recorded Jev answers; no DB and no key needed")
    parser.add_argument("--results", metavar="PATH", help="recorded results jsonl (replay)")
    parser.add_argument("--jobs", metavar="PATH", help="recorded jobs jsonl (replay, optional)")
    parser.add_argument("--labels", metavar="PATH", help="ground-truth labels json (replay, optional)")
    parser.add_argument("--limit", type=int, default=None, help="cap pages read from the DB")
    parser.add_argument("--max-pairs", type=int, default=DEFAULT_MAX_PAIRS)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--threshold", type=float, default=DIFFLIB_THRESHOLD)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--psql", default=None, help="psql binary path (default: $PATH)")
    parser.add_argument("--allow-over-budget", action="store_true",
                        help="proceed even when the estimate exceeds the pilot budget")
    parser.add_argument("--print-estimate-only", action="store_true")
    return parser


def emit_skip(reason: str, note: str, as_json: bool) -> int:
    payload = {"ok": False, "skipped": True, "reason": reason, "note": note,
               "read_only": True, "exit_code": 0}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("[jev-triage] SKIP (%s): %s" % (reason, note))
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started_at = utc_now_iso()
    env = os.environ

    if args.model != MODEL:
        print("[jev-triage] NOTE: model overridden to %s (pinned: %s)" % (args.model, MODEL),
              file=sys.stderr)

    if args.replay:
        if not args.results:
            return emit_skip("replay-without-results",
                             "pass --results <recorded.jsonl> with --replay", args.json)
        if not os.path.exists(args.results):
            return emit_skip("replay-results-missing",
                             "results file not found: %s" % args.results, args.json)
        if args.mode == "pairs":
            report = run_pairs_replay(args.results, args.jobs, args.labels, started_at)
        else:
            report = run_prune_replay(args.results, args.jobs, args.labels, started_at)
        report["estimate"] = estimate_report(0)
        write_outputs(report, args.out, args.json)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            print(render_markdown(report))
        return 0

    key = api_key_from_env(env)
    if not key:
        return emit_skip("no-openrouter-key",
                         "OPENROUTER_API_KEY is not set; no page was touched", args.json)

    url = db_url_from_env(env)
    if not url:
        return emit_skip("no-database",
                         "GBRAIN_DATABASE_URL is not set; no page was touched", args.json)

    if not shutil.which("psql") and not args.psql:
        return emit_skip("psql-not-found",
                         "psql binary is not available; no page was touched", args.json)

    pages, err = fetch_pages(url, psql_bin=args.psql, limit=args.limit)
    if err:
        return emit_skip("db-unavailable",
                         "read-only page fetch failed (%s); no page was touched" % err,
                         args.json)
    print("[jev-triage] read %d pages (read-only SELECT)" % len(pages), file=sys.stderr)

    if args.print_estimate_only:
        if args.mode == "pairs":
            candidates = generate_pair_candidates(pages, threshold=args.threshold,
                                                  max_pairs=args.max_pairs)
            calls = len([c for c in candidates if not c["identical"]])
        else:
            calls = len([p for p in pages if not prune_exclusion(p)])
        estimate = estimate_report(calls)
        payload = {"ok": True, "estimate": estimate, "read_only": True}
        print(json.dumps(payload, ensure_ascii=False) if args.json
              else "estimate: %s calls ≈ $%.5f" % (estimate["calls"], estimate["usd"]))
        return 0

    if args.mode == "pairs":
        candidates = generate_pair_candidates(pages, threshold=args.threshold,
                                              max_pairs=args.max_pairs)
        est_calls = len([c for c in candidates if not c["identical"]])
    else:
        est_calls = len([p for p in pages if not prune_exclusion(p)])
    estimate = estimate_report(est_calls)
    print("[jev-triage] estimate: %d calls ≈ $%.5f (budget $%.2f; pilot upper bound)" % (
        estimate["calls"], estimate["usd"], DEFAULT_MAX_BUDGET_USD), file=sys.stderr)
    if not estimate["under_budget"] and not args.allow_over_budget:
        return emit_skip("estimate-over-budget",
                         "estimated $%.5f > budget $%.2f; rerun with --allow-over-budget" % (
                             estimate["usd"], DEFAULT_MAX_BUDGET_USD), args.json)

    if args.mode == "pairs":
        report = run_pairs_live(pages, key, args.model, args.threshold,
                                args.max_pairs, args.max_calls, retries=args.retries,
                                timeout=args.timeout, started_at=started_at)
    else:
        report = run_prune_live(pages, key, args.model, args.max_calls,
                                retries=args.retries, timeout=args.timeout,
                                started_at=started_at)
    report["estimate"] = estimate

    changed, verr = verify_read_only(url, started_at, psql_bin=args.psql)
    if verr is None:
        report["read_only_verification"] = {
            "query": SQL_VERIFY_UNCHANGED,
            "since": started_at,
            "changed_pages": changed,
            "ok": changed == 0,
        }

    write_outputs(report, args.out, args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
