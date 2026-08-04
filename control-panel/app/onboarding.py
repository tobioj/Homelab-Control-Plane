"""Onboarding wizard, Phase 1 (§3.1 steps 1-4 of the infra brief): repo
intake, deploy key generation, clone, framework/dependency detection, venv +
install. Unlike decommission (one compound command), onboarding has real
waiting points — add a deploy key to GitHub, wait for the user to confirm —
so wizard state is persisted on the projects row itself via onboarding_step,
rather than everything happening in one preview/confirm action.

Deploy keys, clone, and dependency install still go through the same
actions.create_preview_row() + confirm_and_run() pipeline as every other
mutating action in this tool — same audit trail, same expiry, same review-
before-execute guarantee.
"""

import os
import re
import secrets
import shlex

from app.db import get_conn, next_free_redis_index
from app.audit import log_action
from app.config import settings
from app import actions, safety, ssh_client, crypto

# Well-known Redis-URL-shaped env var names, matching primary-store's actual
# convention (see BRIEF.md §2.2) — these are NEVER hand-typed by the operator
# in the env-var form. They're governed entirely by the ledger allocation in
# configure_services_and_redis(), because letting someone type a raw index
# is exactly the brief §4 bug #2 collision this tool exists to prevent.
BROKER_VAR_NAMES = {"REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"}
CACHE_VAR_NAMES = {"CACHE_REDIS_URL", "CACHE_URL"}

SSH_URL_RE = re.compile(r"^git@([\w.\-]+):([\w.\-]+)/([\w.\-]+?)(\.git)?$")
HTTPS_URL_RE = re.compile(r"^https://([\w.\-]+)/([\w.\-]+)/([\w.\-]+?)(\.git)?/?$")
CREDENTIALS_IN_URL_RE = re.compile(r"^https://[^/@]+@")

# Dependency-file search is scoped to the operator-confirmed app_subdir,
# never the whole repo — a repo-wide search can't tell which of several
# unrelated projects in a monorepo is the right one, only the operator can.
# Within that already-narrowed location, a modest depth easily covers a
# nested requirements/ folder.
DEPENDENCY_SEARCH_MAX_DEPTH = 3

# Single source of truth for wizard navigation (the breadcrumb + "go back"
# links). Each tuple is (onboarding_step value once this step COMPLETES,
# short label, kind, route suffix under /wizard/<id>/...). `kind` decides
# how the breadcrumb offers to revisit a completed step:
#   "form"    — a GET page exists, freely re-visitable and re-submittable,
#               no backend changes needed (confirmed live: these routes
#               never check onboarding_step at all).
#   "action"  — a bare POST that re-runs a naturally idempotent remote
#               command (pip install, migrate, systemd cp, enable+start,
#               etc. — confirmed none of these guard against re-running).
#   "reclone" — clone specifically: git refuses to clone into a non-empty
#               directory, so revisiting this needs the dedicated
#               delete-then-reclone flow, not a bare re-POST.
#   "redis"   — services-redis specifically: configure_services_and_redis()
#               explicitly refuses once indices are allocated, so the form
#               itself (not the breadcrumb) offers a free-and-reconfigure
#               path when it detects that case.
#   None      — no dedicated route to revisit (intake/awaiting_deploy_key
#               are shown inline on the main wizard status page already).
WIZARD_STEPS = [
    ("intake", "Repo intake", None, None),
    ("awaiting_deploy_key", "Deploy key", None, None),
    ("cloned", "Clone", "reclone", "clone"),
    ("located", "Locate & framework", "form", "locate"),
    ("entrypoint_set", "Entrypoint", "form", "entrypoint"),
    ("dependency_file_detected", "Dependency file", "form", "dependency-file"),
    ("deps_installed", "Install deps", "action", "setup"),
    ("services_configured", "Services & Redis", "redis", "services-redis"),
    ("env_vars_set", "Environment vars", "form", "env-vars"),
    ("subdomain_set", "DuckDNS subdomain", "form", "subdomain"),
    ("env_written", "Write .env", "action", "write-env"),
    ("migrated", "Migrate", "action", "migrate"),
    ("units_installed", "Install units", "action", "install-units"),
    ("services_started", "Start services", "action", "start"),
    ("onboarding_complete", "Health check", "action", "health-check"),
    ("dns_checked", "DNS check", "form", "dns-check"),
    ("cert_issued", "Certbot", "action", "certbot-start"),
    ("nginx_installed", "Install nginx", "action", "nginx-install"),
    ("https_live", "HTTPS health check", "action", "https-health-check"),
]
WIZARD_STEP_NAMES = [s[0] for s in WIZARD_STEPS]

# Groups WIZARD_STEPS into the three phases this project's own docs (and
# the operator) already talk about — Phase 1 (repo intake through deps
# installed), Phase 2 (services/env/deploy through the first health check),
# Phase 3 (DuckDNS + certbot + nginx + HTTPS). Purely a display grouping
# for the breadcrumb (_wizard_breadcrumb.html) — WIZARD_STEP_NAMES stays
# the single flat list every index-based comparison in this file actually
# uses; this is never consulted for step-ordering logic, only labels.
WIZARD_PHASES = [
    (0, "Phase 1 — Setup"),
    (7, "Phase 2 — Configure & deploy"),
    (15, "Phase 3 — Go live (DuckDNS + HTTPS)"),
]


def advance_onboarding_step(conn, project_id: int, new_step: str):
    """The ONLY safe way for a wizard step's finalize function to set
    onboarding_step — never a bare `UPDATE projects SET onboarding_step =
    '<literal>'`. Real bug hit live TWICE this session: a bare UPDATE
    unconditionally overwrites onboarding_step regardless of how far the
    project has actually progressed, so revisiting/redoing an EARLIER step
    (every "action"/"form" kind step in WIZARD_STEPS is freely re-visitable
    /re-postable by design, via the breadcrumb) silently regresses a
    project that's already further along — wiping out real recorded
    progress (Phase 3's cert_issued/nginx_installed/https_live in
    particular) even though nothing on VM 100 itself changed. First caught
    in dns_cert.create_dns_check_and_mark (bug #23, fixed inline there
    before this shared helper existed), then found to be the SAME unfixed
    bug in onboarding.confirm_write_env — redoing the write-env step (to
    fix an unrelated .env duplication bug) regressed a real live project
    (app-alpha) from https_live all the way back to env_written mid-session.
    Every OTHER hardcoded onboarding_step UPDATE across this file and
    dns_cert.py had the identical latent bug, just not yet triggered live.
    Fixed everywhere at once via this one shared helper instead of
    patching each call site's own inline check.

    Deliberately NOT used by finalize_reclone() or free_services_and_redis()
    — those two are the real, intentional "undo" paths (clone-redo deletes
    the cloned code, Redis-reconfigure frees the ledger allocation), where
    regressing onboarding_step backward is the correct, desired behavior,
    not a bug to guard against."""
    current = conn.execute("SELECT onboarding_step FROM projects WHERE id = ?", (project_id,)).fetchone()["onboarding_step"]
    if current in WIZARD_STEP_NAMES and new_step in WIZARD_STEP_NAMES:
        if WIZARD_STEP_NAMES.index(new_step) < WIZARD_STEP_NAMES.index(current):
            return
    conn.execute("UPDATE projects SET onboarding_step = ? WHERE id = ?", (new_step, project_id))


