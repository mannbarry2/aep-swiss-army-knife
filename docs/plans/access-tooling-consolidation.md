# Consolidating the access tooling

**Status: proposal. Nothing merged yet.**

Written 21 Oct 2026, after four tools grew into the same space from three
different machines.

---

## What exists today

| file | lines | what it answers |
|---|---:|---|
| `toolkit_internals/credential_loader.py` | 554 | where credentials live. **Shared, works, leave alone.** |
| `platform_access_and_audit/credential_keyring_manager_and_access_probe.py` | 767 | is a credential alive? *(v1, plaintext JSON)* |
| `platform_access_and_audit/credential_keyring_manager_and_access_probe.py` | 1,343 | manage + validate credentials in the keyring |
| `platform_access_and_audit/user_activity_log_from_audit_events.py` | 967 | what each person did, from the audit log |
| `platform_access_and_audit/who_can_do_what_permissions_audit.py` | 384 | who can do what, from Access Control |
| `platform_access_and_audit/role_to_permission_grid.py` | 220 | role → permission grid, no people |
| `platform_access_and_audit/external_access_entitlement_and_usage_audit.py` | 1,407 | entitlement vs usage, the purge list |

Roughly **4,700 lines across five tools** that all answer parts of one
question: *who can reach the platform, and what are they doing on it.*

---

## The diagnosis

### 1. They have already started merging themselves, badly

```
credential_loader           <- everything
user_activity_log_from_audit_events       <- who_can_do_what_permissions_audit, external_access_entitlement_and_usage_audit
who_can_do_what_permissions_audit   <- role_to_permission_grid, external_access_entitlement_and_usage_audit
data_dictionary_v3  <- who_can_do_what_permissions_audit, role_to_permission_grid,
                       external_access_entitlement_and_usage_audit
```

`platform_access_and_audit/external_access_entitlement_and_usage_audit.py` already imports three other tools. That happened by
accident, not design, and it has an ugly consequence: **three tools import
`data_dictionary_v3` purely to borrow `http()`, `authenticate()` and
`list_sandboxes()`.**

So running a permissions report drags in the entire data dictionary — 4,000
lines of schema logic — to get an HTTP helper. That is the single worst thing
in the current arrangement and the easiest to fix.

### 2. The same plumbing is written three times

`authenticate()` and `http()` are reimplemented in `platform_access_and_audit/credential_keyring_manager_and_access_probe.py`,
`platform_access_and_audit/credential_keyring_manager_and_access_probe.py` and `platform_access_and_audit/user_activity_log_from_audit_events.py`. They differ in retry
behaviour, timeout handling and error reporting, so a fix in one does not
reach the others. The audit-window bug found on 20 Oct existed only in one of
them.

### 3. Each tool writes its own workbook

Four tools, four spreadsheets, four different naming conventions, and the
reader has to join them by hand — which is exactly the complaint.

### 4. The two credential validators — DONE, 21 Oct

v1 read plaintext `creds/*.json`; v2 managed the keyring. They were not two
jobs: **v2 probed the same 12 Adobe services and added store / list / delete /
migrate / sweep / purge on top.** An earlier draft of this plan claimed v1 had
unique probes worth absorbing — that was wrong, the result of a grep that did
not resolve f-strings. The only endpoint v1 had to itself was fetching a
single AJO journey, where v2 lists them.

`postman_to_keyring.py` was a third copy of the same idea: import a credential
file into the keyring, just a different file shape from `--import-json`.

All three are now one tool, **`platform_access_and_audit/credential_keyring_manager_and_access_probe.py`**: store, import (JSON or
Postman), authenticate, probe what the key can reach, and manage the set.
Postman parsing was moved across and proved identical on both export shapes
before the originals were deleted.

---

## What is proposed

### Keep separate, deliberately

- **`data_dictionary_v3.py`** — a different job entirely. Not in scope.
- **`toolkit_internals/credential_loader.py`** — already the right shape. Nothing to do.

### Create one small shared layer

**`aep_api.py`** — the thing that should have existed from the start:

