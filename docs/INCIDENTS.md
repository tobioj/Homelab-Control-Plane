# Incident log

Nine real failures against real infrastructure, with root cause and fix. These are the reason the
architecture looks the way it does. Ordered roughly by how much they changed my thinking.

A recurring theme: **most of these were silent.** They didn't raise exceptions. A component fell back to
a default, or honoured the last of two duplicate values, or wrote `NULL` where it should have written
nothing — and the system kept running while being wrong. That's the failure mode this tool is built
against.

---

## 1. DNS-01 validation and the stale secondary resolver

**Symptom.** Certificate issuance failed with `Incorrect TXT record "" found` — an *empty* record — even
though an independent `dig` moments later showed the correct value present.

**First fix (insufficient).** The hook script published the TXT record then slept a fixed 30 seconds.
Replaced the sleep with an active poll: query the record every 3 seconds against a public resolver, up
to a ceiling, and proceed only once it's visible.

**Second symptom.** Retest produced a *different, more specific* error:
`During secondary validation: Incorrect TXT record <STALE VALUE>`.

**Root cause.** Let's Encrypt deliberately validates from multiple independent network vantage points to
defend against DNS hijacking. The secondary check reached a *different* node of the DNS provider's
infrastructure than the one my chosen resolver was forwarding to — and that node still held a stale value
from a previous attempt. No single resolver's view could have detected this.

**Fix.** Poll **two** independent public resolvers and require both to agree before proceeding, plus a
longer post-confirmation grace period and a longer overall ceiling.

**Honest limitation.** This is stronger, not airtight. The provider could have a node neither resolver
routes through. If it recurs, the escalation is a third resolver or a longer grace period — the
poll-until-visible pattern itself is sound.

**Lesson.** One resolver's view of DNS is not "DNS." Any check against a distributed system needs to
match the *verifier's* vantage point, not your own.

---

## 2. Duplicate environment keys and last-occurrence precedence

**Symptom.** Celery logged `No hostname was supplied. Reverting to default 'localhost'` and then
continuous connection-refused errors against a message broker it was never configured to use. Errors
streamed into error tracking for days.

**Root cause.** The `.env` builder writes authoritative lines first (Redis URLs computed from the
ledger), then appends every saved custom variable verbatim. Stale empty-string rows for those *same*
keys had been saved during early onboarding, before an upload filter existed. `EnvironmentFile=` honours
the **last** occurrence of a duplicate key — so the empty strings won, and Celery fell back to its
built-in default broker.

**Why it took three attempts.** The same class of bug appeared three times:

1. First for `ALLOWED_HOSTS` / `DEBUG` — an uploaded `.env` smuggled in a stale value that silently
   overrode the computed one, producing a `400 DisallowedHost` on a fully-deployed app.
2. Then for the broker and cache keys — same mechanism, different key set.
3. The fix each time was narrow: add *those* keys to the filter.

**Proper fix.** A single `governed_var_names()` set is the authority, unioning the per-repo detected
names with the canonical ones, applied at three layers: the upload route, the save route, and the file
builder itself. Any code path that writes a dedicated line must register its key there.

**Lesson.** When the same bug recurs, the fix was at the wrong altitude. Generalise on the *second*
occurrence, not the third.

---

## 3. A configuration value truncated at an ampersand

**Symptom.** After fixing an unrelated bug, the app still connected to `localhost:5432` instead of the
real database. The variable was *present* in the environment — just cut off partway through.

**Root cause.** The connection string contained `&` (from `?sslmode=require&channel_binding=require`).
Written unquoted, the moment anything `source`d the file, the shell read `&` as "run the preceding
command in the background" and discarded the rest of the line. Confirmed by reproducing the `source`
outside the app and finding the truncated tail sitting in `jobs`.

**Fix.** Every generated value is `shlex.quote()`-wrapped. Both consumers — systemd's `EnvironmentFile`
parser and the shell's `source` — treat single-quoted values as literal.

**Lesson.** A generated config file has more than one consumer, each with its own parsing rules. Quote
for the strictest one.

