#!/usr/bin/env python3
"""decision-stand — compare decision models on `LocalLLaMA/typed-decisions`.

One stand, one dataset, one set of metrics, so numbers from different
"System 1" decision engines (TypeSafe Jev, Laya, future contenders) are
comparable side by side.

The benchmark: 400 test cases x 5 typed questions = 2 000 decisions over
`noul` / `choice` / `score` primitives, with soft gold distributions
(mean of three teacher samples). Gold is agreement-with-the-teacher, not
truth -- the dataset card is explicit about that.

Modes
-----
fetch     pull the dataset (test split) via the HF datasets-server API  -> data/dataset.json
estimate  token/cost estimate for a `run-jev` pass (no API calls, no spend)
run-jev   replay every row through OpenRouter `alpha/decisions` (pinned model) -> out/results_jev.jsonl
report    metrics for every out/results_*.jsonl against the gold -> JSON + Markdown

Properties, by convention of this repo's tools (see tools/jev-triage):
  * stdlib only -- no third-party imports;
  * the API key is read from OPENROUTER_API_KEY (or `--env-file`), never
    logged, printed, or written into any artifact;
  * results are appended incrementally to JSONL, so an interrupted run
    resumes by skipping ids already recorded;
  * every run writes a `<out>.meta.json` sidecar (model, endpoint, dataset
    fingerprint, counts) for reproducibility;
  * `--limit`/`--max-usd` bound a pass before it starts.

The Laya leg lives in `run_laya.py` (it needs torch/transformers, so it is
deliberately not importable from this stdlib tool); it writes the same JSONL
schema and `report` consumes both.

Metrics (matching the dataset card's vocabulary where possible)
---------------------------------------------------------------
accuracy      argmax of the model distribution == gold argmax
soft acc      model probability mass on the gold argmax (mean over decisions)
Brier         sum of squared differences over the aligned support
TV            total variation distance to the gold distribution
ECE           top-label calibration, 15 equal bins (choice/score: max prob;
              noul: max(p, 1-p)); correctness as above
score MAE     for `score` questions: |expected score - gold expected score|
within 1      for `score` questions: |round(score) - gold argmax level| <= 1
latency       mean/p50/p95 of per-case wall time as recorded by the runner

Usage
-----
    python3 tools/decision-stand/decision_stand.py --mode fetch
    python3 tools/decision-stand/decision_stand.py --mode estimate
    OPENROUTER_API_KEY=... python3 tools/decision-stand/decision_stand.py --mode run-jev
    python3 tools/decision-stand/decision_stand.py --mode report
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "data", "dataset.json")
DEFAULT_OUT = os.path.join(HERE, "out")

DATASET = "LocalLLaMA/typed-decisions"
DATASET_CONFIG = "all"
DATASET_SPLIT = "test"
ROWS_API = "https://datasets-server.huggingface.co/rows"
PAGE = 100

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13-20260917"          # pinned; aliases move
MODEL_INPUT_USD_PER_MTOK = 0.042              # published Jev rate
ECE_BINS = 15


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# dataset
# --------------------------------------------------------------------------

def http_json(url: str, timeout: int = 60):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_rows(limit=None, page=PAGE) -> list[dict]:
    """Pull the test split via the datasets-server API (no HF token needed)."""
    rows, offset = [], 0
    while True:
        url = ("%s?dataset=%s&config=%s&split=%s&offset=%d&length=%d"
               % (ROWS_API, urllib.parse.quote(DATASET), DATASET_CONFIG,
                  DATASET_SPLIT, offset, page))
        payload = http_json(url)
        batch = payload.get("rows") or []
        for item in batch:
            row = item.get("row") or {}
            rows.append({
                "id": row.get("id"),
                "workflow": row.get("workflow"),
                "state": _as_obj(row.get("state")),
                "questions": _as_obj(row.get("questions")),
                "gold": _as_obj(row.get("gold")),
            })
        offset += len(batch)
        total = payload.get("num_rows_total") or 0
        print("fetched %d/%s" % (len(rows), total or "?"), file=sys.stderr, flush=True)
        if not batch or (total and offset >= total) or (limit and len(rows) >= limit):
            break
    return rows[:limit] if limit else rows


def _as_obj(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def save_dataset(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"dataset": DATASET, "config": DATASET_CONFIG, "split": DATASET_SPLIT,
                   "fetched_at": utc_now(), "rows": rows}, fh, ensure_ascii=False, indent=1)


def load_dataset(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def dataset_fingerprint(rows: list[dict]) -> str:
    import hashlib
    h = hashlib.sha256()
    for row in rows:
        h.update(str(row.get("id")).encode())
        h.update(json.dumps(row.get("gold"), sort_keys=True).encode())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Jev transport (same shape as tools/jev-triage)
# --------------------------------------------------------------------------

def api_key(env_file: str | None = None) -> str:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key or not env_file:
        return key
    with open(env_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def call_jev(state, questions, key, model=MODEL, endpoint=ENDPOINT,
             timeout=150, retries=4, opener=None):
    """One decisions call. Never logs or returns the key."""
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
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            last = {"ok": False, "err": "network-%s" % type(exc).__name__,
                    "latency_ms": int((time.time() - started) * 1000)}
            time.sleep(min(1.5 * (attempt + 1), 6))
    return last


def answers_of(record: dict):
    resp = (record or {}).get("resp") or {}
    answers = resp.get("answers")
    return answers if isinstance(answers, dict) else None


def usage_cost(record: dict) -> float:
    """Cost from a JSONL record (`usage`) or a raw transport record (`resp.usage`)."""
    usage = (record or {}).get("usage")
    if not isinstance(usage, dict):
        usage = ((record or {}).get("resp") or {}).get("usage") or {}
    try:
        return float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# JSONL helpers
# --------------------------------------------------------------------------

def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def done_ids(path: str) -> set:
    return {r.get("id") for r in read_jsonl(path) if r.get("id") and r.get("ok")}


def write_meta(path: str, meta: dict) -> None:
    with open(path + ".meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def gold_support(qdef: dict, gold: dict):
    """(labels, gold_dist) for one question, or None when the gold has no distribution."""
    qtype = (qdef or {}).get("type") or gold.get("type")
    probs = gold.get("probabilities")
    if qtype == "choice":
        criteria = (qdef or {}).get("criteria") or {}
        labels = [str(k) for k in criteria] or ([str(k) for k in probs] if isinstance(probs, dict) else [])
        if not labels or not isinstance(probs, dict):
            return None
        dist = [float(probs.get(L, probs.get(str(L), 0.0))) for L in labels]
        return labels, dist
    if qtype == "noul":
        pt = None
        if isinstance(probs, dict):
            pt = probs.get("true", probs.get("True"))
        if pt is None:
            pt = gold.get("noul")
        if pt is None:
            return None
        pt = float(pt)
        return ["false", "true"], [1.0 - pt, pt]
    if qtype == "score":
        criteria = (qdef or {}).get("criteria") or []
        labels = [str(i) for i in range(len(criteria))] if criteria else \
                 ([str(k) for k in probs] if isinstance(probs, dict) else [])
        if not labels or not isinstance(probs, dict):
            return None
        dist = [float(probs.get(L, 0.0)) for L in labels]
        return labels, dist
    return None


def gold_index(qdef: dict, gold: dict, labels: list) -> int | None:
    qtype = (qdef or {}).get("type") or gold.get("type")
    label = gold.get("label")
    if qtype == "noul":
        if str(label).lower() in ("true", "1", "yes"):
            return labels.index("true") if "true" in labels else 1
        if str(label).lower() in ("false", "0", "no"):
            return labels.index("false") if "false" in labels else 0
        return None
    if label is None:
        return None
    if str(label) in labels:
        return labels.index(str(label))
    try:                       # score labels may arrive as ints
        return labels.index(str(int(label)))
    except (ValueError, TypeError):
        return None


def model_dist(ans: dict, labels: list) -> list | None:
    """Model distribution aligned to `labels`; tolerant about the answer shape."""
    probs = ans.get("probabilities")
    if isinstance(probs, dict):
        return [float(probs.get(L, probs.get(str(L), 0.0))) for L in labels]
    if isinstance(probs, list) and len(probs) == len(labels):
        return [float(v) for v in probs]
    # one-hot fallback from the discrete answer, if any
    pick = ans.get("choice")
    if pick is None and ans.get("noul") is not None:
        pick = "true" if float(ans["noul"]) >= 0.5 else "false"
    if pick is None and ans.get("score") is not None and labels and all(l.isdigit() for l in labels):
        try:
            pick = str(int(round(float(ans["score"]))))
        except (TypeError, ValueError):
            pick = None
    if pick is not None and str(pick) in labels:
        idx = labels.index(str(pick))
        return [1.0 if i == idx else 0.0 for i in range(len(labels))]
    return None


def normalize(vec):
    total = sum(v for v in vec if v > 0)
    return [v / total for v in vec] if total > 0 else vec


def brier(dist, gold_dist) -> float:
    return sum((p - g) ** 2 for p, g in zip(dist, gold_dist))


def tv(dist, gold_dist) -> float:
    return 0.5 * sum(abs(p - g) for p, g in zip(dist, gold_dist))


def argmax(vec) -> int:
    return max(range(len(vec)), key=lambda i: vec[i]) if vec else -1


class Metrics:
    """Accumulator for one model over all decisions."""

    def __init__(self):
        self.n = 0
        self.correct = 0
        self.soft = 0.0
        self.brier = 0.0
        self.tv = 0.0
        self.conf_correct = []          # (conf, correct) for ECE
        self.score_mae = []
        self.within1 = []
        self.by_type = {}
        self.by_workflow = {}
        self.latencies = []
        self.cost = 0.0
        self.cases = 0

    def add_case(self, latency_ms=None, cost=None):
        self.cases += 1
        if latency_ms is not None:
            self.latencies.append(float(latency_ms))
        if cost:
            self.cost += float(cost)

    def add(self, qtype: str, gold_dist, dist, gidx, score_gold=None, score_pred=None):
        self.n += 1
        ok = argmax(dist) == gidx
        self.correct += int(ok)
        self.soft += dist[gidx]
        self.brier += brier(dist, gold_dist)
        self.tv += tv(dist, gold_dist)
        conf = max(dist)
        self.conf_correct.append((conf, ok))
        if score_gold is not None and score_pred is not None:
            self.score_mae.append(abs(score_pred - score_gold))
            self.within1.append(abs(round(score_pred) - round(score_gold)) <= 1)

    def summary(self, workflow: str | None = None) -> dict:
        if not self.n:
            return {"n": 0}
        out = {
            "n": self.n,
            "accuracy": round(self.correct / self.n, 4),
            "soft_acc": round(self.soft / self.n, 4),
            "brier": round(self.brier / self.n, 4),
            "tv": round(self.tv / self.n, 4),
            "ece": round(ece(self.conf_correct), 4),
            "mean_confidence": round(
                statistics.fmean([c for c, _ in self.conf_correct]), 4) if self.conf_correct else None,
            "cases": self.cases,
        }
        if self.score_mae:
            out["score_mae"] = round(statistics.fmean(self.score_mae), 4)
            out["within_1_level"] = round(statistics.fmean(self.within1), 4)
        if self.latencies:
            lat = sorted(self.latencies)
            out["latency_ms_mean"] = round(statistics.fmean(lat), 1)
            out["latency_ms_p50"] = round(lat[len(lat) // 2], 1)
            out["latency_ms_p95"] = round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1)
        if self.cost:
            out["cost_usd_total"] = round(self.cost, 4)
            out["cost_usd_per_case"] = round(self.cost / max(1, self.cases), 5)
        if workflow:
            out["workflow"] = workflow
        return out


def ece(pairs, bins=ECE_BINS) -> float:
    if not pairs:
        return float("nan")
    total, value = len(pairs), 0.0
    for lo_i in range(bins):
        lo, hi = lo_i / bins, (lo_i + 1) / bins
        bucket = [(c, ok) for c, ok in pairs
                  if (c > lo or (lo == 0 and c >= 0)) and c <= hi]
        if not bucket:
            continue
        conf = statistics.fmean([c for c, _ in bucket])
        acc = statistics.fmean([1.0 if ok else 0.0 for _, ok in bucket])
        value += (len(bucket) / total) * abs(conf - acc)
    return value


def evaluate(results_path: str, rows: list[dict]) -> dict:
    """Metrics for one results file against the dataset rows."""
    by_id = {r["id"]: r for r in rows}
    records = read_jsonl(results_path)
    overall = Metrics()
    per_type = {}
    per_workflow = {}
    errors = 0
    for rec in records:
        row = by_id.get(rec.get("id"))
        answers = rec.get("answers") or answers_of(rec)
        if row is None or not answers:
            errors += 1
            continue
        wf = row.get("workflow") or "unknown"
        wf_acc = per_workflow.setdefault(wf, Metrics())
        overall.add_case(latency_ms=rec.get("latency_ms"), cost=usage_cost(rec))
        wf_acc.add_case()
        for qid, qdef in (row.get("questions") or {}).items():
            gold = (row.get("gold") or {}).get(qid) or {}
            ans = answers.get(qid)
            if not isinstance(ans, dict):
                continue
            support = gold_support(qdef, gold)
            if not support:
                continue
            labels, gold_dist = support
            dist = model_dist(ans, labels)
            if dist is None:
                continue
            dist = normalize(dist)
            gold_dist = normalize(gold_dist)
            gidx = gold_index(qdef, gold, labels)
            if gidx is None:
                gidx = argmax(gold_dist)
            qtype = (qdef or {}).get("type") or gold.get("type") or "?"
            score_gold = gold.get("score")
            score_pred = ans.get("score")
            score_gold = float(score_gold) if isinstance(score_gold, (int, float)) else None
            score_pred = float(score_pred) if isinstance(score_pred, (int, float)) else None
            for target in (overall, per_type.setdefault(qtype, Metrics()), wf_acc):
                target.add(qtype, gold_dist, dist, gidx,
                           score_gold=score_gold, score_pred=score_pred)
    return {
        "results_file": os.path.basename(results_path),
        "records": len(records),
        "records_ok": len(records) - errors,
        "records_skipped": errors,
        "overall": overall.summary(),
        "by_question_type": {k: v.summary() for k, v in sorted(per_type.items())},
        "by_workflow": {k: v.summary() for k, v in sorted(per_workflow.items())},
    }


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def mode_fetch(args) -> int:
    rows = fetch_rows(limit=args.limit)
    save_dataset(args.data, rows)
    print("saved %d rows -> %s (fingerprint %s)"
          % (len(rows), args.data, dataset_fingerprint(rows)))
    return 0


def estimate_tokens(rows: list[dict]) -> int:
    total = 0
    for row in rows:
        total += (len(json.dumps(row.get("state"), ensure_ascii=False))
                  + len(json.dumps(row.get("questions"), ensure_ascii=False))) // 4
    return total


def mode_estimate(args) -> int:
    rows = load_dataset(args.data)
    if args.limit:
        rows = rows[:args.limit]
    tokens = estimate_tokens(rows)
    cost = tokens / 1_000_000 * MODEL_INPUT_USD_PER_MTOK
    print(json.dumps({
        "model": args.model, "cases": len(rows),
        "estimated_input_tokens": tokens,
        "estimated_cost_usd": round(cost, 5),
        "budget_usd": args.max_usd,
        "note": "estimate only; the pinned rate is %.3f USD/Mtok of input" % MODEL_INPUT_USD_PER_MTOK,
    }, indent=1))
    return 0 if cost <= args.max_usd else 1


def mode_run_jev(args) -> int:
    rows = load_dataset(args.data)
    if args.limit:
        rows = rows[:args.limit]
    key = api_key(args.env_file)
    if not key:
        print("no OPENROUTER_API_KEY (checked env%s) -- nothing to do"
              % (", " + args.env_file if args.env_file else ""), file=sys.stderr)
        return 1
    out = args.out or os.path.join(DEFAULT_OUT, "results_jev.jsonl")
    done = done_ids(out)
    pending = [r for r in rows if r["id"] not in done]
    print("run-jev: %d rows, %d already done, %d pending" % (len(rows), len(done), len(pending)),
          file=sys.stderr, flush=True)
    spent = 0.0
    failed = 0
    if not os.path.exists(out + ".meta.json"):
        write_meta(out, {"adapter": "jev", "model": args.model, "endpoint": ENDPOINT,
                         "dataset": DATASET, "config": DATASET_CONFIG, "split": DATASET_SPLIT,
                         "fingerprint": dataset_fingerprint(rows), "started_at": utc_now(),
                         "args": vars(args)})
    for i, row in enumerate(pending, 1):
        rec = call_jev(row["state"], row["questions"], key, model=args.model,
                       retries=args.retries)
        answers = answers_of(rec)
        cost = usage_cost(rec)
        append_jsonl(out, {
            "id": row["id"], "ok": bool(answers), "adapter": "jev", "model": args.model,
            "latency_ms": rec.get("latency_ms"), "usage": (rec.get("resp") or {}).get("usage"),
            "answers": answers, "error": None if answers else rec.get("err"),
            "ts": utc_now(),
        })
        spent += cost
        if not answers:
            failed += 1
        if i % 25 == 0 or i == len(pending):
            print("  %d/%d  spent ~$%.4f  failed %d" % (i, len(pending), spent, failed),
                  file=sys.stderr, flush=True)
        if spent and spent > args.max_usd:
            print("budget cap reached (%.4f > %.4f) -- stopping" % (spent, args.max_usd),
                  file=sys.stderr)
            break
        if args.sleep:
            time.sleep(args.sleep)
    summary = {"rows": len(rows), "pending": len(pending), "failed": failed,
               "spent_usd": round(spent, 5), "finished_at": utc_now()}
    write_meta(out, {**(json.load(open(out + ".meta.json", encoding="utf-8")) if
                      os.path.exists(out + ".meta.json") else {}), "summary": summary})
    print(json.dumps(summary, indent=1))
    return 0 if failed <= max(1, len(pending) // 20) else 1


def mode_report(args) -> int:
    rows = load_dataset(args.data)
    out_dir = args.out or DEFAULT_OUT
    files = sorted(f for f in os.listdir(out_dir)
                   if f.startswith("results_") and f.endswith(".jsonl"))
    if args.results:
        files = [os.path.basename(p) for p in args.results]
    if not files:
        print("no results_*.jsonl in %s" % out_dir, file=sys.stderr)
        return 1
    report = {"generated_at": utc_now(), "dataset": DATASET,
              "fingerprint": dataset_fingerprint(rows), "models": []}
    for name in files:
        report["models"].append(evaluate(os.path.join(out_dir, name), rows))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    md = render_markdown(report)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    return 0


def render_markdown(report: dict) -> str:
    cols = ["n", "accuracy", "soft_acc", "brier", "tv", "ece", "score_mae",
            "within_1_level", "latency_ms_mean", "latency_ms_p50", "cost_usd_total"]
    lines = ["# decision-stand report", "",
             "Generated %s; dataset %s (fingerprint %s)."
             % (report["generated_at"], report["dataset"], report["fingerprint"]), "",
             "| model | " + " | ".join(cols) + " |",
             "|" + "---|" * (len(cols) + 1)]
    for model in report["models"]:
        o = model["overall"]
        cells = [("%s" % o.get(c, "")) if o.get(c) is not None else "" for c in cols]
        lines.append("| %s | %s |" % (model["results_file"], " | ".join(cells)))
    for model in report["models"]:
        lines += ["", "## %s" % model["results_file"],
                  "", "| workflow | n | accuracy | soft_acc | brier | ece |",
                  "|---|---|---|---|---|---|"]
        for wf, m in model["by_workflow"].items():
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (wf, m.get("n"), m.get("accuracy"), m.get("soft_acc"),
                            m.get("brier"), m.get("ece")))
        lines += ["", "| question type | n | accuracy | soft_acc | brier | ece | score_mae |",
                  "|---|---|---|---|---|---|---|"]
        for qt, m in model["by_question_type"].items():
            lines.append("| %s | %s | %s | %s | %s | %s | %s |"
                         % (qt, m.get("n"), m.get("accuracy"), m.get("soft_acc"),
                            m.get("brier"), m.get("ece"), m.get("score_mae", "")))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compare decision engines on LocalLLaMA/typed-decisions")
    p.add_argument("--mode", required=True,
                   choices=["fetch", "estimate", "run-jev", "report"])
    p.add_argument("--data", default=DEFAULT_DATA, help="dataset json (mode: fetch/estimate/run-jev/report)")
    p.add_argument("--out", default=None, help="results dir or file (default: tools/decision-stand/out)")
    p.add_argument("--results", nargs="*", default=None, help="explicit results files for report mode")
    p.add_argument("--limit", type=int, default=None, help="cap the number of cases")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--max-usd", type=float, default=0.5)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--sleep", type=float, default=0.0, help="pause between calls (seconds)")
    p.add_argument("--env-file", default=None, help="file with OPENROUTER_API_KEY=... (chmod 600)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "fetch":
        return mode_fetch(args)
    if args.mode == "estimate":
        return mode_estimate(args)
    if args.mode == "run-jev":
        return mode_run_jev(args)
    if args.mode == "report":
        return mode_report(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