def normalize_repo_url(url: str) -> str:
    """SSH form only, ever — this tool never constructs or stores a clone
    URL with an embedded token (brief §4 bug #9: a leaked PAT-in-URL had to
    be rotated once already)."""
    url = url.strip()
    if CREDENTIALS_IN_URL_RE.match(url):
        raise actions.ActionError(
            "URLs with embedded credentials (user:token@host) are never accepted — use a deploy key instead"
        )
    m = SSH_URL_RE.match(url)
    if m:
        return url if url.endswith(".git") else url + ".git"
    m = HTTPS_URL_RE.match(url)
    if m:
        host, owner, repo = m.group(1), m.group(2), m.group(3)
        return f"git@{host}:{owner}/{repo}.git"
    raise actions.ActionError(f"Unrecognized repo URL format: {url!r}")


def create_draft(actor: str, name: str, repo_url: str, branch: str, vm_ip: str) -> int:
    safety.assert_project_not_protected(name)
    # Every filename Phase 3 (and units-install) ever derives from a project
    # name is exactly `name` itself or `name` with a fixed prefix/suffix
    # (e.g. `gunicorn@<name>.service`) — so blocking the bare name here is
    # sufficient to keep a project from ever being named after a shared
    # protected file (00-catchall, the %i unit templates) and later
    # colliding with it in /etc/nginx/sites-*/ or /etc/systemd/system/.
    safety.assert_filename_not_protected(name)
    normalized_url = normalize_repo_url(repo_url)

    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
        if existing is not None:
            raise actions.ActionError(f"A project named '{name}' already exists")
        cur = conn.execute(
            """INSERT INTO projects (name, repo_url, branch, vm_ip, is_live, status, onboarding_step)
               VALUES (?, ?, ?, ?, 0, 'draft', 'intake')""",
            (name, normalized_url, branch or "main", vm_ip),
        )
        project_id = cur.lastrowid
    log_action(actor=actor, action="wizard_draft_created", target=name, details=normalized_url)
    return project_id


async def generate_deploy_key(project: dict) -> str:
    """Idempotent — if a key already exists for this project, just returns
    its existing public half rather than generating a new one."""
    key_path = f"/home/deploy/.ssh/deploy_{project['name']}"
    if not project["deploy_key_path"]:
        result = await ssh_client.run(
            f"ssh-keygen -t ed25519 -f {shlex.quote(key_path)} -N '' -C {shlex.quote('deploy-key-' + project['name'])}",
            host=project["vm_ip"],
        )
        if result["exit_status"] != 0:
            raise actions.ActionError(f"Key generation failed: {result['stderr']}")
        with get_conn() as conn:
            conn.execute("UPDATE projects SET deploy_key_path = ? WHERE id = ?", (key_path, project["id"]))
            advance_onboarding_step(conn, project["id"], "awaiting_deploy_key")
        log_action(actor="system", action="deploy_key_generated", target=project["name"])

    pub = await ssh_client.run(f"cat {shlex.quote(key_path + '.pub')}", host=project["vm_ip"])
    return pub["stdout"].strip()


def create_clone_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    if not project["deploy_key_path"]:
        raise actions.ActionError("Generate the deploy key and add it to GitHub before cloning")
    dest = f"/srv/apps/{project['name']}"
    key_path = project["deploy_key_path"]
    command = (
        f"GIT_SSH_COMMAND={shlex.quote('ssh -i ' + key_path + ' -o StrictHostKeyChecking=accept-new')} "
        f"git clone -b {shlex.quote(project['branch'])} {shlex.quote(project['repo_url'])} {shlex.quote(dest)}"
    )
    return actions.create_preview_row(actor, project, command, project["name"], "onboarding_clone")


def finalize_clone(project_id: int, actor: str):
    with get_conn() as conn:
        advance_onboarding_step(conn, project_id, "cloned")
    log_action(actor=actor, action="onboarding_clone_finalized", target=str(project_id))


async def check_reclone_readiness(project: dict) -> dict:
    """Read-only. `git clone` refuses into a non-empty directory, so
    revisiting the clone step after it already succeeded needs the cloned
    directory deleted first — this tells the operator exactly what that
    would take before they confirm anything. If deps were already
    installed, the venv (which lives inside the same directory) would go
    with it too — worth a loud warning, not a silent side effect."""
    dest = f"/srv/apps/{project['name']}"
    result = await ssh_client.run(f"test -d {shlex.quote(dest)} && echo yes || echo no", host=project["vm_ip"], timeout=15)
    exists = result["stdout"].strip() == "yes"
    return {
        "dir_exists": exists,
        "path": dest,
        "deps_were_installed": WIZARD_STEP_NAMES.index(project["onboarding_step"]) >= WIZARD_STEP_NAMES.index("deps_installed")
        if project["onboarding_step"] in WIZARD_STEP_NAMES else False,
    }


def create_reclone_preview(actor: str, project: dict) -> str:
    """Deletes the cloned directory so clone can be re-run cleanly with a
    corrected repo/branch — the same `rm -rf /srv/apps/<name>` shape as
    `decommission.create_delete_files_preview`, but that function requires
    `status == 'decommissioned'` which doesn't apply to a project still
    mid-onboarding, so this is a distinct, narrower preview builder rather
    than a reuse of that one."""
    safety.assert_project_not_protected(project["name"])
    dest = f"/srv/apps/{project['name']}"
    command = f"rm -rf {shlex.quote(dest)}"
    return actions.create_preview_row(actor, project, command, project["name"], "onboarding_reclone")


def finalize_reclone(project_id: int, actor: str):
    """Back to awaiting_deploy_key — the deploy key itself is untouched (it
    lives outside /srv/apps/, generate_deploy_key's idempotency means it
    won't be regenerated), so the operator lands straight back at the
    'clone now' button, free to fix repo_url/branch first via the intake
    fields or just retry with what's already there."""
    with get_conn() as conn:
        conn.execute("UPDATE projects SET onboarding_step = 'awaiting_deploy_key' WHERE id = ?", (project_id,))
    log_action(actor=actor, action="onboarding_reclone_finalized", target=str(project_id))


BRANCH_NOT_FOUND_RE = re.compile(r"Remote branch (\S+) not found in upstream")


def interpret_action_failure(action_type: str, stderr: str) -> dict:
    """Best-effort, grounded ONLY in failure patterns actually seen or
    genuinely well-documented — not a guess-everything error-message
    library. Returns None if nothing recognized, in which case the caller
    just shows the raw stdout/stderr as it always has; this is additive
    framing on top of that, never a replacement for it.

    Two patterns for now, both specific to `onboarding_clone`:
    - branch-not-found: reproduced live, twice, against a real repo whose
      only branch is 'staging' while the wizard's intake form defaults
      the branch field to 'main'.
    - permission-denied (publickey): NOT personally reproduced this
      session, but this is the universal, well-documented OpenSSH/git
      failure text for "this key isn't trusted for this repo yet" —
      distinct from being a guess about THIS codebase's specific behavior.

    Extend this by adding another `action_type == "..."` branch with
    another real pattern once one is actually encountered — resist the
    urge to pre-populate speculative patterns for failures nobody has
    actually seen."""
    if action_type != "onboarding_clone":
        return None
    branch_match = BRANCH_NOT_FOUND_RE.search(stderr)
    if branch_match:
        return {
            "message": f"Branch '{branch_match.group(1)}' doesn't exist in this repository.",
            "needs_branch_fix": True,
        }
    if "Permission denied (publickey)" in stderr:
        return {
            "message": "The deploy key isn't trusted for this repo yet — either it hasn't been added on GitHub, or it was added to a different repo than this one. Double-check Settings → Deploy keys on the actual repo this project points at.",
            "needs_branch_fix": False,
        }
    return None


