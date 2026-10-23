# Adobe API gotchas

Things about Adobe's APIs that cost real debugging time, written down so they
only cost it once. Each entry says what the behaviour is, how it shows up, and
what to do instead.

Nothing here is in Adobe's documentation. All of it was established against a
live tenant.

---

## Audit events API

`https://platform.adobe.io/data/foundation/audit/events`

### The 90-day window drifts. Recompute it for every request.

Adobe measures "the minimum start date cannot be less than 90 days ago" from
the clock **at the moment each request lands** — not from when your run
started.

Calculate `now - 90 days` once at the top of a long job and that fixed start
date slowly slides out of range. Partway through, every remaining request
starts returning:

```
HTTP 422  {"detail":"The minimum start date cannot be less than 90 days ago."}
```

In `external_access_audit.py` this hit at **request 86 of 2,286**. Everything
before it succeeded; everything after it failed.

**Do this instead** — recompute per item, with a margin:

```python
WINDOW_MARGIN = timedelta(hours=2)
for account in accounts:
    b = datetime.now(timezone.utc)
    a = b - timedelta(days=days) + WINDOW_MARGIN
    ...
```

Verified over 219 consecutive requests with no drift.

### The user filter is `user==`, and it is CASE-SENSITIVE

Two traps in one field.

* The parameter is `user`, **not** `userEmail`. Using `userEmail==` returns a
  thoroughly misleading `422 "More than 2 timestamp filters are not allowed."`
* The audit log stores addresses **lower-cased**, while the Admin Console
  preserves whatever case an account was created with. So a roster address
  like `Firstname.Lastname@example.com` returns **zero events** for somebody
  who is in the platform every day.

```python
params = [("property", f"user=={email.lower()}")]   # always .lower()
```

Proven: the same address returned 1,000 events lower-cased and 0 upper-cased.

### `totalElements` is CAPPED at 1,000 — it is a floor, not a count

**Corrected 2026-10-20.** An earlier version of this file claimed the
per-user count was exact. It is not, and the error mattered: usage figures
were being reported as measurements when they were ceilings.

A single query returns at most 1,000 events, and `page.totalElements`
reports at most 1,000 with it. Two users with wildly different volumes both
come back as exactly `1000`:

```
heavy.user@example.com    totalElements = 1000   (really 28,106, per a full sweep)
another.user@example.com  totalElements = 1000
```

So:

* **A count of exactly 1,000 means "at least 1,000".** Present it as `1000+`,
  never as a number.
* **Zero is still a true zero.** The cap is a ceiling; it does not inflate a
  quiet account. This is why a purge list built on "is this exactly zero" is
  unaffected by the cap, while any *ranking* by event volume is meaningless
  above 1,000.
* To get a real count above the cap you must sweep the log and tally
  locally, or split the window until each slice is under 1,000.

### Events are returned newest-first

`_embedded.events[0].timestamp` is the user's most recent activity. Worth
knowing because it makes "last active" free — the query has already been
paid for, so there is no reason to leave that column empty.

### Why these three matter more than they look

All three failure modes return **"no activity"** rather than an error the
caller notices. On an access-review tool that silently becomes *"delete this
person"*.

The guard worth building: an account only counts as unused when **every**
sandbox actually answered. Anything less is a distinct `UNKNOWN` verdict that
is explicitly not safe to act on. That distinction caught 242 wrongly-listed
accounts while the window-drift bug above was still live — including a daily
user of the platform.

### Other practicalities

* **Retention is 90 days, hard.** Any wider window is refused outright, so
  "no usage" can only ever mean "none in 90 days", never "never".
* **A sandbox that 403s is not clean.** Sandboxes exist that a given
  credential cannot read at all. Report them as unreadable; silence from a
  door you could not open is not an all-clear.
* **CJA keeps its own separate audit trail.** It is not in this API. A user
  whose work is entirely in Customer Journey Analytics shows zero activity
  here and may be perfectly active.

---

## User Management API (Admin Console)

`https://usermanagement.adobe.io/v2/usermanagement`

* **Org-level: send no sandbox header.** `x-sandbox-name` must be omitted, not
  sent empty.
* `/users/{orgId}/{page}` gives every account with `groups` (its product
  profiles), `type` (`federatedID` / `adobeID` / `enterpriseID`), `status`,
  `domain` and `adminRoles`. Page until `lastPage` is true.
* `/groups/{orgId}/{page}` gives the profile catalogue, and crucially
  `productName` — which is the only way to tell which product a profile like
  `"Read Access"` actually belongs to.
* **`type` is a security signal.** `adobeID` is a personal Adobe account the
  corporate directory does not control, as opposed to a federated SSO
  identity. Worth surfacing on any access review.
* **A user's `groups` are DIRECT memberships only.** Access inherited by
  nesting a user group inside a product profile is not resolved by this API,
  so any entitlement count derived from it is a **floor, not a total**.

---

## Schema Registry

* **Adobe hex-ids every tenant-created custom class**, so a class id ending in
  a long hex blob says nothing about whether a schema was hand-modelled or
  auto-generated. Use `schemaType` (`standard` vs `adhoc`) and the namespace
  key (`meta:tenantNamespace` vs `meta:datasetNamespace`) instead.
  See `RELEASE_NOTES.md` under data_dictionary v3.4.8 and v3.6.
* **The `?unifiedProfile=enabled:true` filter on Catalog `/dataSets` is
  silently ignored** and returns everything. Filter client-side on
  `tags.unifiedProfile`.
* **The Query Service table name lives in `tags['adobe/pqs/table']`**, and is
  not the friendly dataset name. SQL needs the former.

---

## A general rule, learned the hard way

When an API can fail in a way that looks identical to a legitimate negative
answer, the two **must** be tracked separately all the way to the output. Any
tool whose result removes access, deletes data, or closes something down
should be able to say, per row, *"we asked and the answer was no"* versus
*"we could not ask"*.

If it cannot make that distinction, it is not safe to act on, however tidy the
spreadsheet looks.
