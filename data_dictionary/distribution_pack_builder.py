#!/usr/bin/env python3
"""
data_dictionary/distribution_pack_builder.py  (AEP Swiss Army Knife)
=============================================
Builds the Data Dictionary DISTRIBUTION PACK: one zip someone can unzip into a
folder and use straight away -- no Adobe access, no install, no internet. For
each market it holds the Explorer (one .html, opens in Edge or Chrome) and the
workbook it was drawn from (.xlsx), plus a READ ME FIRST page. The version is
in the zip's name, the folder's name, every file's name and on every page.

Reads only the Data Dictionary's own output; makes no Adobe calls.

Usage:
    python data_dictionary/distribution_pack_builder.py
    python data_dictionary/distribution_pack_builder.py --markets=prod:UK,roi-prod:ROI,hu-prod:HU
Each market is <sandbox>:<name people know it by>. The newest workbook for each
in AEP_OUTPUT_DIR/data_dictionary (else output/data_dictionary) is used; every
workbook must be the same Data Dictionary version. The zip is written to
.../data_dictionary/distribution/ as
"AEP Data Dictionary v<version> - <date>.zip". Put it on the SharePoint
distribution folder rather than email, so it stays inside the company.
"""

from __future__ import annotations

import html
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from data_dictionary import data_dictionary_explorer_html_builder as explorer  # noqa: E402

SCRIPT_NAME = "distribution_pack_builder"
SCRIPT_VERSION = "1.0.0"
SCRIPT_DATE = "2026-10-27"
SCRIPT_AUTHOR = "Barry Mann (barrymann.com)"
DEFAULT_MARKETS = "prod:UK,roi-prod:ROI"


def opt(name, default=None):
    return next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith(f"--{name}=")), default)


def newest_workbook(folder: Path, sandbox: str) -> Path | None:
    return max(folder.glob(f"Data Dictionary - {sandbox} - *.xlsx"), key=lambda p: p.stat().st_mtime, default=None)


