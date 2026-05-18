# Swiss Army Knife

A small toolkit of single-file, stdlib-only command-line tools for working
with Adobe Experience Platform (AEP) from a locked-down VDI — no
`pip install` required. Every tool reads the same shared `config.json`
(copied from `config.example.json`), so credentials are configured once.

| Tool | What it does |
|------|--------------|
| [`babelfish_query_renamer.py`](babelfish_query_renamer.py) | Tidies up messy Query Service SQL template names (optionally AI-suggested via Claude). |
| [`batch_fetcher_2.py`](batch_fetcher_2.py) | Lists recent batches, then downloads a chosen batch's files locally. |
| [`failed_batch_report.py`](failed_batch_report.py) | Exports a CSV summary of every batch that failed in the last N hours. |
| [`prober.py`](prober.py) | Quickly checks whether an IMS/AEP credential set is alive and what it can see. |

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

## batch_fetcher_2.py

Pulls a single AEP batch's files down to your local machine. It lists the
most recent batches in the sandbox, you paste (or pick) the batch ID you
want, and the script downloads every file in that batch and surfaces any
embedded error payloads. Replaces three earlier scripts (`auth.py`,
`authandret.py`, `fetchbatch.py`). GBR9 region headers are sent by default
(override via `region` in `config.json`).

```
python batch_fetcher_2.py
```

## failed_batch_report.py

Exports a CSV summary of every batch that FAILED in the last N hours
(default 24) in the configured sandbox — a quick estate-wide health
snapshot. Use `batch_fetcher_2.py` instead when you need to drill into one
batch and download its failed-record files. Reports are written under
`./failed_batches/` (gitignored).

```
python failed_batch_report.py --hours=72 --sandbox=prod
```

## prober.py

Quickly checks whether an Adobe IMS / AEP credential set is alive. Pick a
credential JSON from `./creds/`; the prober authenticates, decodes the
returned JWT (granted scopes, org, expiry, technical account) and lists the
sandboxes the credential can actually see — a useful proxy for tenancy and
admin breadth.

```
python prober.py            # interactive menu
python prober.py --all      # probe every set in ./creds/
```
