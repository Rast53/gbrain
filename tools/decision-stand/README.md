# decision-stand

One stand, one dataset, one set of metrics — so decision engines
("System 1" models that return typed probabilities instead of text) are
compared on the same decisions rather than on vendor tables.

Motivation: TypeSafe Jev is our auxiliary judgement layer (spec-lint,
gbrain triage), and in September 2026 an OSS alternative — Laya
(`NandhaKishorM/laya`, Apache-2.0, `pip install laya`) — shipped with a
headline "0.766 vs Jev 0.727" that turned out to compare a *specialist*
fine-tuned on this benchmark's train split against Jev *zero-shot*. That
task (`TASK-research-laya-decision-stand`) needs our own numbers on one
stand: this tool.

## Benchmark

`LocalLLaMA/typed-decisions` (HF, Apache-2.0) — 400 test cases x 5 typed
questions = 2 000 decisions over `noul` / `choice` / `score` primitives,
four workflows (agent trace observability, customer service, invoice
processing, security incidents).

Gold is the mean of three samples from a ~4B-class teacher endpoint: it
measures **agreement with that teacher, not correctness**. The dataset card
is explicit — read 0.52 as the floor (prior), ~0.75 as saturation (teacher
self-agreement is 0.735). A score much above 0.75 means the model learned
the teacher's quirks. All model scores below are on this gold.

## Layout

| file | what |
|---|---|
| `decision_stand.py` | the stdlib harness: `fetch` / `estimate` / `run-jev` / `report` |
| `run_laya.py` | Laya leg (needs `laya` + torch in a venv; kept out of the stdlib tool) |
| `test_decision_stand.py` | offline tests — `python3 -m unittest discover -s tools/decision-stand -p 'test_*.py' -v` |
| `fixtures/dataset_sample.json` | tiny synthetic dataset for the tests |
| `data/dataset.json` | the fetched test split (fingerprint in the meta of every run) |
| `out/` | one `results_<adapter>.jsonl` per run + `<file>.meta.json` sidecars + `report.{json,md}` |

## Run it

```bash
# 1. dataset (no token needed; HF datasets-server API)
python3 tools/decision-stand/decision_stand.py --mode fetch

# 2. what a Jev pass would cost (no calls, no spend)
python3 tools/decision-stand/decision_stand.py --mode estimate

# 3. Jev: OpenRouter alpha decisions, pinned model
OPENROUTER_API_KEY=... python3 tools/decision-stand/decision_stand.py \
    --mode run-jev --out tools/decision-stand/out/results_jev.jsonl

# 4. Laya checkpoints (own venv)
uv venv /tmp/laya-venv --python 3.12
uv pip install --python /tmp/laya-venv/bin/python "laya==0.3.4"
/tmp/laya-venv/bin/python tools/decision-stand/run_laya.py \
    --checkpoint multilingual --out tools/decision-stand/out/results_laya_multilingual.jsonl
# ... english | typed-decisions | routed

# 5. one report across every results_*.jsonl in out/
python3 tools/decision-stand/decision_stand.py --mode report
```

## Pins and conventions

- Jev is pinned to `typesafe/jev-1.13-20260917` (`--model` overrides; aliases move).
  The rate used for estimates is the published $0.042 per 1M input tokens.
- Laya runs at the release version installed (`laya==0.3.4` for the 2026-09-22 run).
- The key is read from `OPENROUTER_API_KEY` or `--env-file`; it is never logged,
  printed, or written into any artifact.
- Results are appended record by record, so an interrupted run resumes by
  skipping ids already recorded; `--limit`/`--max-usd` bound a pass up front.
- Every results file has a `.meta.json` sidecar (dataset fingerprint, model,
  endpoint, run args) for reproducibility.
- Read-only with respect to gbrain: nothing here writes pages or the database.

## Metrics

| metric | definition |
|---|---|
| accuracy | argmax of the model distribution == gold argmax |
| soft_acc | mean model probability on the gold argmax |
| Brier | sum of squared differences over the aligned support |
| TV | total variation distance to the gold distribution |
| ECE | top-label calibration, 15 equal bins (correctness = argmax match). Gold is soft, so even a gold-matching model shows non-zero ECE |
| score_mae, within_1_level | `score` questions only: |expected - gold expected|, and |round - round| <= 1 |
| latency_ms | per-case wall time as recorded by the runner (CPU class differs from GPU class — compare within a run, not across) |
| cost_usd_total / per_case | from the API `usage.cost` when the provider reports it |

## Reading the numbers

- Jev is a **generalist** baseline here: one call, arbitrary schema, no training
  on these workflows. `typed-decisions` is a **specialist**: fine-tuned on the
  train split of the same four workflows. The dataset card is explicit that the
  two are not comparable as a quality ranking — read the gap as the price of
  generality.
- Laya base checkpoints are near chance on this benchmark zero-shot; that is
  what the stand should reproduce before anything is concluded from anything.
- Single host, single run: treat differences under ~0.01 as noise.
