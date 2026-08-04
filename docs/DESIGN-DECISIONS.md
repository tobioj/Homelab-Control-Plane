# Design decisions

ADR-style notes: what was chosen, what was rejected, and what the decision cost. Several entries record
decisions that were **reversed** — those are the useful ones.

---

## Separate the control plane from the data plane

**Decision.** The deploy tool runs on a different VM from the applications it deploys.

**Why.** A tool that deploys applications is at its most dangerous while deploying. If it shares a host
with its targets, a bad unit file, an exhausted port, a filled disk or an `nginx -t` failure can take
down the thing you need in order to fix it. Separating them means the recovery path is always available.

**Secondary benefit.** It gives secrets a natural home. The control plane holds them encrypted; the data
plane receives only the single `.env` each app needs. There is no host that holds both the decryption key
and every application's plaintext configuration.

**Cost.** Every operation is now a network round trip, and every one can fail in network-shaped ways.
This is why the SSH layer never raises — see below.

---

## Concrete units over template units

**Decision.** For each app, generate a concrete systemd unit (`gunicorn@<app>.service` as a literal
filename) instead of relying on the shared `%i` template unit.

**Context.** The host already had true template units that the original application resolved through.
Editing those to support a second framework would have changed behaviour for the existing app.

**Why it works.** systemd resolves an exact filename match before falling back to a template, so adding
concrete files is purely additive — the pre-existing app keeps resolving exactly as before. Each new app
gets its framework-correct command line baked in.

**Rejected alternative.** Parameterising the shared template further. It would have made one file
responsible for every framework's differences, and any mistake would affect every app at once.

---

## Preview → confirm → execute for every mutation

**Decision.** No mutating action executes from a request handler. Each becomes a row in
`pending_actions`, rendered as the literal command, and runs only on explicit confirmation.

**Why.** The tool exists to prevent silent mistakes; a tool that silently executes is self-defeating.
Showing the exact command also makes the tool teachable — you can see what it would have typed.

**Refinement forced by reality.** Writing an `.env` doesn't fit: the "command" would contain decrypted
secrets, which must never reach `pending_actions` or the audit log. Rather than exempt it, the pipeline
was split into `load_pending_for_confirm()` and `record_result()` so the `.env` write reuses both halves
with a completely different execution mechanism (SFTP), and stores a redacted placeholder — variable
names only — for review.

**Refinement forced by a race.** The initial confirm was a select-then-check with no atomic claim. Two
concurrent submits could both observe `pending` and both execute. Certificate issuance takes 40–60
seconds with no progress indicator, which actively invites a second click. Fixed with an atomic claim
via a dedicated `claimed_at` column.

> Implementation note: a new `status` enum value was tried first and failed immediately — the column has
> a `CHECK` constraint, and SQLite can't alter one without a full table rebuild. A nullable column added
> through the existing migration helper was the right shape anyway.

---

## Two independent layers of authorisation

**Decision.** Scoped `sudoers` on the target host **and** an application-level denylist in the panel.

**Why both.** A `sudoers` rule can only pattern-match a command string. `systemctl stop gunicorn@*`
matches every application — it structurally cannot express "every app except the important one." So the
protected-app check happens in the application, before a command is ever built.

**Verification.** Attempting all three destructive operations against the protected app through the real
API produced three clean refusals and zero `pending_actions` rows.

**Extension.** A later gap showed the denylist needed a second dimension: a *subdomain* uniqueness check.
Protecting a *name* doesn't stop a harmlessly-named test app from being configured with the production
app's domain and then requesting a real certificate for it. The check now runs both where a subdomain is
first recorded and again immediately before every action that touches DNS, certificates or the proxy.

---

## Ask, don't guess — and the two times auto-detection was removed

**Decision.** Framework, application directory and entrypoint are set manually by the operator. Always.

**History.** The wizard originally auto-detected these with a recursive filesystem search. This was
walked back **twice**, because a repository can contain multiple unrelated projects and only the operator
knows which one is meant. Auto-detection that's right 90% of the time produces a confidently wrong
deployment 10% of the time.

**What auto-detection is still allowed to do.** *Search* and *offer.* Candidate dependency files, remote
branches and repository directories are all discovered automatically and presented as explicit choices.
The tool never picks one.

**The same discipline applied to error messages.** Failure interpretation covers a deliberately short
list of *grounded* patterns — failures actually observed, or universal documented behaviour of the
underlying tool. Everything else returns nothing, and the raw output is always shown regardless.
Speculative error text for failures nobody has seen is worse than no text.

---

## Detect variable *names* per repository

**Decision.** Parse each repository's own `.env.example` to learn which variable names *that* project
uses, and write those.

**Why.** One real repository used `DJANGO_ALLOWED_HOSTS`; another used `ALLOWED_HOSTS`. Writing the wrong
name does not error — the framework silently uses its own default. The failure surfaces later as a
confusing `400`, far from its cause.

**Related.** Determining whether a variable is *required* greps the repository's own source for the
variable being read without a default. An early version used a naive substring check for `"default"` and
mislabelled a genuinely-required variable as optional because an unrelated dictionary key on the same
line contained that word.