```
authenticate()      one implementation, with the retry and window-drift
http()              fixes already learned
aep_headers()
list_sandboxes()
AuditClient         lifted out of user_activity_log_from_audit_events
readonly_guard()    lifted out of external_access_entitlement_and_usage_audit
```

Every tool imports this instead of borrowing from `data_dictionary_v3`. This
change alone is worth doing even if nothing else happens.

### Collapse four tools into one, with modes

**`aep_access.py`** — one tool, one workbook, several modes:

| mode | answers | replaces |
|---|---|---|
| `--mode people` | who has access, who uses it, who to purge | `external_access_entitlement_and_usage_audit` |
| `--mode usage` | per-person activity detail | `user_activity_log_from_audit_events` |
| `--mode permissions` | subject × role × permission | `who_can_do_what_permissions_audit` |
| `--mode roles` | the role → permission grid | `role_to_permission_grid` |
| `--mode credentials` | technical accounts: usage + entitlement | *(new, the gap)* |
| `--mode all` | every tab in one workbook | — |

**One workbook**, tabs named consistently, written to `$AEP_OUTPUT_DIR`:

```
Summary            scope, numbers, and what the data cannot tell you
Access review      everyone with access, last used, recommendation
Purge list         entitled, no usage
All accounts       the full roster with verdicts
Credentials        technical accounts: roles AND usage
Permissions        subject x role x permission
Roles              the role -> permission grid
Profiles           what grants access, and to how many
```

### Merge the validators

`platform_access_and_audit/credential_keyring_manager_and_access_probe.py` absorbs v1's liveness probes. `platform_access_and_audit/credential_keyring_manager_and_access_probe.py`
is deleted. One tool: store a credential, check it works, see what it can reach.

---

## Scope, settled

**In:** AEP / RTCDP, AJO. CJA optional behind a flag — it keeps its own audit
trail, so usage for CJA-only users will always be incomplete and the tool must
say so rather than imply otherwise.

**Out:** Workfront, Analytics, Campaign, Target, AEM, Creative Cloud, Data
Collection, Launch, Privacy Service, Assurance. Counted on the Summary tab so
they are visibly set aside, never silently dropped.

**Technical accounts are in scope** and are the one genuinely unsolved piece —
see below.

---

## The one thing consolidation will not fix

Adobe identifies a service account three ways, and no two APIs agree:

```
Access Control      2C56827C6A2932E90A495EFE@techacct.adobe.com
Audit log           8531ca25-5321-4df2-a937-9450bd4af0ce@techacct.adobe.com
Developer Console   shows BOTH, on one page, and is the only thing that does
```

So "this credential has these rights and is doing this much" cannot be
assembled from the APIs. It needs a lookup table built by hand from Developer
Console. That table already exists at `inputs/api_credential_names.csv`
(gitignored — the names are client identifiers).

**Consolidation does not solve this. It just gives it one obvious home** rather
than leaving it to be rediscovered each time.

---

## Order of work

Smallest risk first; each step is useful on its own and none of them requires
the next.

1. **Extract `aep_api.py`.** Stop three tools importing the data dictionary for
   an HTTP helper. No behaviour change, immediately safer.
2. **Retire `platform_access_and_audit/credential_keyring_manager_and_access_probe.py`**, moving its probes into v2.
3. **Build `aep_access.py` around the existing code**, mode by mode, starting
   with `people` because it is the one in use. Keep the old tools working until
   each mode is proven against them — same inputs, same numbers.
4. **Delete the old tools** only once their mode produces identical output.
5. **Fold in the technical-account lookup** as a first-class input rather than
   a side file.

**Do not** start at step 3. A big-bang rewrite of four tools that currently
produce trusted numbers is how trusted numbers stop being trusted.

---

## Why this happened, for the record

Three machines, branches that did not merge cleanly, and each machine solving
the problem in front of it. `who_can_do_what_permissions_audit` and `role_to_permission_grid`
were written on one machine; `external_access_entitlement_and_usage_audit` on another, the same week,
against the same APIs.

Nothing here is wasted work — the overlap is mostly *plumbing*, and the
analysis in each tool is sound and hard-won. What is missing is a shared floor
beneath them, which is step 1.
