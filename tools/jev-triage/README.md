# jev-triage

Read-only, report-only triage for gbrain hygiene: duplicate/stale page pairs and
stub/empty-template "prune signals". The tool **never** writes, deletes or merges
anything — it produces a ranked JSON + Markdown queue for a human to review.

```
difflib candidates (>= 0.60) → Jev same/action verdict (RU+EN question pairs,
layer conventions in the criteria) → ranked report queue (merge / keep / review)
+ a stub/prune mode with hard doctrinal exclusions
```

## Guarantees

- **Read-only.** Every SQL statement is a `SELECT`. `test_jev_triage.py::SqlGuardTest`
  fails the build on any DML/DDL keyword in a `SQL_*` literal.
- **No secrets.** The API key is read from `OPENROUTER_API_KEY` only, never logged,
  printed or written to the report. The model is pinned to
  `typesafe/jev-1.13-20260917` (override only with `--model`).
- **Degrades gracefully.** Missing key / missing `GBRAIN_DATABASE_URL` / missing
  `psql` / HTTP 5xx → a skip note and **exit code 0**.
- **No real gbrain text in the repo.** Offline fixtures under `fixtures/` are
  synthetic; the real texts stay on the host (see the 2026-09-19 pilot).

## Live run (operator on the tw host)

Prerequisites: `psql`, `GBRAIN_DATABASE_URL` (read-only role), `OPENROUTER_API_KEY`.

```bash
# Mode A — duplicate/stale pairs
python3 tools/jev-triage/jev_triage.py --mode pairs --json --out /tmp/jev-report

# Mode B — stub / empty-template prune signals (hard-excludes code-index + agent-floor)
python3 tools/jev-triage/jev_triage.py --mode prune --json --out /tmp/jev-report

# Estimate only (no Jev calls, no spend)
python3 tools/jev-triage/jev_triage.py --mode pairs --print-estimate-only --json
```

`--json` prints the machine report to stdout; `--out DIR` also writes
`jev_triage_pairs.{json,md}` (or `_prune`). The estimate is always printed to
stderr before any Jev call. Default call cap `--max-calls 600`; the estimate uses
the pilot's worst observed upper bound (`$0.00008/call`), so a full run of
~500 pages + pairs stays under the `$0.05` budget.

After a live run the tool re-checks the read-only invariant with:

```sql
SELECT count(*) FILTER (WHERE updated_at > :'since'::timestamptz) FROM pages
```

and records the count under `read_only_verification` (it must be `0`).

## Offline replay (no DB, no key)

```bash
# Pairs: cross-check the tool's synthesis against the pilot labels
python3 tools/jev-triage/jev_triage.py --mode pairs --replay \
  --results /opt/hermes/work/typesafe-pilot/results_pairs.jsonl \
  --jobs    /opt/hermes/work/typesafe-pilot/jobs_pairs.jsonl \
  --labels  /opt/hermes/work/typesafe-pilot/labels_pairs.json --json

# Prune: code-index pages must not appear in the prune signals
python3 tools/jev-triage/jev_triage.py --mode prune --replay \
  --results /opt/hermes/work/typesafe-pilot/results_pages.jsonl \
  --jobs    /opt/hermes/work/typesafe-pilot/jobs_pages.jsonl \
  --labels  /opt/hermes/work/typesafe-pilot/labels_pages.json --json
```

`--replay` only reads the recorded `results.jsonl` answers; `--jobs` adds the page
texts (and enables deterministic identical-text detection), `--labels` adds the
ground-truth cross-check block.

## Decision semantics

| Situation | Decision | Block |
|---|---|---|
| Identical normalized text under two slugs | `merge` | merge (no Jev call) |
| Both RU/EN `action=merge` and `same_entity=true` | `merge` | merge |
| Both RU/EN `action=keep` | `keep` | keep |
| RU/EN divergence (same or action) | `review` | review |
| Weak noul signal (0.35–0.65) | `review` | review |
| Anything else ambiguous | `review` | review |

Layer conventions baked into the Jev criteria: repository vs service vs project
facets are **not** duplicates; same-name pages from different layers (canonical
vs proposed vs agent-floor) are **not** duplicates when the roles differ; a stale
fragment of a removed contour without a deletion marker **is** a stale version
(merge).

### Hard prune exclusions (never emit a prune signal)

- `page_kind = 'code'` — code-index pages are a navigation layer, not junk.
- `source_id = 'rahermes'` — agent-floor notes are not canon, but are not pruned.

Real probes and empty templates are **not** excluded and flow to Jev.

## Tests

```bash
python3 -m unittest discover -s tools/jev-triage -p 'test_*.py' -v
```

30 offline tests (no network, no DB): decision paths, candidate generation,
prune exclusions, synthetic replay, SQL guard, secret/skip-note handling, mocked
HTTP 5xx, and budget. `ReplayPilotTest` additionally runs against the recorded
pilot answers when `/opt/hermes/work/typesafe-pilot/` is present and skips
otherwise.

## Sources used

- `AGENTMAP.md` — **not present** in the repo root (checked), so no pointer to use.
- gbrain MCP `code_def` / `code_callers` — **not available in this runner** (no MCP
  tool surface). Orientation instead came from `CLAUDE.md`, the task's
  `docs/tasks/TASK-gbrain-jev-triage/spec.md` (absent in the repo; read from the
  `Rast53/raclaw-tasks` task packet), `src/schema.sql` (`pages` columns), and the
  recorded pilot artifacts at `/opt/hermes/work/typesafe-pilot/`.
