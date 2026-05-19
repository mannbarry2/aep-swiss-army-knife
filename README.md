![AEP Swiss Army Knife Banner](banner.png)

# AEP Swiss Army Knife

A small toolkit of single-file, stdlib-only command-line tools for working
with Adobe Experience Platform (AEP) from a locked-down VDI — no
`pip install` required. 
Every tool reads the same shared `config.json`
(copied from `config.example.json`), so credentials are configured once.

The toolkit spans two product ranges:

**Babelfish range** — Query Service template tooling:

| Tool | What it does |
|------|--------------|
| [`babelfish_query_renamer.py`](babelfish_query_renamer.py) | Tidies up messy Query Service SQL template names (optionally AI-suggested via Claude) and pushes renames back. |
| [`babelfish_query_fetcher.py`](babelfish_query_fetcher.py) | Strict read-only export of Query Service templates to local Markdown + RTF. Never writes back. |

**AEP Swiss Army range** — batch / credential utilities:

| Tool | What it does |
|------|--------------|
| [`batch_fetcher.py`](batch_fetcher.py) | Lists recent batches, then downloads a chosen batch's files locally. |
| [`failed_batch_report.py`](failed_batch_report.py) | Exports a CSV summary of every batch that failed in the last N hours. |
| [`credential_validator.py`](credential_validator.py) | Quickly checks whether an IMS/AEP credential set is alive and what it can see. |

All tools are stdlib-only, config-driven, and tenant-aware so the same
scripts run cleanly across multiple Adobe orgs without folder collisions.
`config.json` and `creds/*.json` are gitignored — never commit them; they
contain credentials.

## Common setup

1. `cp config.example.json config.json` and fill in your Adobe IMS
   `client_id`, `client_secret`, `org_id`, and `sandbox_names`.
2. Optionally paste an `anthropic_api_key` to enable AI-suggested names in
   the renamer.

---

# Babelfish range

## babelfish_query_renamer.py

Tidies up SQL query templates in AEP's Query Service (nicknamed *Babelfish*)
by pulling them down, suggesting sensible names — optionally via the Claude
API — and pushing the renames back.

Babelfish makes it easy to fire off a lot of queries during exploration and
iterative development. There's no enforced naming convention, so over a few
weeks the Templates panel ends up full of `33333`, `xxxxx`,
`testsite_c - select all`, half-finished experiments, and several "v2"s of
the same idea. This script lists every template you own, proposes a clean
kebab-case name (tagged `[babelfish]` so you can always tell which were
AI-renamed), and lets you accept, edit, or skip per-template — or run the
whole thing in batch mode. Each run also writes a snapshot and rebuilds a
single cross-tenant Markdown mega-file with every query's SQL.

```
python babelfish_query_renamer.py
```

## babelfish_query_fetcher.py

A strict read-only fork of the renamer: it **never writes back to AEP** —
no PUT, no rename, no name suggestions, no prompts. It authenticates,
discovers every accessible sandbox, fetches all templates, excludes
system/service-account queries (Adobe's `@AdobeID` namespace), and writes
the human-authored queries to local `.sql` files plus a cross-tenant
Markdown mega-file and a formatted RTF (titled header, per-sandbox
sections, monospace SQL). Fully non-interactive — safe to schedule.

```
python babelfish_query_fetcher.py
```

# Swiss Army range

## batch_fetcher.py

Pulls a single AEP batch's files down to your local machine. It lists the
most recent batches in the sandbox, you paste (or pick) the batch ID you
want, and the script downloads every file in that batch and surfaces any
embedded error payloads. Replaces three earlier scripts (`auth.py`,
`authandret.py`, `fetchbatch.py`). GBR9 region headers are sent by default
(override via `region` in `config.json`).

```
python batch_fetcher.py
```

## failed_batch_report.py

Exports a CSV summary of every batch that FAILED in the last N hours
(default 24) in the configured sandbox — a quick estate-wide health
snapshot. Use `batch_fetcher.py` instead when you need to drill into one
batch and download its failed-record files. Reports are written under
`./failed_batches/` (gitignored).

```
python failed_batch_report.py --hours=72 --sandbox=prod
```

## credential_validator.py

Quickly checks whether an Adobe IMS / AEP credential set is alive. Pick a
credential JSON from `./creds/`; it authenticates via OAuth
server-to-server, inspects the returned access token (granted scopes, org,
expiry, technical account) and lists the sandboxes the credential can
actually see — a useful proxy for tenancy and admin breadth.

It then sweeps the major AEP product API surfaces — Query Service, Catalog,
Schema Registry, Flow Service, Data Ingestion, Real-Time Profile, Identity,
Privacy, and Customer Journey Analytics — firing one lightweight GET at each
and reporting per product whether it **stood up** for this credential:
`UP` (reachable + authorized), `NO-PERM` (reachable but the credential
lacks access — e.g. an AEP-only credential hitting CJA), or `DOWN`
(unreachable). A quick "what can these creds actually talk to?" map.

```
python credential_validator.py            # interactive menu
python credential_validator.py --all      # validate every set in ./creds/
```
