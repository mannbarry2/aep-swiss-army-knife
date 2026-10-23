# ingestion/

Getting data in: failed batches, and what drove a jump in Profile.

| Tool | What it does |
|---|---|
| [`failed_batches_summary_report.py`](failed_batches_summary_report.py) | Every batch that failed in the last N hours, in one workbook. |
| [`failed_batch_file_downloader.py`](failed_batch_file_downloader.py) | Lists recent batches and downloads a chosen batch's files. |
| [`profile_count_jump_investigation.py`](profile_count_jump_investigation.py) | What wrote millions of records into Profile for one dataset, and who triggered it. |

Run any tool from the repo root: `python ingestion/<tool>.py --help`.
