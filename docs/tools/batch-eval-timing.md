# batch_eval_timing.py

A read-only diagnostic for **Adobe Experience Platform (AEP) batch audience
evaluation**. It answers the questions people actually ask when a batch audience
"isn't there yet" or "came back empty":

- How long does batch evaluation really take in this sandbox?
- Was **this** audience evaluated, or is it just new and waiting?
- Is the estate actually on the daily schedule, or something else?
- Did a specific scheduled run **actually** evaluate a given set of audiences?
- Who/what is burning the on-demand evaluation quota?

It is **read-only** — it never creates, edits, or deletes anything in AEP — and
uses the **Python standard library only** (no `pip install`), so it runs on a
locked-down/VDI machine.

---

## Setup

1. Drop a credential JSON into `./creds/` (same bank the other tools in this repo
   use). Each file needs at least:

   ```json
   {
     "client_id": "…",
     "client_secret": "…",
     "org_id": "…@AdobeOrg",
     "sandbox": "prod"
   }
   ```

2. Run it. On Windows use the `py` launcher:

   ```bash
   py -3 batch_eval_timing.py                 # interactive credential menu, dev sandbox
   py -3 batch_eval_timing.py prod            # pick creds/prod.json by filename stem
   py -3 batch_eval_timing.py "my creds" --sandbox=prod
   ```

Outputs are written to `./output/` (CSV). That folder is git-ignored — it holds
live tenant data and must not be committed.

---

## Modes

### 1. Estate report (default)

```bash
py -3 batch_eval_timing.py prod
py -3 batch_eval_timing.py prod --all-methods   # include streaming/edge, not just batch
py -3 batch_eval_timing.py prod --jobs=50        # cap to 50 jobs (default: all)
```

Lists every audience with its evaluation method (batch / streaming / edge),
summarises the audience-creation rate per month, then pages **all** batch segment
jobs and reports how long each evaluation took (min / median / avg / max plus a
duration histogram — the direct answer to "why is batch slow?").

Each job row resolves the **audience name(s)** it evaluated (via
`/segment/definitions`, so even system segments are named), and shows **who
triggered it** and **when** (UTC + BST). Everything is exported to
`output/batch_eval_timing_<sandbox>_<stamp>.csv` with these columns:

`job_id, status, audience_names, segment_ids, schedule_id, source, created_by,
triggered_utc, scheduled_utc, ended_utc, duration_seconds, duration_human,
num_segments`

### 2. Single-audience probe — "is this one stuck, or just new?"

```bash
py -3 batch_eval_timing.py prod --audience                       # filter-and-pick menu
py -3 batch_eval_timing.py prod --audience=<audienceId>
py -3 batch_eval_timing.py prod --audience="MY_AUDIENCE_NAME"
```

Prints a **timing card** for one audience: created / last-modified time, current
profile count and when that count last refreshed, the last batch job in the
sandbox, its **feeders** (the dependency segments it is built on — each with
method, count, and last-evaluated, flagged `EMPTY/STALE` when it can't populate
the parent), and a plain **stuck / not-stuck verdict**. A dependent audience can
only be as fresh and full as its feeders, so a feeder sitting at 0 is the first
place to look.

### 3. Schedule config — "is the estate really on the 4am schedule?"

```bash
py -3 batch_eval_timing.py prod --schedules
```

Dumps the sandbox's scheduled-segmentation config (`/config/schedules`): each
schedule's state, cron/trigger time, and — for the `batch_segmentation` entry —
whether it targets **all** segments (`*`) or a specific list. This is the direct
test of "is the daily run configured to cover everything?". Writes
`output/schedules_<sandbox>_<stamp>.csv`.

### 4. Verify-run — "did *this* job actually evaluate these audiences?"

```bash
py -3 batch_eval_timing.py prod --verify-run --date=2026-07-01 --ids=id1,id2
py -3 batch_eval_timing.py prod --verify-run --job=<jobId> --ids-file=ids.txt
```

Given a job id (or a date, which finds that day's scheduler run) and a set of
audience ids, reports **PRESENT / ABSENT** per audience **and the profile count
that job computed** for each.

> **Why this is needed:** a job's `segments[]` is *not* the list of what it
> evaluated — the daily scheduler job carries just a single trigger entry there
> while actually evaluating the whole estate. The authoritative manifest is
> `metrics.segmentedProfileCounter` (1600+ segments for the daily run). This
> settles "did the 04:00 scheduled run evaluate it, or only a later run?": if an
> audience is **present with a computed count** in the scheduled run, evaluation
> happened then — and a later change to the displayed count is a **metric/display
> lag, not an evaluation lag**.

Writes `output/verify_run_<sandbox>_<stamp>.csv`.

### 5. FAE audit — "who is burning the on-demand evaluation quota?"

```bash
py -3 batch_eval_timing.py prod --fae-audit                       # year-to-date
py -3 batch_eval_timing.py prod --fae-audit --from=2026-01-01 --to=2026-07-01
```

Inventories **Flexible Audience Evaluation** runs — the on-demand / API-triggered
evaluations (`source=api`), as opposed to the daily scheduler run. Per run it
shows the triggered time (UTC + BST), the audience(s) evaluated (ids resolved to
names), and the run-of-day number; then tallies **per-day consumption vs the
2/day/sandbox cap** and **year-to-date vs the 50/year prod cap**. Writes
`output/fae_audit_<sandbox>_<stamp>.csv`.

> **Known limitation (flagged in the output):** neither `/segment/jobs` nor the
> AEP Audit API records the **user** who triggered a run, so true per-user
> attribution is not possible from available data. Consumption is reported by
> **day and run**; the `created_by` field reflects only the job's source
> (`system (scheduler)` vs `api/FAE (user not recorded)`).

---

## Key concepts (things that trip people up)

- **`segments[]` is not the manifest.** Use `metrics.segmentedProfileCounter` to
  know what a job really evaluated. `--verify-run` does this for you.
- **Evaluation time ≠ displayed-count time.** An audience can be fully evaluated
  by the early scheduled run yet not show its refreshed count until hours later.
  That's a metric/display lag, not a scheduling problem.
- **Feeders are hidden.** An audience may depend on another audience, or on an
  externally-published (e.g. CJA) audience whose membership lands on its own
  schedule, or on a reference/lookup dataset. If a dependent is empty or stale,
  check the feeder first (`--audience`).
- **Empty ≠ late.** An audience that evaluated on time but returns 0 is a
  definition/data problem (bad code, unpopulated lookup dataset), not a timing
  one.

---

## Command reference

| Flag | Meaning |
|------|---------|
| `<stem>` | Credential file stem in `./creds/` (positional) |
| `--sandbox=<name>` | Override the sandbox (default: from creds, else `dev`) |
| `--jobs=<N>` \| `--jobs=all` | Cap the number of segment jobs paged (default: all) |
| `--all-methods` | Show all evaluation methods, not just batch |
| `--audience[=<id\|name>]` | Single-audience timing card |
| `--schedules` | Dump the scheduled-segmentation config |
| `--verify-run` | Prove a job evaluated a set of audiences |
| `--job=<id>` / `--date=YYYY-MM-DD` | Job selector for `--verify-run` |
| `--ids=<a,b,c>` / `--ids-file=<path>` | Audience ids for `--verify-run` |
| `--fae-audit` | Inventory on-demand (FAE) runs vs quota |
| `--from=YYYY-MM-DD` / `--to=YYYY-MM-DD` | Window for `--fae-audit` (default: YTD) |

All modes are read-only. All timestamps are shown in UTC (with BST where a local
morning check matters).
