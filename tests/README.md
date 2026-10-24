# tests/

Checks for the Data Dictionary's field history and coverage. Run them from the
repo root.

## Offline tests -- run after any change to `data_dictionary_v3.py`

No Adobe calls; seconds each. Each prints `N/N passed`.

| File | What it checks |
|---|---|
| [`test_field_history_verdicts.py`](test_field_history_verdicts.py) | Months of unbroken data and the wording: gaps, stops, start dates, retention, unreadable months. |
| [`test_field_history_batch_kinds.py`](test_field_history_batch_kinds.py) | Which batches Data Access can read (Profile-side, consolidation and `-INGEST` batches cannot), and what is cached or retried. |
| [`test_field_history_months_and_profile.py`](test_field_history_months_and_profile.py) | How a month's batches are found (biggest first, then newest first, two batches read) and the "In Profile now" column, dataset by dataset. |
| [`test_coverage_sampling.py`](test_coverage_sampling.py) | `--data-dict` coverage: newest batches, then the biggest of the last 90 days; "Profile only" and "data-lake files empty" instead of a false "empty". |

```
for t in tests/test_*.py; do python "$t" | tail -1; done
```

## Independent check -- run after a `--history` run, before trusting it

[`field_history_independent_check.py`](field_history_independent_check.py)
re-asks Adobe the run's answers a different way -- every batch of each
"Profile only" month, untried batches of "data-lake files empty" months, the
month before each start date, empty gaps, "all N days in Profile" fields
re-read, and the Summary's retention lines against Catalog -- and says verdict
by verdict whether they hold. Read-only.

```
python tests/field_history_independent_check.py --sandbox=roi-prod
python tests/field_history_independent_check.py --sandbox=prod --limit=40   # a big sandbox: sample 40 per kind
```

Exit code 0 when everything checked holds. A new sandbox -- UK prod above all
-- should pass it before its history is relied on.
