#!/usr/bin/env python3
"""
ingestion/luma_demo_sandbox_builder_and_event_feeder.py
=======================================================
Question it answers: does this demo sandbox hold Adobe's Luma sample data, and
is that data fresh enough to look like a live business?

Builds Adobe's Luma demo in one AEP sandbox, then keeps it alive:

  setup   the Luma structure: 3 identity namespaces, a data type, 2 classes,
          4 field groups, 7 schemas, 13 descriptors, 7 datasets -- exactly the
          requests in Adobe's Postman collections (ported into
          ingestion/luma_demo_sandbox/specs/).
  load    the 7 Luma files (6,104 rows) by batch ingestion, tenant id swapped
          in, event timestamps moved up to now (UTC), fresh event _ids.
  feed    N synthetic events (web, offline purchase, inventory) on the loaded
          profiles, streamed to an HTTP API inlet. Schedule it hourly.

WRITER -- read-only by default. Without --apply every command only reads from
Adobe and prints exactly what it would create or send. With --apply:
  setup  creates the missing namespaces, schema registry objects and datasets
  load   ingests 7 batches;  feed  ingests N events.
Demo sandboxes only: the credential must be listed in config.json under
"luma_demo_sandbox": {"allowed_credentials": [...]} (and its org in
"allowed_org_ids", if that is set), or the tool refuses to start. If
"allowed_sandboxes" is set, any other sandbox is refused too.

Writes locally: output/luma_demo_sandbox_state_<credential>_<sandbox>.json
(the ids setup found or created, and the tenant id).

Usage (credential name first, then --sandbox=):
    python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> setup --sandbox=<name>
    python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> setup --sandbox=<name> --apply
    python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> load  --sandbox=<name> --apply
    python ingestion/luma_demo_sandbox_builder_and_event_feeder.py <credential> feed  --sandbox=<name> --n=20 --apply

Guide: docs/tools/luma_demo_sandbox_builder_and_event_feeder.md
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo root on the path, so toolkit_internals/ imports when run as python <folder>/<tool>.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from toolkit_internals import credential_loader  # noqa: E402
from ingestion.luma_demo_sandbox import batch, datasets, stream, synth  # noqa: E402
from ingestion.luma_demo_sandbox.client import ApiError, Platform  # noqa: E402
from ingestion.luma_demo_sandbox.registry import load_steps, run_setup  # noqa: E402
from ingestion.luma_demo_sandbox.transform import prepare, to_json_file  # noqa: E402

SCRIPT_NAME = "luma_demo_sandbox_builder_and_event_feeder"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-11-02"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"


class Refused(RuntimeError):
    """The run cannot go ahead; the message says why."""


# ----------------------------------------------------------------------------
# Safety and context
# ----------------------------------------------------------------------------
def check_allowed(credential: str, conf: dict[str, str], settings: dict) -> None:
    """This tool writes demo data, so it only runs against credentials listed as demo ones."""
    allowed = settings.get("allowed_credentials") or []
    if credential not in allowed:
        raise Refused(
            f"credential '{credential}' is not a demo credential. List it in config.json under "
            f'"{datasets.CONFIG_KEY}": {{"allowed_credentials": [...]}} -- demo orgs only.')
    orgs = settings.get("allowed_org_ids") or []
    if orgs and conf.get("org_id") not in orgs:
        raise Refused(f"credential '{credential}' belongs to an org not in allowed_org_ids.")


def check_sandbox(sandbox: str, settings: dict) -> None:
    """If config.json pins the sandboxes, refuse every other one."""
    allowed = settings.get("allowed_sandboxes") or []
    if allowed and sandbox not in allowed:
        raise Refused(f"sandbox '{sandbox}' is not in allowed_sandboxes ({', '.join(allowed)}).")


def keep_awake(on: bool) -> None:
    """Hold the PC awake for the run (house rule 9); released however the run ends."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            es_continuous | (es_system_required if on else 0))
    except Exception:
        pass


