# House rules

The rules every tool in the AEP Swiss Army Knife follows. They exist so that
anyone can pick up any tool, run it safely, and trust what it produces -- and so
the toolkit stays tidy as it grows across machines.

`python tests/check_house_rules.py` checks every rule that can be checked from
the code and lists what does not comply yet. It reports only for now; nothing is
blocked.

---

## 1. Finding and naming things

1. **Tools live in theme folders.** `data_dictionary/`,
   `platform_access_and_audit/`, `query_service/`,
   `performance_and_guardrails/`, `ingestion/`. The root holds only the README,
   the release notes, requirements, `check_my_setup.py` and config templates.
2. **Names say what the tool does**, in plain English, snake_case:
   `query_service_scheduled_queries_audit.py`, not `audit_batch_schedules_v2.py`.
   No version numbers, no nicknames ("babelfish", "hunt", "jump"). Length is
   fine -- nobody types them.
3. **Every tool is listed** in its folder's README with one line, and has a guide
   in `docs/tools/<tool name>.md`.
4. **Plumbing lives in `toolkit_internals/`** -- internal to this toolkit, not for
   other people to use. Tools import it (`from toolkit_internals import
   credential_loader`); they never copy it, and never import another *tool* just
   to borrow a helper.
5. **Unfinished or one-off work goes in `parked/`**, reference material in
   `reference/`, documents in `docs/`.

## 2. Running a tool

6. **Run from the repo root:** `python <folder>/<tool>.py`. Every tool puts the
   repo root on the path itself, so this works from any terminal.
7. **The same command line everywhere:** the credential name first, then
   `--sandbox=`, `--days=`, `--client=` as they apply. `--help` explains the tool.
8. **Never stuck at a prompt.** Given everything it needs on the command line, a
   tool runs start to finish without asking anything, so it can be left
   running or scheduled.
9. **Long runs keep the PC awake** for their duration and release the hold
   however they end. Power settings are never changed.

## 3. Credentials and safety

10. **Credentials come only from the credential loader:** Windows Credential
    Manager (keyring) first, the `creds/` folder only where keyring is
    unavailable. Store them with
    `platform_access_and_audit/credential_keyring_manager_and_access_probe.py`.
11. **Nothing secret or client-identifying is ever committed.** No secrets,
    tokens, client names, real people's email addresses or OneDrive paths in the
    code or docs. They belong in outputs, inputs and `config.json`, all
    gitignored. (A real client's name belongs on its report, never in the repo.)
12. **Read-only by default.** A tool only reads from Adobe. Anything that writes
    (renames, deletes, permission changes) sits behind an explicit flag, says
    exactly what it will change first, and is named as a writer in its folder
    README.
13. **Query Service queries are labelled.** Any query a tool submits is named
    `Data Dictionary -- <purpose> -- <dataset>` and starts with a
    `-- Data Dictionary: ...` comment, because everyone in the org sees the query
    log. SELECT only.
14. **No PostgreSQL connections.** The Query Service PostgreSQL route is not
    permitted and is switched off. Never add a PostgreSQL driver.

## 4. Heavy work

15. **Estimate before running anything heavy:** how many Adobe calls, roughly
    how long. Say it before starting.
16. **Develop heavy passes in ROI prod first** (`--sandbox=roi-prod`), never
    UK prod. A scoped test on two or three datasets is fine anywhere; a full
    first run is not.
17. **An optional part may fail, but never takes the main output with it.** A
    beta pass that falls over is logged, and the workbook is written without it.
18. **Say which Adobe endpoint is slow.** Retries name the endpoint and the
    reason (no answer in 90s, HTTP 504); long runs end with an endpoint health
    report.

## 5. What a tool produces

19. **One workbook, overwritten.** A single workbook with a stable name, replaced
    on each run: no timestamped copies, no CSV or JSON alongside. The only other
    files are caches, inputs and the Data Dictionary's full-PQL sidecar (Excel
    cannot hold a long rule).
20. **The client's name is in the file name** (`--client`, or the credential's
    `client` key), so workbooks from different organisations can never be mixed
    up.
21. **Output goes to `AEP_OUTPUT_DIR`** when it is set (each machine sets it to
    its own corporate storage), otherwise to the repo's `output/`. **Caches and
    run-to-run state always stay in the repo's `output/`**, never synced.
22. **House style**, via `toolkit_internals/house_style_excel_writer.py`
    (`Book(...).sheet(...).save(...)`): STRICTLY CONFIDENTIAL banner, title and
    italic note, blue header row with filters, frozen panes, fixed widths,
    Summary tab first.
23. **The author is Barry Mann ([barrymann.com](https://barrymann.com))** on every
    workbook and deck -- set the file's author and last-modified-by properties to
    `SCRIPT_AUTHOR`, or the library signs it as itself (`openpyxl`,
    `python-pptx`).

    ```python
    wb = Workbook()
    wb.properties.creator = wb.properties.lastModifiedBy = SCRIPT_AUTHOR
    ```

24. **Times are UTC and say so.**

## 6. Changing a tool

25. **Every tool carries its version, date and author** (`SCRIPT_VERSION`,
    `SCRIPT_DATE`, `SCRIPT_AUTHOR`) and its header says what question it answers,
    whether it is read-only, what it writes, and how to run it.
26. **A change bumps the version and gets a release note.** The Data Dictionary's
    notes are [`RELEASE_NOTES.md`](../RELEASE_NOTES.md); its workbook stamps the
    commit that produced it, so commit before a run you want to keep.
27. **Develop outside, then promote.** A new capability starts as its own tool,
    is checked by eye, and only then folds into the Data Dictionary -- and the
    standalone stays.
28. **Commit straight to `main`**, with a message that says what changed and
    why.
