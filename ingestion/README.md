# ingestion/

Getting data in: failed batches, what drove a jump in Profile, and stocking a demo sandbox.

| Tool | What it does |
|---|---|
| [`failed_batches_summary_report.py`](failed_batches_summary_report.py) | Every batch that failed in the last N hours, in one workbook. |
| [`failed_batch_file_downloader.py`](failed_batch_file_downloader.py) | Lists recent batches and downloads a chosen batch's files. |
| [`profile_count_jump_investigation.py`](profile_count_jump_investigation.py) | What wrote millions of records into Profile for one dataset, and who triggered it. |
| [`luma_demo_sandbox_builder_and_event_feeder.py`](luma_demo_sandbox_builder_and_event_feeder.py) | **Writer, demo sandboxes only** (`--apply`). Builds Adobe's Luma demo in a sandbox keeps it fed with synthetic events, and creates demo audiences. [Guide](../docs/tools/luma_demo_sandbox_builder_and_event_feeder.md). Its support modules are in [`luma_demo_sandbox/`](luma_demo_sandbox/). |

Run any tool from the repo root: `python ingestion/<tool>.py --help`.