def banner(conf: dict[str, str], credential: str, sandbox: str, opts: argparse.Namespace) -> None:
    bar = "=" * 72
    mode = "WRITES (--apply)" if opts.apply else "read-only (add --apply to write)"
    print(bar)
    print(f"  {SCRIPT_NAME} v{SCRIPT_VERSION}   ({SCRIPT_DATE})")
    print(f"  by {SCRIPT_AUTHOR}")
    print(f"  Luma demo sandbox: {opts.command}, {mode}")
    print(f"  Credential: {credential}   Org: {conf['org_id']}   Sandbox: {sandbox}")
    print(bar)


def resolve_tenant(api: Platform, state: dict) -> str:
    """Tenant id from state, else one Schema Registry read (cached in the state file)."""
    tenant = state.get("tenant_id") or api.get_json("/data/foundation/schemaregistry/stats")["tenantId"]
    state["tenant_id"] = tenant.lstrip("_")
    return datasets.normalise_tenant(tenant)


def ids_for(ds: datasets.Dataset, state: dict) -> tuple[str, str]:
    known = state.get("vars", {})
    return known.get(ds.dataset_var, ""), known.get(ds.schema_var, "")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------
# setup
# ----------------------------------------------------------------------------
def cmd_setup(api: Platform, opts: argparse.Namespace, state: dict) -> int:
    steps = len(load_steps())
    print(f"  Estimate: {steps} lookups" + (f", up to {steps} creates" if opts.apply else "")
          + " -- under a minute.\n")
    variables = {**state.get("vars", {}), "TENANT_ID": resolve_tenant(api, state)}
    variables, counts = run_setup(api, variables, apply=opts.apply, verbose=opts.verbose)
    state["vars"] = {k: v for k, v in variables.items() if k != "TENANT_ID"}
    state["datasets"] = {ds.key: state["vars"].get(ds.dataset_var) for ds in datasets.DATASETS}
    print(f"\n  {counts['exists']} exist, {counts['created']} created, {counts['missing']} missing")
    if counts["missing"] and not opts.apply:
        print(f"  Read-only: nothing was created. Add --apply to create the {counts['missing']} missing.")
    return 0


# ----------------------------------------------------------------------------
# load
# ----------------------------------------------------------------------------
def report_batches(api: Platform, started: dict[str, str], interval: float) -> bool:
    print("\n  Waiting for Catalog (polls every %.0fs) ..." % interval)
    all_ok = True
    for key, batch_id in started.items():
        info = batch.wait_for(api, batch_id, interval=interval)
        ok = info["status"] in batch.DONE_OK
        all_ok &= ok
        print(f"  {key:<18} {batch_id}  {info['status']}")
        if not ok:
            for reason in batch.failure_reasons(info):
                print(f"      {reason}")
    return all_ok


def cmd_load(api: Platform, opts: argparse.Namespace, state: dict) -> int:
    targets = [datasets.BY_KEY[opts.only]] if opts.only else datasets.DATASETS
    if opts.apply:
        print(f"  Estimate: {len(targets) * 3} ingestion calls, then Catalog polls -- "
              f"Adobe usually takes 5-15 minutes to process the batches.\n")
    tenant = resolve_tenant(api, state)
    now = now_utc()
    missing = [ds.key for ds in targets if not ids_for(ds, state)[0]]
    if missing and opts.apply:
        raise Refused(f"no dataset id for {', '.join(missing)} -- run setup --apply first.")
    started: dict[str, str] = {}
    for number, ds in enumerate(targets, 1):
        rows = prepare(datasets.read_rows(ds), tenant_id=tenant, is_event=ds.is_event, now=now,
                       window_days=opts.window_days or None)
        dataset_id = ids_for(ds, state)[0] or "<not created yet: run setup --apply>"
        span = ""
        if ds.is_event:
            stamps = sorted(r["timestamp"] for r in rows)
            span = f"   events {stamps[0]} .. {stamps[-1]} (UTC)"
        print(f"  batch {number}/{len(targets)}  {ds.key:<18} {len(rows):>5} rows -> {dataset_id}{span}")
        if not opts.apply:
            continue
        started[ds.key] = batch.ingest(api, dataset_id, ds.file, to_json_file(rows))
    if not opts.apply:
        print(f"\n  Read-only: {len(targets)} batch(es) planned, nothing sent. Add --apply to ingest.")
        return 0
    ok = report_batches(api, started, opts.poll_interval)
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# feed
# ----------------------------------------------------------------------------
def inlet_for(key: str, opts: argparse.Namespace, settings: dict) -> str:
    return (opts.inlet or (settings.get("streaming_inlet_urls") or {}).get(key)
            or settings.get("streaming_inlet_url") or "")


