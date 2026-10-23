# platform_access_and_audit/external_access_entitlement_and_usage_audit.py

**Who can reach Experience Platform, and which of them are actually using it.**

---

## Why this exists

Access accumulates. People join a project, get added to a group, and move on.
Consultants finish an engagement. Partner agencies rotate staff. Nobody is
responsible for noticing, because nothing breaks when access outlives its
purpose — it just sits there.

The Admin Console will tell you who *has* access. The audit log will tell you
what people *did*. Neither will tell you the thing you actually want to know,
which is the overlap: **who has access and is not using it.** Putting those
two together by hand means querying the audit log one email at a time, which
nobody is going to do for several hundred people.

This tool does that, in about a minute.

### The distinction the whole thing rests on

> **Entitlement** is what the Admin Console says somebody *can* reach.
> **Use** is what the audit log says they *did*.

An account with entitlement and no use is a standing risk nobody is currently
exploiting — a door left unlocked. An account with both is a person doing
their job. An account with use and *no* entitlement should be impossible, and
if one appears, something is wrong with your access model.

Report only one of the two and you get the severity wrong in both directions.

---

## It cannot change anything, and that is enforced

The output of this tool is used to remove people's access. It must never be
able to alter what it is measuring.

"It only sends GETs" is a promise about code as it stands, not a property of
it — one careless edit, or one imported helper that posts, and the promise is
void with nothing to notice. So the guarantee is enforced at the network
layer instead:

```python
install_readonly_guard()   # runs before anything else in main()
```

This wraps `urllib` for the entire process. **Any request that is not a GET
raises before it leaves the machine**, including requests made by modules this
file imports but does not own.

There is exactly one exception, and it is not a data write: OAuth requires the
token request itself to be a POST. It is matched against a two-entry allowlist
on exact scheme + host + path, so a lookalike host or a different path on the
same host is still refused.

**There is deliberately no flag to disable this.** Making the tool write
something would mean deleting the guard, which is the amount of deliberation
that change deserves.

---

## Running it

```bash
# the one you will use most: ~1 minute
python platform_access_and_audit/external_access_entitlement_and_usage_audit.py kuba --days 90 --sandboxes prod --no-sweep

# the occasional deeper pass: ~2 hours
python platform_access_and_audit/external_access_entitlement_and_usage_audit.py kuba --days 90 --sandboxes prod
```

### Two modes, two different questions

|  | **Mini** (`--no-sweep`) | **Full sweep** |
|---|---|---|
| Asks | *Are the people with access using it?* | *Is anyone using it who shouldn't be?* |
| Method | One audit query per entitled person | Downloads every event, tallies per person |
| Cost | ~1 min, ~370 requests | ~2 hrs, millions of events |
| Gives you | The removal list | The removal list **plus** anyone active without entitlement |

Run the mini version routinely. Run the full sweep occasionally — monthly, or
when something feels off. The full sweep is how the AD-group gap below was
found, so it does earn its keep, just not every time.

### Options worth knowing

| flag | default | why |
|---|---|---|
| `--sandboxes` | `prod` | One sandbox rather than nine is a 9× saving. `all` restores the wide behaviour. |
| `--days` | `90` | Adobe's retention limit. Anything larger is refused by the API. |
| `--no-sweep` | off | Skip the whole-log pass. The removal list is just as accurate without it. |
| `--no-verify` | off | Skip the per-email re-check. **Do not use** if you intend to act on the output. |
| `--home-domain` | detected | The corporate domain; everything else counts as external. Detected from the roster. |

---

## Scope

**In:** Experience Platform / RTCDP, and Journey Optimizer (AJO).

**Out:** Customer Journey Analytics, Workfront, Adobe Analytics, Campaign,
Target, AEM, Creative Cloud. These are counted on the Summary tab so you can
see they were considered and set aside, not missed.

CJA is excluded both because it is a separate responsibility and because it
keeps its own audit trail that this tool cannot read — reporting on it here
would be guesswork either way.

### The thing that will catch you out: access comes from AD groups

Most people do **not** get Platform access through an Adobe product profile.
They get it through an Active Directory group nested into one:

```
GG-UK-<Org>-PROD-AdobeAEP-Analyst
GG-UK-<Org>-PROD-AdobeAEP-AdobeConsultants
GG-UK-<Org>-PROD-AdobeAEP-CRM
```

Those groups carry no Adobe product name in the API. An early version of this
tool looked only at product profiles and **missed 120 such groups**. The
symptom was 67 accounts visibly using Platform every day while the report
claimed they had no entitlement at all — an impossible combination, which is
what exposed the fault.

