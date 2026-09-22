#!/usr/bin/env python3
"""run_laya — Laya leg of the decision-stand.

Runs one Laya checkpoint over the stand's dataset (fetched by
``decision_stand.py --mode fetch``) and appends JSONL records in the same
schema, so ``decision_stand.py --mode report`` scores Jev and Laya with one
set of metrics.

Requires an environment with ``laya`` + torch/transformers (this file is
deliberately separate from the stdlib tool):

    uv venv /tmp/laya-venv --python 3.12
    uv pip install --python /tmp/laya-venv/bin/python "laya==0.3.4"
    /tmp/laya-venv/bin/python tools/decision-stand/run_laya.py \
        --checkpoint multilingual --out out/results_laya_multilingual.jsonl

Checkpoints: english (ModernBERT-large), multilingual (mmBERT-base),
typed-decisions (fine-tuned on this benchmark's train split), or
``routed`` (the package's Router, auto-detection off -- the honest
zero-shot routing behaviour).

Records are appended incrementally; re-running skips ids already done.
A ``<out>.meta.json`` sidecar records the run parameters. No secrets here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "data", "dataset.json")
DEFAULT_OUT = os.path.join(HERE, "out")

CHECKPOINTS = ("english", "multilingual", "typed-decisions", "routed")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_jsonl(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Laya leg of decision-stand")
    p.add_argument("--checkpoint", required=True, choices=CHECKPOINTS)
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--device", default="cpu")
    p.add_argument("--laya-path", default=None,
                   help="optional path to a laya source checkout (added to sys.path)")
    args = p.parse_args(argv)

    if args.laya_path:
        sys.path.insert(0, args.laya_path)
    os.environ.setdefault("USE_TF", "0")
    os.environ.setdefault("USE_TORCH", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    import laya  # noqa: E402  (import after sys.path tweak)
    from laya.router import Router  # noqa: E402

    with open(args.data, encoding="utf-8") as fh:
        rows = json.load(fh)["rows"]
    if args.limit:
        rows = rows[:args.limit]
    out = args.out or os.path.join(DEFAULT_OUT, "results_laya_%s.jsonl" % args.checkpoint)
    done = {r.get("id") for r in read_jsonl(out) if r.get("id") and r.get("ok")}
    pending = [r for r in rows if r["id"] not in done]
    print("run_laya[%s]: %d rows, %d done, %d pending (laya %s)"
          % (args.checkpoint, len(rows), len(done), len(pending), getattr(laya, "__version__", "?")),
          file=sys.stderr, flush=True)

    t0 = time.time()
    if args.checkpoint == "routed":
        # auto_task_detection off: routing never picks typed-decisions on its
        # own, so english + multilingual are the full routed behaviour here.
        router = Router(standalone_repos=True, device=args.device,
                        preload=["english", "multilingual"], auto_task_detection=False)
    else:
        router = Router(standalone_repos=True, device=args.device,
                        preload=[args.checkpoint])
    print("loaded in %.1fs" % (time.time() - t0), file=sys.stderr, flush=True)

    meta_path = out + ".meta.json"
    if not os.path.exists(meta_path):
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"adapter": "laya", "checkpoint": args.checkpoint,
                       "device": args.device, "dataset": os.path.basename(args.data),
                       "started_at": utc_now(), "laya_version": getattr(laya, "__version__", None)},
                      fh, ensure_ascii=False, indent=1)

    failed = 0
    for i, row in enumerate(pending, 1):
        started = time.time()
        record = {"id": row["id"], "ok": False, "adapter": "laya",
                  "model": args.checkpoint, "ts": utc_now()}
        try:
            kwargs = {} if args.checkpoint == "routed" else {"model": args.checkpoint}
            res = router.predict(row["state"], row["questions"], **kwargs)
            record["answers"] = res.get("answers")
            record["routing"] = res.get("routing")
            record["ok"] = bool(res.get("answers"))
        except Exception as exc:  # noqa: BLE001 - record and move on
            record["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            failed += 1
        record["latency_ms"] = int((time.time() - started) * 1000)
        append_jsonl(out, record)
        if i % 25 == 0 or i == len(pending):
            print("  %d/%d  failed %d" % (i, len(pending), failed), file=sys.stderr, flush=True)

    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    meta["summary"] = {"rows": len(rows), "pending": len(pending), "failed": failed,
                       "finished_at": utc_now()}
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    return 0 if failed <= max(1, len(pending) // 20) else 1


if __name__ == "__main__":
    sys.exit(main())