def read_me(version: str, built: str, markets: list[dict]) -> str:
    rows = "".join(
        f"<tr><td><b>{html.escape(m['name'])}</b></td><td>{html.escape(m['read'])}</td>"
        f"<td><code>{html.escape(m['explorer'])}</code></td><td><code>{html.escape(m['workbook'])}</code></td></tr>"
        for m in markets)
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="author" content="{html.escape(SCRIPT_AUTHOR)}"><title>READ ME FIRST – AEP Data Dictionary v{version}</title>
<style>
body{{font:15px/1.55 "Segoe UI",system-ui,Arial,sans-serif;max-width:900px;margin:0 auto;padding:28px 20px 60px;color:#1c2230;background:#fff}}
.ver{{display:inline-block;background:#1F4E78;color:#fff;font-weight:700;border-radius:12px;padding:4px 14px;font-size:20px}}
h1{{font-size:28px;margin:14px 0 4px}} h2{{font-size:18px;margin:28px 0 8px;color:#1F4E78}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th{{background:#1F4E78;color:#fff;text-align:left;padding:7px 9px}}
td{{border-bottom:1px solid #e2e5eb;padding:7px 9px;vertical-align:top}} code{{font-family:Consolas,monospace;font-size:12.5px}}
.box{{background:#FFF2CC;border-radius:8px;padding:12px 16px}} li{{margin:4px 0}} .sub{{color:#5d6678;font-size:13px}}
</style></head><body>
<span class="ver">Data Dictionary v{version}</span>
<h1>AEP Data Dictionary – read me first</h1>
<p class="sub">Pack built {html.escape(built)} · by {html.escape(SCRIPT_AUTHOR)}</p>

<h2>What this is</h2>
<p>A picture of our Adobe Experience Platform data, read straight from Adobe: every schema and field,
how often each field is filled in and how far back it goes, where the data comes from, and which
audiences use it – and where those audiences go (journeys and destinations).</p>

<h2>How to open it</h2>
<ol>
<li><b>Unzip the whole folder first</b> (right-click the zip, <i>Extract All</i>). Opening a file from inside the zip will not work properly.</li>
<li>Double-click the <b>Explorer</b> file for your market. It opens in Edge or Chrome – nothing to install, no Adobe login, and it works offline.</li>
<li>Search for anything at the top (press <b>/</b>), or start from the home page. Every name is a link: click along the lineage.</li>
<li>The <b>workbook</b> (.xlsx) is the full detail the Explorer is drawn from; its <i>How to Use</i> tab explains every column.</li>
</ol>

<h2>What is in this pack</h2>
<table><tr><th>Market</th><th>Data read from Adobe</th><th>Explorer (open this)</th><th>Workbook</th></tr>{rows}</table>

<h2>Good to know</h2>
<ul>
<li>It is a <b>snapshot</b>: true on the date above. Check the version and date before relying on it.</li>
<li>Coverage and "most common values" come from a <b>sample</b> of recent records, not every record.</li>
<li>"<b>Nothing found using it</b>" on an audience means no journey, destination or other audience uses it as far as Adobe's APIs show – not proof. Ask the person who made it before unpublishing.</li>
<li>Adobe Analytics report-suite data is hidden in the Explorer by default (the <b>AA hidden</b> switch shows it); it is all in the workbook.</li>
</ul>

<div class="box"><b>Internal only.</b> This holds our customer data model, audience rules and colleagues' names.
Keep it inside the company: share it from the Data Dictionary distribution folder on SharePoint, not by email or outside Tesco.</div>
</body></html>
"""


def main():
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        return
    folder = Path(os.environ.get("AEP_OUTPUT_DIR") or REPO / "output") / "data_dictionary"
    markets = []
    for item in (opt("markets", DEFAULT_MARKETS) or "").split(","):
        sb, _, name = item.partition(":")
        wb = newest_workbook(folder, sb.strip())
        if not wb:
            sys.exit(f"No Data Dictionary workbook for {sb} in {folder} -- run the Data Dictionary first.")
        markets.append({"sandbox": sb.strip(), "name": (name or sb).strip(), "path": wb})

    print(f"{SCRIPT_NAME} v{SCRIPT_VERSION} ({SCRIPT_DATE})  by {SCRIPT_AUTHOR}")
    built = datetime.now().strftime("%Y-%m-%d %H:%M")
    with tempfile.TemporaryDirectory() as tmp:
        models = []
        for m in markets:
            stem = m["path"].stem
            date = stem.rsplit(" - ", 1)[-1]
            pql = folder / "pql" / f"audiences_pql_{m['sandbox']}_{date.replace('-', '')}.jsonl"
            model = explorer.build_model(explorer.read_workbook(m["path"]), explorer.read_pql(pql), m["path"].name, m["name"])
            models.append((m, model, date))
        versions = {model["meta"]["version"] for _, model, _ in models}
        if len(versions) != 1 or not next(iter(versions)):
            sys.exit(f"The workbooks are not all one Data Dictionary version ({', '.join(sorted(map(str, versions)))}): "
                     "re-run the older ones first, or leave them out with --markets.")
        version = versions.pop()
        top = f"AEP Data Dictionary v{version} - {built[:10]}"
        root = Path(tmp) / top
        root.mkdir()
        listed = []
        for m, model, date in models:
            ex_name = f"Data Dictionary Explorer - {m['name']} - v{version} - {date}.html"
            wb_name = f"Data Dictionary - {m['name']} - v{version} - {date}.xlsx"
            (root / ex_name).write_text(explorer.render_html(model), encoding="utf-8")
            shutil.copy2(m["path"], root / wb_name)
            read = (model["meta"].get("generated") or "").split("|")[0].replace("Generated", "").strip()
            listed.append({"name": m["name"], "read": read, "explorer": ex_name, "workbook": wb_name})
            print(f"  {m['name']:<5} {m['path'].name}  ->  {ex_name}")
        (root / "READ ME FIRST.html").write_text(read_me(version, built, listed), encoding="utf-8")
        out_dir = folder / "distribution"
        out_dir.mkdir(parents=True, exist_ok=True)
        zip_path = out_dir / f"{top}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in sorted(root.iterdir()):
                z.write(f, f"{top}/{f.name}")
    print(f"written: {zip_path}  ({zip_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