---

## 4. DNS drift between a hand-typed list and reality

**Symptom.** One app reachable, two others unreachable — previously working, no deploys in between.
Every service on the host was healthy: processes active, sockets present, proxy config valid, identical
local responses for all three.

**Root cause.** The ISP had rotated the WAN IP. The dynamic-DNS updater was a cron script with a
**hand-typed** domain list. The one app in that list followed the new address; the two that had never
been added stayed pointed at the dead one. Purely stale DNS — nothing wrong with any deployment.

**The design flaw it exposed.** That list had no connection to the set of apps the panel actually
manages, so **every newly onboarded app was born prone to this.** The panel would issue a certificate,
mark the app live, and never add it to any DNS updater.

**Fix.** A panel-owned updater that derives its domain set from the projects table on every run — every
managed app is maintained automatically, forever.

**Lesson.** Reconciliation input must be *derived from state*, never maintained in parallel with it. A
hand-maintained list adjacent to a database is a drift generator.

---

## 5. Monotonic state, enforced by a helper rather than by discipline

**Symptom.** A live, fully-deployed app silently regressed to an earlier onboarding step, discarding its
recorded go-live status — twice, on two different apps, in one session.

**Root cause.** Each wizard step's finaliser did an unguarded `UPDATE projects SET step = '<literal>'`.
Most steps are deliberately re-visitable. Re-running an *earlier* step after a *later* one had succeeded
stomped the recorded position backwards, so the wizard re-offered work that was already done.

**First fix (insufficient).** Guarded the one step that had broken.

**Proper fix.** One `advance_step()` helper that writes only if the new step is at or after the current
one in the canonical order, applied to every call site — about fifteen of them. Two genuine "undo" paths
were deliberately left unguarded.

**Lesson.** If a rule must hold at fifteen call sites, it needs to be a function, not a convention. The
first fix treated the symptom because I patched where it broke rather than where it *could* break.

---

## 6. A resource allocator with no knowledge of its own limit

**Symptom.** An app's Celery worker failed with `DB index is out of range`. It had been allocated Redis
database indices 16 and 17 on a server configured with 16 databases (valid range 0–15).

**Root cause.** The allocator was `MAX(index) + 1`, unconditionally. A long session of creating and
destroying test apps had freed up most low indices, but the counter kept climbing past the real ceiling
regardless.

**Fix, part one.** Prefer reclaiming the lowest genuinely-free index; refuse rather than allocate at or
beyond the configured maximum. Index 0 is excluded from reclamation — it's the protocol default a bare
client connects to, not a safely reusable slot.

**Fix, part two — the fix broke its callers.** Three call sites computed `cache_index = broker_index + 1`,
correct *only* under the old always-increment behaviour. Once indices could be reclaimed
non-consecutively, that arithmetic could hand out an index already belonging to a different app — exactly
the collision the ledger exists to prevent. The same sites also did bare `INSERT`s that now hit unique
constraint violations when a "free" index was an existing row.

**Fix.** The allocator takes an `exclude` set so a caller needing two indices requests the second one
properly, never by arithmetic. Every ledger write became an upsert.

**Lesson.** Changing a function's *guarantees* is a breaking change even when the signature is
unchanged. Callers encode assumptions you didn't write down.

---

## 7. Choosing the wrong field as a proxy

**Symptom.** An app became permanently stuck. "Delete certificate" fired a real deletion for a
certificate that had never been issued, failed, and "delete permanently" then refused to run because a
field was still set — with no path to clear it, because the only clearing path required the deletion to
succeed.

**Root cause.** "Does this app have a certificate?" was answered with "is a subdomain recorded?" But the
subdomain is recorded during *configuration*, long before issuance is even attempted. An app that never
reached the go-live phase has a subdomain and no certificate.

**Compounding bug.** The expiry field was also being wiped to `NULL` on a *successful* no-op renewal.
When the certificate authority decides a certificate isn't due, it exits successfully and prints
"not yet due for renewal" — with no expiry line. The parser found no match and wrote `NULL`, destroying
a perfectly good recorded date. Recovering it required a forced re-issuance.

