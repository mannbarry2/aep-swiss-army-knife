# Prompt: port the AEP 8am count board to Adobe App Builder (Project Firefly)

Paste everything below into the assistant in your App Builder repo, and attach
`sluggishness_detector.py` as the reference implementation.

---

You are porting a working Python CLI tool into an **Adobe App Builder (Project
Firefly)** app. I'm attaching the Python file **`sluggishness_detector.py`** — it
is the **reference implementation and source of truth for the logic**. Read it
first; your job is to reproduce its behaviour as an App Builder app, not to
redesign it.

**This repo already has a working "Hello World" App Builder app.** Build on that
existing scaffold, credentials, and deploy pipeline — do NOT re-scaffold a new
project. Before writing code, tell me what's already wired (actions, `app.config.yaml`,
`.env`, the Console project's credentials) and what you'll add.

## What the tool does (business context)

Marketing builds "audiences" (customer segments) in Adobe Experience Platform.
Batch audiences are recalculated by an overnight scheduled run. The team walks in
~8–9am needing to send an audience to Audience API, but the UI shows **0 / blank**
until ~10:14, so they can't sanity-check the volume. The tool surfaces each
recently-built audience's **real profile count at 8am**, hours before the UI does.

## The one critical insight (do NOT rediscover the hard way)

The count is not late, it's **hidden**. When the overnight segment job evaluates
an audience it writes that audience's **exact count into the job itself**, and the
job finishes ~07:00. A separate downstream telemetry export copies those counts
onto the audience record (what the UI reads) only at ~10:14, stamping the whole
estate at one instant. Therefore:

- **Read counts from the overnight job's manifest**, field
  `metrics.segmentedProfileCounter` (a map of `audienceId -> integer count`). This
  is available ~07:00 — before 8am. THIS is the whole trick.
- **Do NOT use the audience's own `metrics.updateEpoch`/count** — that's the late
  telemetry, identical for every audience, useless per-audience.
- **Do NOT use the on-demand estimate/preview API** (`/ups/preview` + `/ups/estimate`).
  It works for first-party PQL but **errors on segment-of-segments** ("missing/
  invalid dependencies"), which are exactly the audiences that matter. Confirmed
  dead end.

## Phase 1 — just stand up the core scheduled action (nothing else yet)

A scheduled App Builder action that runs **~08:00 daily** (after the ~07:00
overnight run) and returns the count board as JSON. Steps, all mirroring the
Python:

1. **Auth to AEP** using this project's **OAuth Server-to-Server** credential from
   the Console project (via `@adobe/aio-lib-ims` / the action's IMS context) — NOT
   hardcoded secrets, NOT the Python's keyring. The credential's `client_id` is the
   `x-api-key`; you'll also need the IMS org id and the AEP scopes.
2. **List all audiences** (paged) and keep **batch** ones (`evaluationInfo.batch.enabled === true`)
   **created in the last N days** (default 3; `createEpoch` is epoch-ms).
3. **Find the overnight run**: page `/segment/jobs`, take the most recent job with
   `source === "scheduler"` and status in `{SUCCEEDED, PROCESSED}`. Note its start
   (~04:00 trigger) and completion (~07:00).
4. **GET that job's full payload** and read `metrics.segmentedProfileCounter` → the
   `audienceId -> count` manifest.
5. **Classify** each candidate:
   - in the manifest → **READY**, `count = manifest[id]`
   - else built after the run's ~04:00 start → **TOO-NEW** (next cycle)
   - else → **MISSING** (built in time but not counted — genuine miss, no count exists)
   - (also flag **segment-of-segments**: audience detail `dependencies` array non-empty)
6. **Return structured JSON** (same shape as the Python's `--json`): generated time,
   overnight run id + completion, counts summary, and the per-audience list with
   name / id / count / status / is_sos.

### AEP API specifics (base `https://platform.adobe.io/data/core/ups`)

Headers on every call: `Authorization: Bearer <token>`, `x-api-key: <client_id>`,
`x-gw-ims-org-id: <orgId>`, `x-sandbox-name: prod`, `Accept: application/json`.

- List audiences: `GET /audiences?limit=100&start=<cursor>` — items under `children`,
  next cursor at `_page.next` (loop until absent).
- Segment jobs: `GET /segment/jobs?limit=100` — same pagination; each job has
  `source`, `status`, `creationTime`, and completion time fields.
- Full job (the manifest): `GET /segment/jobs/{jobId}` → `metrics.segmentedProfileCounter`.
  Can hold ~1800 entries — fine.
- Audience detail (SoS flag): `GET /audiences/{id}` → `dependencies` array.

The Python's `overnight_run()`, `assess_audience()`, and `classify()` are the exact
functions to port. Ignore the Python's `ssl._create_unverified_context()` — that's
a corporate-laptop workaround; use normal HTTPS in the cloud.

## Constraints

- **Read-only** — never create/edit/delete anything in AEP.
- Node.js (App Builder actions). Handle pagination. Clear logging. Return JSON.
- Success criterion (hard): the counts must be available by **08:00**. They are,
  because the overnight job finishes ~07:00 — but confirm the run completed before
  building the board, and if it hasn't, say so rather than reporting everything MISSING.

## Explicitly out of scope for now (later phases — stub or skip)

- The React Spectrum UI / dashboard.
- Notifications (email / Slack / Teams).
- We are ONLY trying to get the scheduled action producing correct JSON, deployed
  and invokable.

## Before you assume, ask me for

- The **IMS org id** and confirmation the **sandbox is `prod`**.
- Confirmation the **Console project's credential has AEP (Experience Platform)
  access** added to a product profile — the calls 403 without it.
- Whether to use the App Builder **cron/alarm** feed for the 08:00 trigger, or a
  different scheduler you already use.

## Deliverables

1. The action code (ported logic).
2. The schedule/trigger config for 08:00.
3. How credentials are wired (which Console credential, which scopes, where the
   env vars live).
4. Exact commands to deploy and test (`aio app deploy`, `aio app run`, or
   `aio rt action invoke`), and a sample of the JSON it returns.
