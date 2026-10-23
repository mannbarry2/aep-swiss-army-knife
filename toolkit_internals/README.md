# toolkit_internals/

The plumbing every tool imports: one copy each.

| Tool | What it does |
|---|---|
| [`credential_loader.py`](credential_loader.py) | Loads a credential for any tool: keyring first, creds/ folder fallback. |
| [`house_style_excel_writer.py`](house_style_excel_writer.py) | Writes every tool's Excel workbook in house style (banner, author, headers, filters). |

Run any tool from the repo root: `python toolkit_internals/<tool>.py --help`.

These are libraries, imported by the tools -- not run directly.
