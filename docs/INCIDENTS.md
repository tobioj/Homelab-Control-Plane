# Incident log

Nine real failures of the Control Panel against real infrastructure, each with its cause and its fix. They are
the reason the architecture looks the way it does. (Pushlane's and the host's edge cases are written up in
[PUSHLANE-CI.md](PUSHLANE-CI.md#edge-cases-it-handles) and in
[proxmox-homelab](https://github.com/tobioj/proxmox-homelab).)

**The common thread: almost none of them were loud.** Nothing raised an exception. A component fell back to a
default, honoured the last of two duplicate values, or wrote `NULL` where it should have written nothing, and
the system kept running while being wrong. That quiet kind of failure is what this tool is built against.

| # | Incident | In one line |
| --- | --- | --- |
| 1 | [DNS-01 and the stale secondary resolver](#1-dns-01-and-the-stale-secondary-resolver) | Validate from the verifier's vantage point, not your own |
| 2 | [Duplicate keys, and the last one wins](#2-duplicate-keys-and-the-last-one-wins) | Generalise on the second occurrence |
| 3 | [A value cut off at an ampersand](#3-a-value-cut-off-at-an-ampersand) | Quote for the strictest reader |
| 4 | [DNS drift from a hand-typed list](#4-dns-drift-from-a-hand-typed-list) | Derive from state |
| 5 | [Monotonic state, enforced by a helper](#5-monotonic-state-enforced-by-a-helper) | Fifteen call sites need a function |
| 6 | [An allocator that didn't know its limit](#6-an-allocator-that-didnt-know-its-limit) | New guarantees break old callers |
| 7 | [The wrong field as a proxy](#7-the-wrong-field-as-a-proxy) | Pick a field only the event writes |
| 8 | [A pattern that matched too much](#8-a-pattern-that-matched-too-much) | Test patterns on real third-party names |
| 9 | [An idle poll with an unbounded cost](#9-an-idle-poll-with-an-unbounded-cost) | "Idle" depends on who is watching |

---

## 1. DNS-01 and the stale secondary resolver

**Symptom.** Issuance failed with `Incorrect TXT record "" found`, an *empty* record, although a `dig` a moment
later showed the right value.

**First fix, not enough.** The hook used to publish the record and sleep 30 seconds. It became an active poll:
query a public resolver every 3 seconds and continue only once the record is visible.

**Second symptom.** A different, more precise error: `During secondary validation: Incorrect TXT record
<STALE VALUE>`.

**Cause.** Let's Encrypt validates from several independent vantage points to resist DNS hijacking. Its
secondary check reached a *different* node of the DNS provider than my resolver did, and that node still held a
stale value from an earlier attempt. No single resolver could have seen it.

**Fix.** Poll **two** independent public resolvers and require both to agree, then wait a longer grace period,
under a longer overall ceiling.

**Still true.** This is stronger, not airtight: the provider could have a node neither resolver reaches. If it
returns, the next step is a third resolver or a longer grace period; the poll-until-visible pattern holds.

**Lesson.** One resolver's view of DNS is not "DNS". A check against a distributed system has to match the
*verifier's* vantage point.

---

## 2. Duplicate keys, and the last one wins

**Symptom.** Celery logged `No hostname was supplied. Reverting to default 'localhost'`, then endless
connection-refused errors against a broker it was never configured to use. Error tracking filled up for days.

**Cause.** The `.env` builder writes the authoritative lines first (Redis URLs from the ledger), then appends
every saved custom variable. Empty-string rows for those *same* keys had been saved during early onboarding,
before an upload filter existed. `EnvironmentFile=` keeps the **last** occurrence of a key, so the empty values
won and Celery fell back to its default broker.

**Why it took three tries.** The same class of bug appeared three times: first `ALLOWED_HOSTS` and `DEBUG` (an
uploaded `.env` overrode the computed value and produced `400 DisallowedHost`), then the broker and cache keys.
Each fix added *those* keys to a filter.

**Real fix.** One `governed_var_names()` set is the authority, combining the names detected in the repository
with the canonical ones, applied at all three layers: the upload route, the save route and the file builder.
Any code that writes a dedicated line registers its key there.

**Lesson.** When a bug comes back, the fix was at the wrong altitude. Generalise on the *second* occurrence, not
the third.

---

## 3. A value cut off at an ampersand

**Symptom.** After an unrelated fix, an app still connected to `localhost:5432` instead of its real database.
The variable was present, just cut short.

**Cause.** The connection string contained `&` (`?sslmode=require&channel_binding=require`). Unquoted, the
moment anything `source`d the file, the shell read `&` as "run this in the background" and dropped the rest of
the line. Reproducing the `source` by hand left the missing tail sitting in `jobs`.

**Fix.** Every generated value goes through `shlex.quote()`. Both readers, systemd's `EnvironmentFile` parser
and the shell, treat single-quoted values literally.

**Lesson.** A generated config file has more than one reader, each with its own parsing rules. Quote for the
strictest.

---

## 4. DNS drift from a hand-typed list

**Symptom.** One app reachable, two unreachable, all previously fine, no deploys in between. Every service was
healthy: processes up, sockets present, proxy config valid, identical local responses for all three.

**Cause.** The ISP had changed the WAN IP. The dynamic-DNS updater was a cron script with a **hand-typed**
domain list. The one app on it followed the new address; the two never added stayed pointed at the old one.
Nothing was wrong with any deployment.

**The flaw underneath.** The list had no link to the apps the panel manages, so **every newly onboarded app was
born exposed to this**: the panel issued its certificate, marked it live, and never added it to any updater.

**Fix.** A panel-owned updater that builds its domain set from the projects table on every run. Every managed
app is kept current automatically, for good.

**Lesson.** Reconciliation input must be *derived from state*, never kept alongside it. A hand-maintained list
next to a database is a drift generator.

---

## 5. Monotonic state, enforced by a helper

**Symptom.** A live, fully deployed app silently fell back to an earlier wizard step and lost its go-live
status, twice, on two different apps, in one session.

**Cause.** Each step's finaliser ran an unguarded `UPDATE projects SET step = '<literal>'`. Most steps are meant
to be re-visitable, so re-running an *earlier* step after a later one had finished moved the position backwards,
and the wizard offered work that was already done.

**First fix, not enough.** Guarding the one step that broke.

**Real fix.** One `advance_step()` that writes only when the new step is at or after the current one in the
canonical order, used at every call site, about fifteen of them. The two genuine undo paths stay unguarded on
purpose.

**Lesson.** A rule that must hold at fifteen call sites is a function, not a convention. The first fix treated
the symptom because I patched where it broke rather than everywhere it *could*.

---

## 6. An allocator that didn't know its limit

**Symptom.** An app's Celery worker failed with `DB index is out of range`: it had been given Redis indices 16
and 17 on a server with 16 databases (0 to 15).

**Cause.** The allocator was `MAX(index) + 1`, unconditionally. A long session of creating and deleting test
apps freed most low indices, but the counter kept climbing past the real ceiling.

**Fix, part one.** Reuse the lowest genuinely free index, and refuse rather than allocate at or beyond the
maximum. Index 0 is never reused: it's the default a bare client connects to.

**Fix, part two: the fix broke its callers.** Three call sites computed `cache_index = broker_index + 1`, right
only under the old always-increment behaviour. With reused, non-consecutive indices, that arithmetic could hand
out an index owned by another app, exactly the collision the ledger exists to prevent. The same sites did bare
`INSERT`s that now hit unique-constraint errors when a "free" index already had a row.

**Final fix.** The allocator takes an `exclude` set, so a caller that needs two indices asks for the second
properly. Every ledger write is an upsert.

**Lesson.** Changing a function's *guarantees* is a breaking change even when its signature stays the same.
Callers encode assumptions nobody wrote down.

---

## 7. The wrong field as a proxy

**Symptom.** An app got permanently stuck. "Delete certificate" tried a real deletion of a certificate that was
never issued and failed; "delete permanently" then refused because a field was still set, and the only way to
clear that field needed the deletion to succeed.

**Cause.** "Does this app have a certificate?" was answered with "is a subdomain recorded?". The subdomain is
recorded during *configuration*, long before issuance, so an app that never went live has a subdomain and no
certificate.

**Made worse by.** A *successful* no-op renewal wiped the expiry field to `NULL`. When a certificate isn't due,
certbot exits successfully and prints "not yet due for renewal" with no expiry line; the parser found no match
and wrote `NULL` over a perfectly good date. Recovering it took a forced re-issue.

**Fix.** Use the expiry date, set *only* by a successful issuance, as the proof a certificate exists. Never
write `NULL` on a non-match: a non-match means "nothing new", not "the old value is gone".

**Lesson.** When a field stands in for an event, choose one that *only* that event writes. And
`if match else None` is a bug wherever `None` means "erase".

---

## 8. A pattern that matched too much

**Symptom.** Caught in testing, not production: a real vendor API key in a repository's `.env.example` was
silently replaced with random bytes.

**Cause.** Secret-key generation matched `(^|_)SECRET_KEY$`, the same suffix style used for `ALLOWED_HOSTS` and
`DEBUG`. But `SECRET_KEY` is a far more crowded suffix among vendor credentials: it also matches
`STRIPE_SECRET_KEY`. The variable got a random value and was never shown on the form for the real one.

**Why it's nasty.** The app would boot fine with garbage payment credentials, and nothing would look wrong until
a real payment failed.

**Fix.** An exact-match allowlist of known framework key names instead of a suffix pattern.

**Lesson.** Before a suffix or substring pattern decides anything that matters, test it against real third-party
names. "Looks like a framework variable" is not a safe inference.

---

## 9. An idle poll with an unbounded cost

**Symptom.** A scheduler service crash-looped until systemd gave up (`Start request repeated too quickly`), and
signing in to the app returned HTTP 500.

**Investigation.** Both symptoms had one cause, confirmed from the app's own environment:

```
OperationalError: connection to server at "ep-example-pooler.REGION.aws.neon.tech", port 5432
failed: ERROR: Your account or project has exceeded the compute time quota.
```

The managed Postgres was refusing every connection:

- The scheduler uses a **database-backed** schedule, so it can't start without Postgres. The Celery *worker*
  stayed healthy throughout because it needs only Redis, which made this look like a scheduler bug rather than a
  database outage.
- Signing in is a database operation: no database, no sign-in, HTTP 500.

**Why the quota ran out.** The database-backed scheduler polls Postgres every five seconds, forever. The database
is serverless, bills by compute time and suspends when idle, so a five-second poll means it *never* idles. The
scheduler's idle state was continuous billable load.

**Fix.** A configurable poll interval, defaulting high enough for the database to suspend between checks.
Schedule edits now take up to that interval to be noticed, which is fine at this scale.

**Still true.** This removes the constant drain, not the monthly cap. If an app's periodic tasks really do touch
the database around the clock, the ceiling still applies; lifting it means a paid tier or self-hosted Postgres.

**Lesson.** "Idle" depends on who is watching. A polling loop is idle to the poller and fully busy to the billing
system. When adopting a serverless resource, audit everything that touches it on a timer.

---

## Cross-cutting observations

**Real dependencies find real bugs.** Four of the configuration bugs above exist only because testing used a real
private repository and a real managed database instead of a disposable fixture. A synthetic test would have had
no `&` in its connection string, no vendor-prefixed key and no stale rows.

**Silence is the common thread.** Nearly every incident was a component doing something reasonable-looking
instead of failing loudly: falling back to a default broker, keeping the last duplicate key, backgrounding at an
`&`, writing `NULL` on a non-match. Loud failures get fixed in minutes; quiet ones run for days.

**An unexplained reboot is not automatically a compromise.** One incident started as "am I hacked?". Checking
boot history, failed logins, `authorized_keys` timestamps, unfamiliar accounts, listening sockets and process
owners found a hypervisor-level reset with no trigger inside the guest and no sign of compromise. The real
finding was operational: the panel had no service unit and didn't come back after a reboot. Verify with evidence
before concluding in *either* direction.