**Fix.** Use the expiry date — set *only* by a genuinely successful issuance — as the existence proxy.
Never write `NULL` on a regex non-match; a non-match means "no new information," not "the old value is
gone."

**Lesson.** When picking a field as a proxy for an event, choose one that *only* that event writes. And
`if match else None` is a bug whenever `None` means "erase."

---

## 8. A regex that matched more than it should

**Symptom.** Caught in testing, not production. A real vendor API credential in a repo's
`.env.example` was silently replaced with random bytes.

**Root cause.** Secret-key auto-generation matched `(^|_)SECRET_KEY$` — the same suffix style used
elsewhere for `ALLOWED_HOSTS` and `DEBUG`. But `SECRET_KEY` is a far more overloaded suffix among vendor
credential names: it also matches `STRIPE_SECRET_KEY`. The variable was auto-filled with a random token
and never shown on the form for the operator to supply the real value.

**Why it's nasty.** The app would boot successfully with garbage payment credentials. Nothing looks
wrong until a real payment fails.

**Fix.** An exact-match allowlist of known framework key names, not a suffix pattern.

**Lesson.** Before using a suffix or substring pattern for anything consequential, test it against real
third-party names. "Looks like a framework variable" is not a safe inference.

---

## 9. A scheduler's idle poll as an unbounded cost

**Symptom.** A scheduler service crash-looped until systemd gave up
(`Start request repeated too quickly`), and logging into the app returned HTTP 500.

**Investigation.** Both symptoms traced to one cause, confirmed by connecting from the app's own
environment:

```
OperationalError: connection to server at "ep-example-pooler.REGION.aws.neon.tech", port 5432
failed: ERROR: Your account or project has exceeded the compute time quota.
```

The managed Postgres was refusing every connection. That explains both symptoms:

- The scheduler uses a **database-backed** schedule, so it cannot start without Postgres. The Celery
  *worker* stayed healthy throughout, because it only needs Redis — which is what made this look like a
  scheduler-specific bug rather than a database outage.
- Login is a database operation. No database, no authentication, HTTP 500.

**Root cause of the quota exhaustion.** The database-backed scheduler polls Postgres every five seconds,
permanently. The database is serverless and bills by compute-time, autosuspending when idle — so a
five-second poll means it *never* idles. The scheduler's "idle" state was continuous billable load.

**Fix.** Make the poll interval configurable and default it high enough that the database can suspend
between checks. The trade-off is that schedule edits take up to that interval to be noticed, which is
fine at this scale.

**Honest limitation.** This removes the constant drain but not the monthly cap. If the app's periodic
tasks genuinely hit the database around the clock, the ceiling still applies; removing it means a paid
tier or self-hosted Postgres.

**Lesson.** "Idle" is component-specific. A polling loop is idle from the poller's perspective and fully
active from the billing system's. When you adopt a serverless resource, audit everything that touches it
on a timer.

---

## Cross-cutting observations

**Real dependencies find real bugs.** Four of the configuration bugs above exist only because testing
used a real private repository and a real managed database instead of a disposable fixture. A synthetic
test would have had no `&` in its connection string, no vendor-prefixed secret key, and no pre-existing
stale rows.

**Silence is the common thread.** Nearly every incident here involved a component doing something
reasonable-looking instead of failing loudly: falling back to a default broker, honouring the last
duplicate key, backgrounding at an `&`, writing `NULL` on a non-match. Loud failures get fixed in
minutes. Silent ones run for days.

**An unexplained reboot is not automatically a compromise.** One incident began as "am I hacked?" The
check — boot history, failed auth attempts, `authorized_keys` mtime, unfamiliar accounts, listening
sockets, process ownership — found a hypervisor-level reset with no in-guest trigger and no indicators
of compromise. The real finding was operational: the panel had no service unit and didn't come back after
a reboot. Verify with evidence before concluding in *either* direction.
