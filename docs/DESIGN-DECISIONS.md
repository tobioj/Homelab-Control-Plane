# Design decisions

Short, ADR-style notes: what was chosen, what was rejected, and what it cost. Several record decisions that
were **reversed**; those are the most useful ones.

**Control Panel:**
[separate planes](#separate-the-control-plane-from-the-data-plane) ·
[concrete units](#concrete-units-over-template-units) ·
[preview, confirm, execute](#preview-confirm-execute-for-every-mutation) ·
[two authorisation layers](#two-independent-layers-of-authorisation) ·
[ask, don't guess](#ask-dont-guess) ·
[variable names per repo](#read-variable-names-from-each-repository) ·
[SSH never raises](#the-ssh-layer-never-raises) ·
[quote everything](#quote-every-generated-value) ·
[self-healing commands](#compound-commands-clean-up-after-themselves) ·
[honest proxies](#use-a-field-only-the-event-itself-writes) ·
[derive from state](#derive-reconciliation-input-from-state) ·
[monotonic steps](#monotonic-state-through-one-helper) ·
[automated renewal](#automated-renewal-as-a-narrow-exception) ·
[no SPA](#server-rendered-html-no-spa) ·
[SQLite](#sqlite-as-the-single-source-of-truth)

**Pushlane CI:**
[test before GitHub](#test-before-github-not-after) ·
[stay on LTS](#stay-on-the-lts-release) ·
[strict lanes](#strict-lanes-through-per-account-identities) ·
[graceful drains](#capacity-changes-never-kill-jobs) ·
[a folder per lane](#one-action-folder-per-lane) ·
[one shared cache](#one-cache-server-isolated-per-repository) ·
[tested agent docs](#documentation-that-is-executed-as-a-test) ·
[a GitHub App](#a-github-app-not-personal-tokens)

---

## Control Panel

### Separate the control plane from the data plane

**Decision.** The deploy tool runs on a different VM from the apps it deploys.

**Why.** A deploy tool is most dangerous while it deploys. On the same host as its targets, a bad unit file, a
full disk or a failed `nginx -t` can take down the very thing you need to fix it. Apart, the recovery path is
always there.

**Bonus.** Secrets get a natural home. The control plane holds them encrypted; the data plane receives only
the one `.env` each app needs. No single host holds both the decryption key and every app's plaintext.

**Cost.** Every operation is a network round trip that can fail in network-shaped ways, which is why the SSH
layer never raises.

### Concrete units over template units

**Decision.** Generate a concrete unit per app (`gunicorn@<app>.service` as a real file name) instead of leaning
on the shared `%i` template.

**Context.** The host already had template units that the first app resolved through. Changing them to support
a second framework would have changed behaviour for that app.

**Why it works.** systemd prefers an exact file name over a template, so concrete files are purely additive: the
existing app resolves exactly as before, and each new app gets its framework's command line baked in.

**Rejected.** Parameterising the shared template further, which would make one file carry every framework's
differences and let one mistake hit every app at once.

### Preview, confirm, execute for every mutation

**Decision.** No mutating action runs from a request handler. Each becomes a `pending_actions` row, shown as the
literal command, and runs only on explicit confirmation.

**Why.** The tool exists to prevent silent mistakes; one that executes silently defeats itself. Showing the
exact command also makes it teachable: you can see what it would have typed.

**First refinement: secrets.** Writing an `.env` doesn't fit, because its "command" would contain decrypted
values that must never reach `pending_actions` or the audit log. Instead of exempting it, the pipeline was split
into `load_pending_for_confirm()` and `record_result()`, so the `.env` write reuses both halves with a different
mechanism (SFTP) and stores a redacted preview listing variable names only.

**Second refinement: a race.** The first confirm was select-then-check with no atomic claim, so two quick submits
could both see `pending` and both run. Certificate issuance takes up to a minute with no progress bar, which
invites exactly that. A `claimed_at` column now makes the claim atomic.

> A new `status` value was tried first and failed at once: the column has a `CHECK` constraint, and SQLite can't
> change one without rebuilding the table. A nullable column through the existing migration helper was the
> better shape anyway.

### Two independent layers of authorisation

**Decision.** Scoped `sudoers` on the target host **and** an application-level denylist in the panel.

**Why both.** `sudoers` can only match command strings. `systemctl stop gunicorn@*` matches every app; it can't
say "every app except the important one". So protected apps are refused in the application, before a command
exists.

**Verified.** All three destructive operations against the protected app through the real API: three clean
refusals, zero `pending_actions` rows.

**Extended.** Protecting a *name* doesn't stop a harmlessly named test app from being given the production
app's domain and requesting a real certificate for it. Subdomain ownership is now checked when a subdomain is
recorded and again right before every action that touches DNS, certificates or the proxy.

### Ask, don't guess

**Decision.** The operator always sets the framework, app folder and entrypoint by hand.

**History.** The wizard used to detect them with a recursive search. That was walked back **twice**: one
repository can hold several unrelated projects, and only the operator knows which is meant. Detection that is
right 90% of the time produces a confidently wrong deployment the other 10%.

**What detection may still do.** Search and offer. Dependency files, remote branches and folders are discovered
and presented as choices; the tool never picks one.

**The same rule for error messages.** Failure hints cover a short list of *grounded* patterns, failures actually
seen or documented behaviour of the tool. Anything else gets no hint, and the raw output is always shown.
Speculative advice for failures nobody has seen is worse than none.

### Read variable names from each repository

**Decision.** Parse each repository's own `.env.example` to learn which names *that* project reads, and write
those.

**Why.** One project read `DJANGO_ALLOWED_HOSTS`, another `ALLOWED_HOSTS`. The wrong name doesn't error; the
framework quietly uses its default, and the failure shows up later as a puzzling `400`.

**Related.** Whether a variable is *required* is decided by finding where the source reads it without a default.
An early substring check for `"default"` marked a required variable optional because an unrelated dictionary key
on the same line contained that word.

### The SSH layer never raises

**Decision.** `ssh_client.run()` returns a result for every outcome, including timeouts (`124`) and connection
failures (`255`).

**Why.** An early version let a timeout escape, and a slow dependency install turned into a 500 that lost the
operator's place in the wizard over something that wasn't even an error. In a deploy tool, remote failures are
expected states, not exceptions.

**Consequence.** Callers check exit codes; there is no exception to forget. The default timeout is generous
(300 s) because real installs are slow.

### Quote every generated value

**Decision.** Every value written into a generated `.env` goes through `shlex.quote()`.

**Why.** A connection string containing `&` was cut off when the file was `source`d
([incident #3](INCIDENTS.md#3-a-value-cut-off-at-an-ampersand)). Both readers of the file, systemd's parser and
the shell, treat single-quoted values literally.

**Rule.** Any new code that writes an env-style file quotes the same way.

### Compound commands clean up after themselves

**Decision.** A command that links a config into an enabled folder and then validates must undo the link if
validation fails:

```
ln -sf <site> <enabled> && ( nginx -t || ( rm -f <enabled> && exit 1 ) ) && reload
```

**Why.** `ln -sf` succeeds whether or not the config is valid. A plain `&&` chain that stops at validation
leaves a broken link behind, which then fails validation, and so every future reload, **for every other app on
the host**. Reproduced live: one app's failed install made the whole proxy config invalid.

**Generalised.** In a compound privileged command, ask what the earlier steps leave behind when a later one
fails. Guarding the last step isn't the same as being safe.

### Use a field only the event itself writes

**Decision.** "Does this app have a certificate?" is answered by the expiry-date field, which only a successful
issuance writes.

**Rejected.** "Is a subdomain recorded?", which is set during configuration, long before issuance
([incident #7](INCIDENTS.md#7-the-wrong-field-as-a-proxy)).

**Also rejected.** Reading the certificate folder. It is root-only `0700`; the deploy user can't even test that it
exists. Granting that would widen the privilege boundary to answer a question the database already answers.

### Derive reconciliation input from state

**Decision.** The DNS updater builds its domain list from the projects table on every run.

**Rejected.** The original hand-typed list in a cron script, which drifted and caused an outage where every
service was healthy and two apps were simply unreachable
([incident #4](INCIDENTS.md#4-dns-drift-from-a-hand-typed-list)).

**Generalised.** A periodic job that acts on "all the things" must list them from the source of truth. A
parallel list is a drift generator on a delay fuse. (Pushlane's lane sync follows the same rule.)

### Monotonic state through one helper

**Decision.** A project's wizard position only moves forward, enforced by one `advance_step()` used at every call
site.

**Why not discipline.** Discipline was tried. Fifteen call sites each did an unguarded write, and two live apps
regressed in one session. A rule that must hold in fifteen places is a function.

**Exceptions are explicit.** Two genuine "undo" paths write backwards on purpose and say so in a comment.

### Automated renewal as a narrow exception

**Decision.** Certificate renewal runs on a timer without confirmation, the only exception to preview and
confirm.

**Why it's acceptable.** It only touches apps whose certificate a human already approved, runs the same command
shape that was reviewed, can't create, delete or reconfigure anything, and every run is audited under its own
actor name so automated actions stay separate from human ones.

**Where it runs.** On the control plane, which keeps the DNS provider's token in one place instead of on the app
host.

### Server-rendered HTML, no SPA

**Decision.** Jinja2 templates, with htmx where partial updates help.

**Why.** A single-operator internal tool doesn't justify a build pipeline, a dependency tree or a second
language. Plain form posts also mean the preview and confirm flow has no client-side state to drift from the
pending action it represents.

**Where a little JavaScript earned its place.** Adding and removing rows in the variable editor, a global
"working…" state on submit buttons (most actions are multi-second SSH round trips), and log panes that follow the
tail only when the reader is already at the bottom.

### SQLite as the single source of truth

**Decision.** One SQLite file holds projects, the Redis ledger, pending actions, encrypted secrets, health and the
audit log.

**Why it fits.** One writer, low volume, trivial backup, nothing extra to supervise; and a control plane that
depended on a database server it also deploys would be circular.

**Schema changes.** An idempotent `_ensure_column()` helper at startup instead of a migration framework. Adequate
at this scale, with one sharp edge: `CHECK` constraints can't be altered in place, which is why the atomic claim
became a nullable column rather than a new status.

---

## Pushlane CI

### Test before GitHub, not after

**Decision.** Developers push to Pushlane; a managed job forwards a commit to GitHub only after its tests pass.

**Why.** CI that runs after the push to GitHub reports a broken commit that is already public and already
pulled. Testing first means GitHub's history only ever contains commits that passed, private code is tested on
private hardware, and hosted CI minutes aren't spent.

**Cost.** A second remote to push to. The forwarder makes that cheap: it fast-forwards only, skips outdated
commits, copies LFS objects, and explains every refusal in actionable terms.

### Stay on the LTS release

**Decision.** Run Forgejo's long-term-support release, and upgrade from one LTS to the next.

**Why.** CI is infrastructure other people depend on; a few new features are worth less than a stable, patched
base. **Cost, accepted knowingly:** job summaries (`GITHUB_STEP_SUMMARY`) are not displayed until the next LTS.

### Strict lanes through per-account identities

**Decision.** Two lanes, priority and shared, with priority accounts kept *only* on the priority lane, enforced
by giving each lane one owner-scoped runner identity per account it serves.

**Why.** The goal is that the shared lane is always available to everyone else. Forgejo has no way to exclude
accounts from an instance-wide runner, and labels would have meant editing every user's workflows.

**Cost.** Accounts must be registered per lane. A lane sync on the host keeps that automatic: new accounts and
organisations are added, deleted ones dropped. An identity is deleted from Forgejo only once its lane runs a
config without it, and a lane is never configured with an identity Forgejo no longer has, because then it
wouldn't start.

### Capacity changes never kill jobs

**Decision.** A lane's shutdown timeout equals the job time limit; applying a new setting restarts the lane
gracefully.

**Why.** Concurrency gets retuned as the workload changes, and nobody should lose a 40-minute test run to it.
With small slot counts the drain costs almost nothing: a full lane couldn't have started new work anyway.

### One action folder per lane

**Decision.** Each lane keeps the actions it downloads (`actions/checkout`, ...) in its own folder.

**Why.** A runner serialises action fetches only among its own jobs. Two lanes running as the same user shared a
default folder, and jobs starting together on both failed with `shallow.lock: File exists`. The fix was
reproduced first on throwaway runners, then verified the same way.

### One cache server, isolated per repository

**Decision.** Both lanes use one Actions cache server; entries are partitioned by repository.

**Why.** With strict lanes, a project can run on either lane over time (a plan change, an organisation repo).
Two caches would mean cold starts on every switch. Per-repository isolation keeps one project from reading or
poisoning another's cache.

### Documentation that is executed as a test

**Decision.** Every command in `agents.md` (the reference AI agents read) is executed by a test against a
temporary account before publishing, and checked against the status code the page documents.

**Why.** Agents follow docs literally. A stale example costs an agent's whole session, and nobody notices until a
user complains. Running the docs turns drift into a failing test.

### A GitHub App, not personal tokens

**Decision.** One GitHub App (contents: read, commit statuses: write, push events) for syncing and statuses,
created through GitHub's manifest flow.

**Why.** Deploy keys can push but can't post statuses or receive webhooks. A personal token would be tied to one
person and expire. An App is installed per repository by its owner, mints short-lived tokens limited to that one
repository, and its private key goes straight from GitHub to the server without anyone copying it.

**Guardrails.** Sync only fast-forwards or creates branches, never forces or deletes, and leaves diverged
branches alone; a bad webhook signature gets `403`, never `401`, so the edge's ban rules can't lock GitHub out.