async def list_remote_branches(project: dict) -> list:
    """Read-only `git ls-remote --heads` via the project's own deploy key —
    powers the branch-not-found retry form with real branch names instead
    of asking the operator to guess. Same command shape used manually,
    live, to diagnose this exact failure earlier this session."""
    result = await ssh_client.run(
        f"GIT_SSH_COMMAND={shlex.quote('ssh -i ' + project['deploy_key_path'] + ' -o StrictHostKeyChecking=accept-new')} "
        f"git ls-remote --heads {shlex.quote(project['repo_url'])}",
        host=project["vm_ip"], timeout=30,
    )
    branches = []
    for line in result["stdout"].splitlines():
        parts = line.strip().split("refs/heads/")
        if len(parts) == 2:
            branches.append(parts[1])
    return branches


async def list_repo_directories(project: dict) -> list:
    """Real subdirectories found in the already-cloned repo on VM 100 (the
    clone step always precedes locate — see WIZARD_STEPS), so the operator
    can pick from an actual list instead of typing a path blind. This is
    NOT the auto-detection this file otherwise refuses to do (see
    set_location_and_framework's docstring) — it never picks a folder FOR
    the operator, it just shows what's really there, same "ask, don't
    guess" shape as list_remote_branches() showing real branches instead of
    making the operator guess one for the retry-clone form."""
    dest = f"/srv/apps/{project['name']}"
    # Excludes .git, venv/env-name dirs, __pycache__, and common build/static
    # output dirs — real, but never a legitimate app_subdir pick, so listing
    # them would just be noise the operator has to scroll past.
    result = await ssh_client.run(
        f"find {shlex.quote(dest)} -mindepth 1 -maxdepth 2 -type d "
        f"-not -path '*/.git*' -not -path '*/__pycache__*' -not -path '*/venv*' -not -path '*/.venv*' "
        f"-not -path '*/node_modules*' -not -path '*/staticfiles*' -not -path '*/static*' 2>/dev/null",
        host=project["vm_ip"], timeout=30,
    )
    paths = [p.strip() for p in result["stdout"].splitlines() if p.strip()]
    return sorted(p[len(dest):].lstrip("/") for p in paths)


def set_location_and_framework(project_id: int, actor: str, app_subdir: str, framework: str):
    """Fully manual, by design — no repo-wide search. A recursive find for
    manage.py can't tell which of several unrelated projects in a monorepo
    is the right one; only the operator knows which folder they actually
    mean. app_subdir is relative to the repo root ('' = repo root itself)."""
    if framework not in ("django", "fastapi"):
        raise actions.ActionError(
            "Automated deployment currently supports Django and FastAPI only. "
            "Pick FastAPI if it's an ASGI app served by uvicorn, or Django if it's WSGI — "
            "a correct systemd ExecStart can't be generated reliably for anything else."
        )
    app_subdir = app_subdir.strip().strip("/")
    with get_conn() as conn:
        conn.execute("UPDATE projects SET framework = ?, app_subdir = ? WHERE id = ?", (framework, app_subdir, project_id))
        advance_onboarding_step(conn, project_id, "located")
    log_action(actor=actor, action="location_and_framework_set", target=str(project_id), details=f"{framework} @ {app_subdir or '(repo root)'}")


def set_entrypoint(project_id: int, actor: str, entrypoint: str):
    """The wsgi/asgi module string (e.g. 'config.wsgi:application' or
    'main:app') — needed by deploy_templates.build_exec_start. No
    auto-suggestion, same reasoning as set_location_and_framework: asking
    beats guessing wrong silently."""
    entrypoint = entrypoint.strip()
    if not entrypoint:
        raise actions.ActionError("Entrypoint cannot be empty")
    with get_conn() as conn:
        conn.execute("UPDATE projects SET wsgi_asgi_entrypoint = ? WHERE id = ?", (entrypoint, project_id))
        advance_onboarding_step(conn, project_id, "entrypoint_set")
    log_action(actor=actor, action="entrypoint_set", target=str(project_id), details=entrypoint)


async def find_dependency_candidates(project: dict) -> list:
    """Returns every plausible dependency file found within the operator-
    confirmed app_subdir (never the whole repo — same monorepo reasoning as
    set_location_and_framework), path relative to the repo root. Never
    auto-picks one; some repos split requirements.txt / requirements-dev.txt
    / requirements/production.txt, and which is correct is a judgment call
    only the operator should make."""
    dest = f"/srv/apps/{project['name']}"
    search_root = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    result = await ssh_client.run(
        # Three ways a dependency file shows up: pyproject.toml anywhere;
        # a requirements*.txt filename anywhere; or ANY .txt file inside a
        # directory literally named requirements/ (e.g. requirements/base.txt,
        # requirements/production.txt — the filename itself doesn't start
        # with "requirements" in that layout, only the containing dir does).
        f"find {shlex.quote(search_root)} -maxdepth {DEPENDENCY_SEARCH_MAX_DEPTH} -not -path '*/.git/*' -type f "
        f"\\( -name pyproject.toml -o -name 'requirements*.txt' -o -path '*/requirements/*.txt' \\) 2>/dev/null",
        host=project["vm_ip"],
    )
    paths = [p.strip() for p in result["stdout"].splitlines() if p.strip()]
    return sorted(p[len(dest):].lstrip("/") for p in paths)


async def set_dependency_file(project: dict, actor: str, dependency_file_rel: str):
    """dependency_file is stored as a full path RELATIVE TO THE REPO ROOT,
    deliberately never used to (re-)derive app_subdir — a requirements file
    routinely lives in its own subfolder (e.g. backend/requirements/
    production.txt) that is NOT the app's working directory (backend/).
    app_subdir is set once, by set_location_and_framework, and nothing else
    is allowed to overwrite it."""
    dest = f"/srv/apps/{project['name']}"
    check = await ssh_client.run(f"test -f {shlex.quote(dest + '/' + dependency_file_rel)} && echo yes || echo no", host=project["vm_ip"])
    if check["stdout"].strip() != "yes":
        raise actions.ActionError(f"{dependency_file_rel} does not exist under {dest}")

    with get_conn() as conn:
        conn.execute("UPDATE projects SET dependency_file = ? WHERE id = ?", (dependency_file_rel, project["id"]))
        advance_onboarding_step(conn, project["id"], "dependency_file_detected")
    log_action(actor=actor, action="dependency_file_set", target=str(project["id"]), details=dependency_file_rel)


def create_setup_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    if not project["dependency_file"]:
        raise actions.ActionError("Dependency file not chosen yet")
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    dep_path = f"{dest}/{project['dependency_file']}"
    install_cmd = (
        f"{app_dir}/venv/bin/pip install {shlex.quote(os.path.dirname(dep_path))}"
        if project["dependency_file"].endswith("pyproject.toml")
        else f"{app_dir}/venv/bin/pip install -r {shlex.quote(dep_path)}"
    )
    command = f"/usr/local/bin/python3.11 -m venv {shlex.quote(app_dir + '/venv')} && {install_cmd}"
    return actions.create_preview_row(actor, project, command, project["name"], "onboarding_setup")


def finalize_setup(project_id: int, actor: str):
    with get_conn() as conn:
        advance_onboarding_step(conn, project_id, "deps_installed")
    log_action(actor=actor, action="onboarding_setup_finalized", target=str(project_id))


# ============================================================================
# Phase 2 (§3.1 steps 5-10, 14): services, Redis ledger allocation, env vars,
# writing the real .env, migrations, systemd install, start, health check.
# Phase 3 (DuckDNS + certbot) is deliberately NOT here — per the user's
# explicit instruction, that's a manual round-trip they run on their own PC;
# this tool only ever needs to surface what's needed and record what they
# report back. See CLAUDE.md §3 for the full reasoning.
# ============================================================================