---

## The SSH layer never raises

**Decision.** `ssh_client.run()` returns a result dictionary for every outcome, including timeouts
(`124`) and connection failures (`255`).

**Why.** An early version let a timeout propagate, and a slow dependency install crashed a request with
a 500 — losing the operator's place in the wizard for something that wasn't even an error. Remote
failures are *expected* states in a deploy tool, not exceptions.

**Consequence.** Callers must check exit codes; there's no exception to forget to catch. The default
timeout is generous (300s) because real installs are slow.

---

## Quote every generated value

**Decision.** Every value written into a generated `.env` is `shlex.quote()`-wrapped.

**Why.** See [incident #3](INCIDENTS.md#3-a-configuration-value-truncated-at-an-ampersand). A connection
string containing `&` was silently truncated when the file was `source`d. Both consumers of the file —
systemd's parser and the shell — treat single-quoted values as literal.

**Rule.** Any new code that writes an env-style file must quote the same way.

---

## Self-healing compound commands

**Decision.** Commands that link a config file into an enabled directory and then validate must clean up
after themselves on failure:

```
ln -sf <site> <enabled> && ( nginx -t || ( rm -f <enabled> && exit 1 ) ) && reload
```

**Why.** `ln -sf` succeeds regardless of whether the config it enables is valid. A plain `&&` chain that
fails at validation leaves a broken symlink behind — which then blocks validation, and therefore any
future reload, **for every other application on the host**. Reproduced live: a failed operation for one
app made the whole proxy config invalid.

**Lesson generalised.** In a compound privileged command, ask what the intermediate steps leave behind if
a later step fails. Gating the last step is not the same as being safe.

---

## Choose proxies that only the event you care about writes

**Decision.** "Does this app have a certificate?" is answered by the expiry date field, which is written
only by a genuinely successful issuance.

**Rejected.** "Is a subdomain recorded?" — recorded during configuration, long before issuance is
attempted. See [incident #7](INCIDENTS.md#7-choosing-the-wrong-field-as-a-proxy).

**Also rejected.** Reading the certificate directory directly. It is root-owned `0700`, and the deploy
user cannot read it at all — not even to test existence. No `sudoers` grant covers a bare existence
check, and adding one would widen the privilege boundary to answer a question the database can already
answer.

---

## Derive reconciliation input from state

**Decision.** The DNS updater builds its domain list from the projects table on every run.

**Rejected.** The pre-existing approach: a hand-typed list in a cron script. It drifted from reality and
caused a real outage in which every service was healthy and two apps were simply unreachable. See
[incident #4](INCIDENTS.md#4-dns-drift-between-a-hand-typed-list-and-reality).

**Generalised.** Any periodic job that acts on "all the things" must enumerate them from the source of
truth. A parallel list is a drift generator with a delay fuse.

---

## Monotonic state transitions via a helper

**Decision.** Onboarding position only ever moves forward, enforced by one `advance_step()` function
used at every call site.

**Why not discipline.** Discipline was tried. Fifteen call sites each did an unguarded write, and two
different live apps regressed within one session. A rule that must hold in fifteen places is a function.

**Exceptions are explicit.** Two genuine "undo" paths write backwards deliberately, and are commented as
such.

---

## Automated renewal as a deliberate, narrow exception

**Decision.** Certificate renewal runs on a timer without a confirmation step — the only exception to
preview-and-confirm.

**Justification.** It only ever touches an app whose certificate a human already approved once; it runs
the same command shape that was already reviewed; it cannot create, delete or reconfigure anything; and
every run is audited under a distinct actor name so automated actions are separable from human ones.

**Where it runs.** On the control plane, not the target host — which keeps the DNS provider's API token
in exactly one place instead of persisting it on the app host.

---

## Server-rendered HTML, no SPA

**Decision.** Jinja2 templates with htmx for the few places that benefit from partial updates.

**Why.** A single-operator internal tool doesn't justify a build pipeline, a dependency tree, or a second
language. Server-rendered pages also mean the preview/confirm flow is plain form posts — no client-side
state to get out of sync with the pending action it represents.

**Where a little JavaScript was warranted.** Dynamic add/remove rows on the environment-variable editor,
a global "working…" state on submit buttons (most actions are multi-second SSH round trips with no
feedback), and log panes that follow the tail only when the reader is already at the bottom.

---

## SQLite as the single source of truth

**Decision.** One SQLite file holds projects, the Redis ledger, pending actions, encrypted secrets,
health history and the audit log.

**Why it's correct here.** One writer, low volume, trivial backup, no separate service to supervise —
and a control plane that depends on a database server it also deploys would be circular.

**Schema evolution.** An idempotent `_ensure_column()` helper runs at startup rather than a migration
framework. Adequate at this scale, with one sharp edge worth recording: `CHECK` constraints can't be
altered in place, so constrained columns need a rebuild — which is why an atomic claim became a new
nullable column rather than a new status value.