def cmd_feed(api: Platform, opts: argparse.Namespace, state: dict, settings: dict) -> int:
    tenant = resolve_tenant(api, state)
    mix = {"web-events": opts.web, "offline-purchases": opts.offline, "inventory-events": opts.inventory}
    events = synth.generate(opts.n, pools=synth.load_pools(), tenant_id=tenant, now=now_utc(),
                            window_minutes=opts.window_minutes, mix=mix, rng=random.Random(opts.seed))
    events = {key: rows for key, rows in events.items() if rows}
    ids = {key: ids_for(datasets.BY_KEY[key], state) for key in events}
    missing = [key for key, (dataset_id, schema_id) in ids.items() if not dataset_id or not schema_id]
    if missing and opts.apply:
        raise Refused(f"no dataset/schema id for {', '.join(missing)} -- run setup --apply first.")
    counts = ", ".join(f"{key} {len(rows)}" for key, rows in events.items())
    started_at = time.monotonic()

    if opts.batch:
        for key, (dataset_id, _) in ids.items():
            print(f"  {len(events[key])} x {key} -> batch into {dataset_id or '<run setup --apply>'}")
        if not opts.apply:
            print(f"\n  Read-only: {opts.n} events ({counts}) planned, nothing sent.")
            return 0
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        started = {key: batch.ingest(api, dataset_id, f"feed-{key}-{stamp}.json", to_json_file(events[key]))
                   for key, (dataset_id, _) in ids.items()}
        ok = report_batches(api, started, opts.poll_interval)
        print(f"feed: {opts.n} events ({counts}) via batch in {time.monotonic() - started_at:.0f}s"
              f" -- {'all batches success' if ok else 'BATCH FAILURE'}")
        return 0 if ok else 1

    inlets = {key: inlet_for(key, opts, settings) for key in events}
    if opts.apply and not all(inlets.values()):
        raise Refused('no streaming inlet: pass --inlet=<url>, set "streaming_inlet_url" in config.json, '
                      "or use --batch.")
    messages = {key: [stream.envelope(e, schema_id=ids[key][1] or "<schema id>",
                                      dataset_id=ids[key][0] or "<dataset id>", org_id=api.conf["org_id"])
                      for e in rows] for key, rows in events.items()}
    if not opts.apply:
        for key, batch_of in messages.items():
            print(f"  {len(batch_of)} x {key} -> {inlets[key] or '<streaming inlet>'}")
            print("      " + json.dumps(batch_of[0], indent=2).replace("\n", "\n      "))
        print(f"\n  Read-only: {opts.n} events ({counts}) planned, nothing sent. Add --apply to send.")
        return 0

    auth = api.auth_headers() if settings.get("streaming_authenticated") else None
    accepted, failures = 0, []
    for key, batch_of in messages.items():
        for message in batch_of:
            resp = stream.post(api.session, inlets[key], message, auth)
            if resp.ok:
                accepted += 1
            else:
                failures.append(f"{key} HTTP {resp.status_code}: {resp.text[:200]}")
    print(f"feed: {accepted}/{opts.n} accepted ({counts}) via streaming in "
          f"{time.monotonic() - started_at:.1f}s, {now_utc():%Y-%m-%d %H:%M} UTC"
          + (f" -- {len(failures)} failed, first: {failures[0]}" if failures else ""))
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=f"python ingestion/{SCRIPT_NAME}.py",
        description="Build Adobe's Luma demo in an AEP sandbox and keep it fed. Read-only without --apply.")
    p.add_argument("credential", help="credential name, as stored with the credential manager")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--sandbox", help="sandbox name (the Name column in Sandboxes > Browse)")
        sp.add_argument("--apply", action="store_true", help="actually write to Adobe")

    sp = sub.add_parser("setup", help="create the Luma structure")
    common(sp)
    sp.add_argument("--verbose", action="store_true", help="print the request body of every missing object")

    sp = sub.add_parser("load", help="batch-ingest the 7 Luma files")
    common(sp)
    sp.add_argument("--only", choices=list(datasets.BY_KEY), help="one dataset only")
    sp.add_argument("--window-days", type=float, default=30,
                    help="events end now; a file spanning longer is squeezed into this many days "
                         "(0 = never squeeze). Default 30.")
    sp.add_argument("--poll-interval", type=float, default=15, help="seconds between Catalog polls")

    sp = sub.add_parser("feed", help="synthetic events on the loaded profiles")
    common(sp)
    sp.add_argument("--n", type=int, default=50, help="events per run (default 50)")
    sp.add_argument("--window-minutes", type=float, default=60, help="timestamps in the last N minutes")
    sp.add_argument("--web", type=float, default=0.5, help="share of web events (default 0.5)")
    sp.add_argument("--offline", type=float, default=0.4, help="share of offline purchases (default 0.4)")
    sp.add_argument("--inventory", type=float, default=0.1, help="share of inventory events (default 0.1)")
    sp.add_argument("--inlet", help="streaming inlet URL (else config.json)")
    sp.add_argument("--batch", action="store_true", help="batch ingestion instead of streaming")
    sp.add_argument("--seed", type=int, help="repeatable events")
    sp.add_argument("--poll-interval", type=float, default=15, help="seconds between Catalog polls (--batch)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    print(credential_loader.source_banner(), file=sys.stderr)
    try:
        settings = datasets.load_settings()
        conf = credential_loader.load_creds(opts.credential)
        check_allowed(opts.credential, conf, settings)
        sandbox = opts.sandbox or ("" if conf.get("sandbox") in (None, "", "all") else conf["sandbox"])
        if not sandbox:
            raise Refused("no sandbox: pass --sandbox=<name>.")
        check_sandbox(sandbox, settings)
        banner(conf, opts.credential, sandbox, opts)
        api = Platform(conf, sandbox)
        state = datasets.load_state(opts.credential, sandbox)
        keep_awake(True)
        try:
            if opts.command == "setup":
                code = cmd_setup(api, opts, state)
            elif opts.command == "load":
                code = cmd_load(api, opts, state)
            else:
                code = cmd_feed(api, opts, state, settings)
        finally:
            keep_awake(False)
            state.update(org_id=conf["org_id"], sandbox=sandbox,
                         updated_utc=now_utc().isoformat(timespec="seconds"))
            path = datasets.save_state(opts.credential, sandbox, state)
        print(f"\n  State: {path.relative_to(datasets.REPO_ROOT)}")
        if opts.command == "load" or (opts.command == "feed" and opts.batch):
            print("\n  Endpoint health:")
            print("\n".join(api.health.report()))
        return code
    except (Refused, credential_loader.CredsError) as err:
        print(f"\n  Refused: {err}", file=sys.stderr)
        return 2
    except ApiError as err:
        print(f"\n  Adobe error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n  Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