If you ever see "active without entitlement" in meaningful numbers, suspect
the scope test before you suspect the platform.

---

## Reading the output

One workbook: `output/aep-user-logs/external_access_audit_<date>.xlsx`

| tab | what it holds |
|---|---|
| **Summary** | The numbers, the scope, and what the data cannot tell you |
| **All accounts** | Every account, with usage and a verdict |
| **Purge list** | Entitled, no usage found — the removal list |
| **External accounts** | Everything outside the corporate domain |
| **Profiles** | What grants access, and how many hold each |
| **Tech accounts** | Service / API integrations by volume — not people |

### The verdicts

| verdict | meaning |
|---|---|
| `ACTIVE` | Has access, is using it |
| `PURGE -- no usage found` | Has access, no recorded activity. **The removal list.** |
| `UNKNOWN -- could not check` | A query failed. **Not safe to act on.** |
| `ACTIVE WITHOUT AEP ENTITLEMENT` | Using it with no detectable access. Investigate. |
| `no AEP access, no usage` | Not your problem |

**`UNKNOWN` is the important one.** An account only reaches the purge list
when every readable sandbox actually answered. A failed query is never
counted as "no usage" — because a rejected question looks identical to a
negative answer, and acting on that difference is how you cut off somebody
who is working.

---

## What it cannot tell you

State these whenever you hand the output to someone else. They are on the
Summary tab of every run for that reason.

1. **The audit log retains 90 days.** Adobe refuses a wider window. "No
   usage" means none in 90 days — never "never".
2. **Prod only, by default.** Someone working exclusively in a non-prod
   sandbox appears unused. Deliberate, but worth remembering for developers.
3. **A sandbox that refuses the credential is not an all-clear.** Some
   sandboxes 403 for some credentials; those are reported as unreadable.
   Silence from a door you could not open proves nothing.
4. **CJA is not covered**, and keeps a separate audit trail.
5. **Entitlement is read from group memberships.** If access can be granted
   by some route not visible there, the entitled count is a floor.
6. **Technical accounts cannot yet be joined to their usage.** Access Control
   identifies API credentials by IMS id (`2C56827C…@techacct.adobe.com`); the
   audit log identifies them by UUID (`8531ca25-…@techacct.adobe.com`). The
   two sets do not overlap, so the Tech accounts tab shows usage without
   entitlement. Do not read it as a complete inventory of credentials.

---

## Three bugs worth knowing about, because they all failed the same way

Each of these made a failure look like a legitimate "no activity" — the one
error that gets a working account deleted. They are fixed; the pattern is
worth carrying to any tool like this.

**1. The 90-day window drifted.** The window was calculated once at the start
of a run. Adobe measures that boundary from the moment each request *lands*,
so the fixed date slid out of range part-way through — request 86 of 2,286
began returning `HTTP 422` and every one after it failed. Those failures were
counted as "no activity". **242 accounts were queued for deletion, including
daily users.** Now recomputed per account with a two-hour margin.

**2. The user filter is case-sensitive.** The audit log stores addresses
lower-cased; the Admin Console preserves whatever case an account was created
with. `user==Firstname.Lastname@…` returns zero events for somebody who is in
the platform every day. Emails are lower-cased before querying.

**3. A failed query was counted as a negative answer.** Fixed by the `UNKNOWN`
verdict described above.

See [adobe-api-gotchas.md](adobe-api-gotchas.md) for the API-level detail.

---

## Known rough edges

- **The workbook is only written at the very end of a run.** A late failure
  loses the whole pass. Harmless for the mini version; annoying for the
  two-hour sweep.
- **The sweep's day cache invalidates too easily.** It is keyed partly on
  which service account is noisiest and what it is doing, and that changes
  day to day — so the cache is usually void and the sweep usually costs the
  full two hours.
- **Technical accounts are enumerated from whatever events turned up**, not
  from a definitive list. A quiet integration can be missed entirely.

---

## Endpoints used

All GET, except the IMS token POST.

| endpoint | purpose |
|---|---|
| `ims-na1.adobelogin.com/ims/token` | authentication (the one POST) |
| `usermanagement.adobe.io/v2/usermanagement/users/{org}/{page}` | the roster |
| `usermanagement.adobe.io/v2/usermanagement/groups/{org}/{page}` | profile catalogue |
| `platform.adobe.io/data/foundation/audit/events` | usage — one query per email |

The audit endpoint is effectively the entire cost. Everything else is about
ten calls.