def configure_services_and_redis(project_id: int, actor: str, uses_celery: bool, uses_celerybeat: bool, separate_cache_index: bool, celery_app: str = "config"):
    """Allocates 1 or 2 fresh Redis indices from the ledger (db.next_free_redis_index())
    and records the service selection — done together since which indices
    get labeled 'broker' vs 'cache' only matters once you know whether celery
    is even in the picture. Runs once per project: if redis indices are
    already allocated, refuses rather than silently leaking/reallocating.

    Real bug found live: `cache_idx = broker_idx + 1` assumed indices are
    always sequential, which was true under the old "always increment,
    never reuse" allocator but breaks now that next_free_redis_index() can
    return a RECLAIMED index — the slot right after a reclaimed index isn't
    necessarily free too, and blindly claiming it could silently steal an
    index a different project already has (exactly the brief §4 bug #2
    collision class this ledger exists to prevent). Fixed: request the
    cache index with the broker index passed as `exclude`, so it's
    guaranteed distinct without relying on transaction-commit ordering.
    Each claim uses INSERT ... ON CONFLICT(idx) DO UPDATE rather than a
    bare INSERT — also found live, a bare INSERT crashes with a UNIQUE
    constraint violation whenever next_free_redis_index() returns a
    RECLAIMED (already-existing) row instead of a genuinely new one."""
    with get_conn() as conn:
        project = conn.execute("SELECT redis_broker_index FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project["redis_broker_index"] is not None:
        raise actions.ActionError("Redis indices already allocated for this project")

    if uses_celerybeat and not uses_celery:
        raise actions.ActionError("Celery beat requires the Celery worker to also be enabled")

    # Store the Celery app module only when Celery is actually in use; NULL
    # otherwise. Falls back to "config" (Django convention) at every read site.
    celery_app_value = (celery_app.strip() or "config") if uses_celery else None

    broker_purpose = "Celery broker + result backend" if uses_celery else "cache (no celery on this project)"
    broker_idx = next_free_redis_index()
    cache_idx = next_free_redis_index(exclude=frozenset({broker_idx})) if separate_cache_index else broker_idx

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO redis_ledger (idx, project_id, purpose, status) VALUES (?, ?, ?, 'used')
               ON CONFLICT(idx) DO UPDATE SET project_id = excluded.project_id, purpose = excluded.purpose, status = excluded.status""",
            (broker_idx, project_id, broker_purpose),
        )
        if separate_cache_index:
            conn.execute(
                """INSERT INTO redis_ledger (idx, project_id, purpose, status) VALUES (?, ?, ?, 'used')
                   ON CONFLICT(idx) DO UPDATE SET project_id = excluded.project_id, purpose = excluded.purpose, status = excluded.status""",
                (cache_idx, project_id, "cache (separate from broker)"),
            )

    with get_conn() as conn:
        conn.execute(
            """UPDATE projects
               SET uses_celery = ?, uses_celerybeat = ?, redis_broker_index = ?, redis_cache_index = ?, celery_app = ?
               WHERE id = ?""",
            (int(uses_celery), int(uses_celerybeat), broker_idx, cache_idx, celery_app_value, project_id),
        )
        advance_onboarding_step(conn, project_id, "services_configured")
    log_action(
        actor=actor, action="services_and_redis_configured", target=str(project_id),
        details=f"celery={uses_celery} beat={uses_celerybeat} celery_app={celery_app_value} broker_idx={broker_idx} cache_idx={cache_idx}",
    )


def free_services_and_redis(project_id: int, actor: str):
    """The undo path for configure_services_and_redis()'s explicit refusal
    to run twice — frees this project's current ledger row(s) back to
    'free' (same shape as decommission.finalize_stop's ledger-freeing
    block) and clears the project's service/index fields so
    configure_services_and_redis() can run again cleanly. Resets
    onboarding_step back to 'deps_installed', one step before this one, so
    the operator lands back on the services-redis form."""
    with get_conn() as conn:
        project = conn.execute(
            "SELECT redis_broker_index, redis_cache_index FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project["redis_broker_index"] is None:
            raise actions.ActionError("No Redis indices allocated for this project — nothing to free")
        for idx in {project["redis_broker_index"], project["redis_cache_index"]}:
            if idx is None:
                continue
            conn.execute(
                "UPDATE redis_ledger SET status = 'free', project_id = NULL WHERE idx = ? AND project_id = ?",
                (idx, project_id),
            )
        conn.execute(
            """UPDATE projects SET uses_celery = 0, uses_celerybeat = 0, redis_broker_index = NULL,
               redis_cache_index = NULL, onboarding_step = 'deps_installed' WHERE id = ?""",
            (project_id,),
        )
    log_action(actor=actor, action="services_and_redis_freed", target=str(project_id))


ALLOWED_HOSTS_KEY_RE = re.compile(r"(^|_)ALLOWED_HOSTS$", re.IGNORECASE)
DEBUG_KEY_RE = re.compile(r"(^|_)DEBUG$", re.IGNORECASE)
# The app's OWN generic signing key — safe to auto-generate (a fresh random
# value is strictly BETTER than anything an operator would type), no
# external dependency, no ambiguity about what the value should be. Real
# bug caught live testing this session: a suffix-pattern regex like
# `(^|_)SECRET_KEY$` also matches `STRIPE_SECRET_KEY` — a real external
# vendor credential that must come from the operator, not be silently
# replaced with random garbage the operator never even gets asked about
# (the app would "boot successfully" with a fake key and payments would
# just fail). "SECRET_KEY" is FAR more overloaded as a vendor-name suffix
# than ALLOWED_HOSTS/DEBUG are, so this stays an EXPLICIT exact-match
# allowlist of known generic-framework-signing-key names, never a suffix
# pattern.
SECRET_KEY_NAMES = {"SECRET_KEY", "DJANGO_SECRET_KEY", "APP_SECRET_KEY", "FLASK_SECRET_KEY"}


async def parse_env_example(project: dict) -> tuple:
    """Returns (pairs, auto_generated_keys) — pairs is [(key, example_value), ...]
    parsed from the repo's own .env.example (or equivalent); every project
    genuinely needs different variables (brief §3.1.5: one needed Stripe/
    Shippo/Brevo/R2/OpenExchangeRates, another needed none), so this is read
    fresh from the repo, never a hardcoded list. Skips blank lines, comments,
    and the Redis-URL/host/debug keys that get their own dedicated steps
    instead of the generic form.

    Also DETECTS AND RECORDS the actual variable names this repo uses for
    ALLOWED_HOSTS/DEBUG/Redis — a real repo tested this session used
    DJANGO_ALLOWED_HOSTS/DJANGO_DEBUG, not the bare names primary-store
    uses. Assuming a fixed name and writing it regardless would silently
    write a variable the app never reads (brief §4 bug #3, reproduced
    live). Only REDIS_URL (no separate CELERY_BROKER_URL/RESULT_BACKEND)
    turned up in that same repo — same reasoning applies to those.

    SECRET_KEY-shaped keys are auto-generated and saved immediately (INSERT
    OR IGNORE, not overwrite-on-conflict — re-visiting this step, e.g. a
    page reload, must NOT regenerate an already-issued secret, that would
    invalidate anything already relying on it once .env is written) rather
    than added to `pairs` — the operator never sees or types them."""
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    result = await ssh_client.run(
        f"cat {shlex.quote(app_dir + '/.env.example')} 2>/dev/null || "
        f"cat {shlex.quote(app_dir + '/.env.sample')} 2>/dev/null || true",
        host=project["vm_ip"],
    )
    pairs = []
    allowed_hosts_key = None
    debug_key = None
    broker_keys = []
    cache_keys = []
    auto_generated_keys = []
    for line in result["stdout"].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, example_value = line.partition("=")
        key = key.strip()
        if key in BROKER_VAR_NAMES:
            if key not in broker_keys:
                broker_keys.append(key)
            continue
        if key in CACHE_VAR_NAMES:
            if key not in cache_keys:
                cache_keys.append(key)
            continue
        if ALLOWED_HOSTS_KEY_RE.search(key):
            allowed_hosts_key = key
            continue
        if DEBUG_KEY_RE.search(key):
            debug_key = key
            continue
        if key.upper() in SECRET_KEY_NAMES:
            with get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO project_env_vars (project_id, key, value) VALUES (?, ?, ?)",
                    (project["id"], key, crypto.encrypt(secrets.token_urlsafe(50))),
                )
            auto_generated_keys.append(key)
            continue
        pairs.append((key, example_value.strip()))

    with get_conn() as conn:
        conn.execute(
            """UPDATE projects SET allowed_hosts_var_name = ?, debug_var_name = ?,
               redis_broker_var_names = ?, redis_cache_var_names = ? WHERE id = ?""",
            (
                # For Django, keep defaulting to the bare names (byte-identical
                # to before). For FastAPI, an undetected name stays NULL rather
                # than forcing an ALLOWED_HOSTS/DEBUG the app never reads.
                allowed_hosts_key or ("ALLOWED_HOSTS" if project["framework"] == "django" else None),
                debug_key or ("DEBUG" if project["framework"] == "django" else None),
                ",".join(broker_keys) or "REDIS_URL",
                ",".join(cache_keys) or "",
                project["id"],
            ),
        )
    return pairs, auto_generated_keys


def parse_uploaded_env(content: str) -> dict:
    """Pure function, no DB writes, no side effects — same line-parsing
    shape parse_env_example() already uses (skip blank lines/comments,
    key, _, value = line.partition('=')). Used to pre-fill the env-vars
    form from an operator-uploaded .env file; the operator still reviews
    and can edit every value before anything is saved — this only changes
    what's pre-filled, exactly like parse_env_example()'s `example` values
    already do for placeholders, just with `value=` instead."""
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


async def detect_required_env_vars(project: dict, keys: list) -> dict:
    """Best-effort per-key signal: does THIS repo's own code have a
    fallback if the variable is unset, or will it crash at boot without
    one? Grounded in a real grep of the repo's own .py files under
    app_subdir, same "inspect the real repo" discipline as
    parse_env_example() — NOT a guess based on the .env.example
    placeholder text's shape (e.g. assuming `KEY=your-key-here` means
    "required" and `KEY=0` means "has a safe default" is exactly the kind
    of fragile heuristic this project has repeatedly rejected).

    Confirmed live against a real repo's config/settings.py: `env("STRIPE_
    SECRET_KEY")` (no default kwarg -> ImproperlyConfigured at import time
    if unset) vs `env("SHIPENGINE_API_KEY", default="")` (has one). The
    grep pattern below is intentionally broad (any KEY inside quotes,
    regardless of which settings library) rather than trying to parse
    django-environ specifically — broad enough to catch the common cases,
    and any key that doesn't show up at all in the repo's own code is left
    unlabeled (None) rather than guessing either way.

    Returns {key: "required" | "has_default" | None}."""
    if not keys:
        return {}
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    alternation = "|".join(re.escape(k) for k in keys)
    result = await ssh_client.run(
        f"grep -rnE {shlex.quote(alternation)} --include={shlex.quote('*.py')} "
        f"{shlex.quote(app_dir)} 2>/dev/null || true",
        host=project["vm_ip"], timeout=30,
    )
    # Must be an actual `default=` keyword argument, not just the bare word
    # "default" anywhere on the line — caught live: `'default': env.db(
    # 'DATABASE_URL')` (Django's DATABASES dict key, nothing to do with a
    # fallback value) was misclassified as "has_default" by a naive
    # substring check, even though env.db() here has no fallback at all
    # and DATABASE_URL is a hard requirement in practice.
    default_kwarg_re = re.compile(r"default\s*=")
    verdict = {key: None for key in keys}
    for line in result["stdout"].splitlines():
        for key in keys:
            if verdict[key] == "required":
                continue  # already found a no-default usage, that's decisive
            if f'"{key}"' in line or f"'{key}'" in line:
                verdict[key] = "has_default" if default_kwarg_re.search(line) else "required"
    return verdict


def save_env_vars(project_id: int, actor: str, values: dict):
    """values are encrypted before ever touching the DB (brief §5). Upsert —
    re-running this step (e.g. to fix a typo) overwrites cleanly. Kept for the
    upload-secret path and observability.py, which both want a pure additive
    upsert with NO deletion. The env-vars step itself now uses
    reconcile_env_vars() instead, so a removed row is actually removed."""
    with get_conn() as conn:
        for key, value in values.items():
            conn.execute(
                """INSERT INTO project_env_vars (project_id, key, value) VALUES (?, ?, ?)
                   ON CONFLICT(project_id, key) DO UPDATE SET value = excluded.value""",
                (project_id, key, crypto.encrypt(value)),
            )
        advance_onboarding_step(conn, project_id, "env_vars_set")
    log_action(actor=actor, action="env_vars_saved", target=str(project_id), details=f"{len(values)} vars (values not logged)")


def governed_var_names(project: dict) -> set:
    """Every variable NAME this tool computes/owns itself and therefore never
    lets the operator hand-edit as an ordinary custom var: the detected (or
    fallback) ALLOWED_HOSTS/DEBUG names, plus every Redis/Celery broker/cache
    name — both the ones actually detected for THIS repo and the canonical
    well-known names. This is the same union build_env_file_content() uses as
    its write-time backstop; centralised here so the env-vars form, its save
    reconciliation, and the .env writer all agree on exactly one definition."""
    broker_keys = (project["redis_broker_var_names"] or "REDIS_URL").split(",")
    cache_keys = [k for k in (project["redis_cache_var_names"] or "").split(",") if k]
    return (
        {project["allowed_hosts_var_name"] or "ALLOWED_HOSTS",
         project["debug_var_name"] or "DEBUG"}
        | set(broker_keys) | set(cache_keys)
        | BROKER_VAR_NAMES | CACHE_VAR_NAMES
    )


def hidden_var_names(project: dict) -> set:
    """Names never shown as editable rows AND never offered as .env.example
    suggestions, because the tool governs them itself: the Redis/Celery +
    ALLOWED_HOSTS/DEBUG names, the auto-generated signing keys, and the
    observability integration's own vars. parse_env_example already drops the
    governed/signing names from its pairs, but NOT the observability ones —
    so a repo whose .env.example lists SENTRY_DSN/LOKI_*/NTFY_TOPIC would
    otherwise suggest the operator hand-add a key the observability wizard
    owns. One definition used by both the form GET and the upload route."""
    from app import observability  # local import — observability imports onboarding
    return governed_var_names(project) | set(SECRET_KEY_NAMES) | set(observability.MANAGED_ENV_KEYS)


def load_editable_env_vars(project: dict) -> tuple:
    """Returns (editable, generated) from what's ACTUALLY saved for this
    project (decrypted in-memory, never logged) — this is the fix for the
    "sticky" form that used to re-derive its fields from .env.example every
    visit and never showed the operator their own saved values.

    * editable  = [(key, value), ...] the operator sees and can change/remove.
    * generated = auto-issued signing-key names (SECRET_KEY_NAMES), shown as
                  read-only chips — never rendered as an editable input.

    Rows the tool governs itself (Redis/Celery URLs, ALLOWED_HOSTS/DEBUG) and
    rows owned by the observability integration (SENTRY_*/LOKI_*/NTFY_TOPIC)
    are excluded from BOTH lists: they're preserved untouched across a save
    (see reconcile_env_vars) and must never be surfaced for hand-editing."""
    from app import observability  # local import — observability imports onboarding
    governed = governed_var_names(project)
    editable, generated = [], []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM project_env_vars WHERE project_id = ? ORDER BY key",
            (project["id"],),
        ).fetchall()
    for r in rows:
        key = r["key"]
        if key.upper() in SECRET_KEY_NAMES:
            generated.append(key)
        elif key in governed or key in observability.MANAGED_ENV_KEYS:
            continue
        else:
            editable.append((key, crypto.decrypt(r["value"])))
    return editable, generated


def reconcile_env_vars(project_id: int, actor: str, values: dict):
    """Like save_env_vars (encrypt + upsert every submitted key) but ALSO
    deletes previously-saved rows the operator removed from the form — so
    "Remove" actually removes, and stale/blank rows can't accumulate (the bug
    class behind §4h's empty-CELERY_BROKER_URL incident).

    NEVER deletes rows the tool owns itself, because those are deliberately
    not shown in the form and so their absence from `values` must not be read
    as "delete": auto-generated signing keys (SECRET_KEY_NAMES), the governed
    Redis/Celery + ALLOWED_HOSTS/DEBUG names, and the observability
    integration's own vars. Getting this wrong would silently wipe a live
    project's signing key or its observability config on an ordinary env-vars
    re-save — the single highest-risk detail of this feature."""
    from app import observability  # local import — observability imports onboarding
    submitted = set(values.keys())
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
        governed = governed_var_names(project)
        existing = [r["key"] for r in conn.execute(
            "SELECT key FROM project_env_vars WHERE project_id = ?", (project_id,)).fetchall()]
        for key, value in values.items():
            conn.execute(
                """INSERT INTO project_env_vars (project_id, key, value) VALUES (?, ?, ?)
                   ON CONFLICT(project_id, key) DO UPDATE SET value = excluded.value""",
                (project_id, key, crypto.encrypt(value)),
            )
        to_delete = [
            k for k in existing
            if k not in submitted
            and k.upper() not in SECRET_KEY_NAMES
            and k not in governed
            and k not in observability.MANAGED_ENV_KEYS
        ]
        for k in to_delete:
            conn.execute("DELETE FROM project_env_vars WHERE project_id = ? AND key = ?", (project_id, k))
        advance_onboarding_step(conn, project_id, "env_vars_set")
    log_action(
        actor=actor, action="env_vars_saved", target=str(project_id),
        details=f"{len(values)} saved, {len(to_delete)} removed (values not logged)",
    )


def set_subdomain_intent(project_id: int, actor: str, subdomain: str, vm_ip: str):
    """Records the INTENDED DuckDNS subdomain and builds ALLOWED_HOSTS from
    it now — brief §4 bug #3: a stale/placeholder ALLOWED_HOSTS caused a
    400 that looked like an nginx problem but was Django rejecting the Host
    header. Recording intent now means the .env written in this phase
    already has the right value, even though the actual DNS/cert (Phase 3)
    hasn't happened yet — Phase 3 only needs to make the intent real, not
    retrofit ALLOWED_HOSTS afterward."""
    subdomain = subdomain.strip().lower()
    if not subdomain:
        raise actions.ActionError("Subdomain cannot be empty")
    safety.assert_subdomain_available(subdomain, project_id)
    allowed_hosts = f"{subdomain}.duckdns.org,{vm_ip},localhost,127.0.0.1"
    with get_conn() as conn:
        conn.execute(
            "UPDATE projects SET duckdns_subdomain = ?, allowed_hosts = ? WHERE id = ?",
            (subdomain, allowed_hosts, project_id),
        )
        advance_onboarding_step(conn, project_id, "subdomain_set")
    log_action(actor=actor, action="subdomain_intent_set", target=str(project_id), details=subdomain)


def build_env_file_content(project: dict) -> str:
    """Assembles the real .env — parsed .env.example vars (decrypted here,
    in-memory only, never logged) + the ledger-governed Redis URLs +
    ALLOWED_HOSTS/DEBUG. Uses whatever variable names parse_env_example
    actually detected in THIS repo's .env.example (allowed_hosts_var_name,
    debug_var_name, redis_broker_var_names, redis_cache_var_names) —
    never a hardcoded name — since a mismatch here means Django silently
    falls back to its own default instead of erroring (brief §4 bug #3,
    reproduced live against a real repo that used DJANGO_ALLOWED_HOSTS).

    DEBUG stays True deliberately: brief §4 bug #4 — flipping DEBUG=False
    before HTTPS is actually confirmed working (Phase 3, not done yet)
    silently breaks cookie security flags. That flip belongs at the end of
    Phase 3, once HTTPS is verified, not here.

    Every value is single-quoted via shlex.quote(). Real bug found live: an
    unquoted `&` in a real Neon connection string (...?sslmode=require&
    channel_binding=require) got interpreted as "run the rest of this line
    in the background" the moment anything `source`s this file (see
    create_migrate_preview/run_health_check, which both have to, since this
    app's settings.py never loads .env itself — it relies entirely on
    systemd's EnvironmentFile= at runtime). Quoting protects both
    consumers: systemd's EnvironmentFile parser and bash's `source` both
    honor single-quoted values as literal, per their own docs."""
    with get_conn() as conn:
        saved = conn.execute(
            "SELECT key, value FROM project_env_vars WHERE project_id = ?", (project["id"],)
        ).fetchall()

    broker_url = f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{project['redis_broker_index']}"
    cache_url = f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{project['redis_cache_index']}"

    # Django keeps the bare-name fallback (byte-identical output). FastAPI only
    # gets these lines if its .env.example actually declared them — otherwise
    # they're skipped rather than writing vars the app ignores.
    allowed_hosts_key = project["allowed_hosts_var_name"] or ("ALLOWED_HOSTS" if project["framework"] == "django" else None)
    debug_key = project["debug_var_name"] or ("DEBUG" if project["framework"] == "django" else None)
    broker_keys = (project["redis_broker_var_names"] or "REDIS_URL").split(",")
    cache_keys = [k for k in (project["redis_cache_var_names"] or "").split(",") if k]

    lines = []
    if debug_key:
        lines.append(f"{debug_key}={shlex.quote('True')}")
    if allowed_hosts_key:
        lines.append(f"{allowed_hosts_key}={shlex.quote(project['allowed_hosts'])}")
    for name in broker_keys:
        lines.append(f"{name}={shlex.quote(broker_url)}")
    for name in cache_keys:
        lines.append(f"{name}={shlex.quote(cache_url)}")
    # Defense-in-depth: skip any saved row whose key collides with a
    # dedicated line already written above (ALLOWED_HOSTS/DEBUG/broker/
    # cache). Real bug hit live TWICE, same root cause, two different key
    # sets: an operator's uploaded .env file included its own values for
    # keys this app governs itself, which (before the upload-route fixes
    # that now exclude them — BROKER_VAR_NAMES/CACHE_VAR_NAMES for the
    # second occurrence) got saved into project_env_vars as ordinary
    # custom vars. Appending them here unconditionally produced a SECOND
    # line for the same key, and since EnvironmentFile= honors the LAST
    # occurrence of a duplicate key, the uploaded (wrong/blank) values
    # silently won over the correct ones. First hit with ALLOWED_HOSTS/
    # DEBUG (Django DisallowedHost 400). Second hit with CELERY_BROKER_URL/
    # REDIS_URL/CELERY_RESULT_BACKEND/CACHE_REDIS_URL — live on `app-alpha`: an
    # empty-string CELERY_BROKER_URL row (saved during onboarding, before
    # the upload-route fix existed) got appended AFTER the correct
    # ledger-computed broker line the moment 'Write .env' was re-run,
    # emptying the broker URL and making Celery silently fall back to its
    # own built-in AMQP default (guest@localhost:5672) — real, live,
    # continuous connection-refused errors until this fix. This filter is
    # the backstop for any row that predates an upload-route fix, or
    # reaches project_env_vars some other way in the future — union of the
    # dedicated keys ACTUALLY written above (this project's real detected
    # var names) plus the canonical well-known names, so a stale row under
    # either can never sneak through.
    governed_keys = (
        {k for k in (allowed_hosts_key, debug_key) if k} | set(broker_keys) | set(cache_keys)
        | BROKER_VAR_NAMES | CACHE_VAR_NAMES
    )
    for row in saved:
        if row["key"] in governed_keys:
            continue
        lines.append(f"{row['key']}={shlex.quote(crypto.decrypt(row['value']))}")
    return "\n".join(lines) + "\n"


def create_write_env_preview(actor: str, project: dict) -> str:
    """Unlike every other preview in this tool, the 'command' stored here is
    NOT what actually runs — it's a redacted placeholder (var names only,
    safe to show/log) purely for the operator to review before confirming.
    The real content is built fresh (decrypted in-memory) inside
    confirm_write_env(), and reaches VM 100 only via ssh_client.write_file()
    (SFTP), never as a shell command string — see that function's docstring
    for why."""
    safety.assert_project_not_protected(project["name"])
    if project["redis_broker_index"] is None:
        raise actions.ActionError("Allocate Redis indices before writing .env")

    with get_conn() as conn:
        saved_keys = [r["key"] for r in conn.execute(
            "SELECT key FROM project_env_vars WHERE project_id = ?", (project["id"],)
        ).fetchall()]
    broker_keys = (project["redis_broker_var_names"] or "REDIS_URL").split(",")
    cache_keys = [k for k in (project["redis_cache_var_names"] or "").split(",") if k]
    governed = {project["allowed_hosts_var_name"] or "ALLOWED_HOSTS", project["debug_var_name"] or "DEBUG"}
    all_keys = sorted(governed | set(broker_keys) | set(cache_keys) | set(saved_keys))
    placeholder = f"[write .env — {len(all_keys)} vars: {', '.join(all_keys)}]"
    return actions.create_preview_row(actor, project, placeholder, project["name"], "onboarding_write_env")


async def confirm_write_env(token: str, actor: str) -> dict:
    row = actions.load_pending_for_confirm(token)
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE name = ?", (row["project_name"],)).fetchone())

    content = build_env_file_content(project)
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    env_path = f"{app_dir}/.env"

    log_action(actor=actor, action="confirm_execute", target=row["target_unit"], details="(env content redacted)")
    result = await ssh_client.write_file(env_path, content, host=project["vm_ip"], mode=0o600)
    row = actions.record_result(token, row, result)
    if row["status"] == "executed":
        with get_conn() as conn:
            advance_onboarding_step(conn, project["id"], "env_written")
    return row


def _enabled_services(project: dict) -> list:
    services = ["gunicorn"]
    if project["uses_celery"]:
        services.append("celery")
    if project["uses_celerybeat"]:
        services.append("celerybeat")
    return services


async def create_migrate_preview(actor: str, project: dict) -> str:
    """Django: manage.py check (brief §3.1.9 — sanity check before touching
    migrations) -> migrate -> collectstatic. Non-Django (FastAPI): import the
    actual app OBJECT the server will import (catches "module imports but the
    app callable is missing/errors"), and — if the repo ships an alembic.ini —
    run `alembic upgrade head` too, mirroring Django's "sanity check, THEN
    migrate" ordering. Alembic detection is a grounded file check within the
    operator-confirmed app_subdir; the operator still reviews the exact
    command in the preview before it runs, so the preview IS the "ask".

    Real bug found live: this repo's settings.py never calls read_env() /
    load_dotenv() itself — it relies entirely on systemd's
    EnvironmentFile= populating the process env, which only happens when
    gunicorn/celery are started BY systemd. A plain SSH exec of manage.py
    has no such thing sourcing .env for it, so Django silently fell back to
    its own hardcoded local DB default instead of erroring. `set -a;
    source .env; set +a` before the python calls makes a bare SSH exec see
    the same environment systemd would give it."""
    safety.assert_project_not_protected(project["name"])
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    source_env = f"set -a && source {shlex.quote(app_dir + '/.env')} && set +a"
    if project["framework"] == "django":
        command = (
            f"{source_env} && "
            f"{app_dir}/venv/bin/python {app_dir}/manage.py check && "
            f"{app_dir}/venv/bin/python {app_dir}/manage.py migrate --noinput && "
            f"{app_dir}/venv/bin/python {app_dir}/manage.py collectstatic --noinput"
        )
    else:
        ep = project["wsgi_asgi_entrypoint"]
        if ":" in ep:
            module, attr = ep.split(":", 1)
            import_check = f"from {module} import {attr}"
        else:
            import_check = f"import {ep}"
        alembic = await ssh_client.run(
            f"test -f {shlex.quote(app_dir + '/alembic.ini')} && echo yes || echo no",
            host=project["vm_ip"], timeout=15,
        )
        steps = [f"{app_dir}/venv/bin/python -c {shlex.quote(import_check)}"]
        if alembic["stdout"].strip() == "yes":
            steps.append(f"{app_dir}/venv/bin/alembic upgrade head")
        command = f"cd {shlex.quote(app_dir)} && {source_env} && " + " && ".join(steps)
    return actions.create_preview_row(actor, project, command, project["name"], "onboarding_migrate")


def finalize_migrate(project_id: int, actor: str):
    with get_conn() as conn:
        advance_onboarding_step(conn, project_id, "migrated")
    log_action(actor=actor, action="onboarding_migrate_finalized", target=str(project_id))


async def create_units_install_preview(actor: str, project: dict) -> str:
    """Renders concrete (non-template) unit files via deploy_templates.py —
    same reasoning as decommission.py's stop command: the shared %i
    templates on VM 100 (gunicorn@.service etc.) are NEVER touched, new
    projects get their own gunicorn@<project>.service etc. Content is
    staged via SFTP first (harmless, no secrets in a unit file, just paths),
    then the preview shows the sudo cp + daemon-reload the operator actually
    confirms."""
    safety.assert_project_not_protected(project["name"])
    from app import deploy_templates

    units = [("gunicorn", deploy_templates.render_gunicorn_unit(
        project["name"], project["framework"], project["wsgi_asgi_entrypoint"], project["app_subdir"] or ""
    ))]
    if project["uses_celery"]:
        units.append(("celery", deploy_templates.render_celery_unit(
            project["name"], celery_app=project["celery_app"] or "config", app_subdir=project["app_subdir"] or "")))
    if project["uses_celerybeat"]:
        units.append(("celerybeat", deploy_templates.render_celerybeat_unit(
            project["name"], celery_app=project["celery_app"] or "config",
            app_subdir=project["app_subdir"] or "", framework=project["framework"])))

    for svc, content in units:
        filename = f"{svc}@{project['name']}.service"
        safety.assert_filename_not_protected(filename)
        staging_path = f"/home/deploy/.cp-staging/{filename}"
        result = await ssh_client.write_file(staging_path, content, host=project["vm_ip"], mode=0o644)
        if result["exit_status"] != 0:
            raise actions.ActionError(f"Failed to stage {filename}: {result['stderr']}")

    parts = []
    for svc, _ in units:
        filename = f"{svc}@{project['name']}.service"
        parts.append(
            f"sudo -n /usr/bin/cp {shlex.quote('/home/deploy/.cp-staging/' + filename)} "
            f"{shlex.quote('/etc/systemd/system/' + filename)}"
        )
    parts.append("sudo -n /usr/bin/systemctl daemon-reload")
    command = " && ".join(parts)
    return actions.create_preview_row(actor, project, command, project["name"], "onboarding_install_units")


def finalize_units_install(project_id: int, actor: str):
    with get_conn() as conn:
        advance_onboarding_step(conn, project_id, "units_installed")
    log_action(actor=actor, action="onboarding_units_installed", target=str(project_id))


def create_start_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    parts = []
    for svc in _enabled_services(project):
        unit = f"{svc}@{project['name']}"
        parts.append(f"sudo -n /usr/bin/systemctl enable {shlex.quote(unit)}")
        parts.append(f"sudo -n /usr/bin/systemctl start {shlex.quote(unit)}")
    command = " && ".join(parts)
    return actions.create_preview_row(actor, project, command, project["name"], "onboarding_start")


def finalize_start(project_id: int, actor: str):
    with get_conn() as conn:
        advance_onboarding_step(conn, project_id, "services_started")
    log_action(actor=actor, action="onboarding_services_started", target=str(project_id))


async def run_health_check(project: dict) -> dict:
    """Brief §3.1.15 / §4 bug #10 — systemctl is-active can be misleadingly
    green while the app inside is actually broken. This talks to the app
    directly through its real gunicorn socket (no nginx yet — that's Phase
    3), and runs `celery status` if a worker is enabled, rather than only
    checking systemd's opinion of things."""
    sock = f"/run/gunicorn/{project['name']}.sock"
    http = await ssh_client.run(
        f"curl -s -o /dev/null -w '%{{http_code}}' --unix-socket {shlex.quote(sock)} http://localhost/ 2>&1",
        host=project["vm_ip"], timeout=15,
    )
    checks = {
        "gunicorn_active": await ssh_client.unit_status(f"gunicorn@{project['name']}", host=project["vm_ip"]),
        "gunicorn_http_response": http["stdout"].strip() or f"(no response: {http['stderr']})",
    }
    if project["uses_celery"]:
        dest = f"/srv/apps/{project['name']}"
        app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
        # Same gap as create_migrate_preview — a bare SSH exec never gets
        # the env systemd's EnvironmentFile= would have given it.
        celery = await ssh_client.run(
            f"set -a && source {shlex.quote(app_dir + '/.env')} && set +a && "
            f"cd {shlex.quote(app_dir)} && {app_dir}/venv/bin/celery -A {shlex.quote(project['celery_app'] or 'config')} status",
            host=project["vm_ip"], timeout=30,
        )
        checks["celery_active"] = await ssh_client.unit_status(f"celery@{project['name']}", host=project["vm_ip"])
        checks["celery_status"] = (celery["stdout"].strip() or celery["stderr"].strip())
    if project["uses_celerybeat"]:
        checks["celerybeat_active"] = await ssh_client.unit_status(f"celerybeat@{project['name']}", host=project["vm_ip"])
    return checks


def finalize_onboarding(project_id: int, actor: str, checks: dict):
    """Marks Phase 1+2 complete. Deliberately does NOT gate this on the
    health check looking good — brief's whole point about is-active vs real
    health is that a human should look at the actual numbers and judge, not
    that the tool should silently decide 'healthy enough' on their behalf."""
    with get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'running' WHERE id = ?", (project_id,))
        advance_onboarding_step(conn, project_id, "onboarding_complete")
    log_action(actor=actor, action="onboarding_complete", target=str(project_id), details=str(checks))


async def build_deployment_summary(project: dict) -> dict:
    """The final checklist — everything that got set up for this project
    and exactly where to find it on VM 100, so the operator can go verify
    it themselves rather than just trusting the DB's opinion. Every path
    shown here is a real convention this codebase already uses elsewhere
    (deploy_templates.py's unit/nginx renderers, build_env_file_content's
    .env path) — nothing here is invented new. Live reads (service status,
    DNS resolution, HTTPS reachability) use the exact same functions
    run_health_check/run_https_health_check already use, not new ones."""
    from app import dns_cert  # local import: dns_cert imports nothing from
    # onboarding, but importing it at module level here would be a real
    # circular-import risk if that ever changes — same caution already
    # applied to dns_cert.create_nginx_install_preview's `from app import
    # deploy_templates` local import.

    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest

    services = []
    for svc in _enabled_services(project):
        unit = f"{svc}@{project['name']}"
        services.append({
            "name": svc,
            "unit_file": f"/etc/systemd/system/{unit}.service",
            "live_status": await ssh_client.unit_status(unit, host=project["vm_ip"]),
        })

    redis = [{
        "index": project["redis_broker_index"],
        "purpose": "Celery broker + result backend" if project["uses_celery"] else "cache",
        "url": f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{project['redis_broker_index']}",
    }]
    if project["redis_cache_index"] != project["redis_broker_index"]:
        redis.append({
            "index": project["redis_cache_index"],
            "purpose": "cache (separate from broker)",
            "url": f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{project['redis_cache_index']}",
        })

    nginx_enabled = None
    dns_resolved_ip = None
    https_reachable = None
    if project["duckdns_subdomain"]:
        nginx_check = await ssh_client.run(
            f"test -L {shlex.quote('/etc/nginx/sites-enabled/' + project['name'])} && echo yes || echo no",
            host=project["vm_ip"], timeout=15,
        )
        nginx_enabled = nginx_check["stdout"].strip() == "yes"
        dns_resolved_ip = dns_cert.check_dns_resolves(project["duckdns_subdomain"])
        if project["onboarding_step"] == "https_live":
            https_result = await ssh_client.run(
                f"curl -sk -o /dev/null -w '%{{http_code}}' https://localhost/ "
                f"-H {shlex.quote('Host: ' + project['duckdns_subdomain'] + '.duckdns.org')}",
                host=project["vm_ip"], timeout=15,
            )
            https_reachable = https_result["stdout"].strip()

    return {
        "app_dir": app_dir,
        "env_path": f"{app_dir}/.env",
        "services": services,
        "redis": redis,
        "nginx_site_path": f"/etc/nginx/sites-available/{project['name']}" if project["duckdns_subdomain"] else None,
        "nginx_enabled": nginx_enabled,
        "cert_path": f"/etc/letsencrypt/live/{project['duckdns_subdomain']}.duckdns.org/" if project["duckdns_subdomain"] else None,
        "cert_expiry_date": project["cert_expiry_date"],
        "dns_name": f"{project['duckdns_subdomain']}.duckdns.org" if project["duckdns_subdomain"] else None,
        "dns_resolved_ip": dns_resolved_ip,
        "https_reachable": https_reachable,
        "public_url": f"https://{project['duckdns_subdomain']}.duckdns.org" if project["onboarding_step"] == "https_live" else None,
    }
