import sqlite3
from contextlib import contextmanager

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'viewer')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    repo_url TEXT,
    branch TEXT DEFAULT 'main',
    framework TEXT CHECK(framework IN ('django', 'fastapi', 'other')),
    vm_ip TEXT NOT NULL,
    wsgi_asgi_entrypoint TEXT,
    uses_celery INTEGER NOT NULL DEFAULT 0,
    uses_celerybeat INTEGER NOT NULL DEFAULT 0,
    redis_broker_index INTEGER,
    redis_cache_index INTEGER,
    allowed_hosts TEXT,
    duckdns_subdomain TEXT,
    is_live INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS redis_ledger (
    idx INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    purpose TEXT,
    status TEXT NOT NULL CHECK(status IN ('used', 'retired', 'free')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    result TEXT
);

-- Generic preview-then-confirm pipeline for every state-changing action this
-- tool ever takes against a managed VM. Nothing writes to VM 100 without a
-- row here first, and the command string is only ever built server-side
-- from validated enum inputs (see app/actions.py) — never from raw user text.
CREATE TABLE IF NOT EXISTS pending_actions (
    token TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_host TEXT NOT NULL,
    target_unit TEXT NOT NULL,
    command TEXT NOT NULL,
    project_name TEXT NOT NULL,
    is_live INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'executed', 'failed', 'cancelled', 'expired')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at TEXT,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT
);

-- Per-project env vars parsed from .env.example and filled in during
-- onboarding. `value` is always Fernet-ciphertext (see app/crypto.py) —
-- brief §5: secrets in the panel's own storage must be encrypted at rest,
-- never a flat readable file. Decrypted only in-memory, only when building
-- the actual .env content to write to VM 100 (app/onboarding.py).
CREATE TABLE IF NOT EXISTS project_env_vars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE(project_id, key)
);

-- External-uptime state per project, written by app/uptime.py (the polled
-- prober, run by the control-panel-uptime systemd timer). This captures the
-- one failure mode the systemd service columns structurally cannot: services
-- healthy on VM 100 but the site unreachable from the public internet (the
-- 2026-07-24 stale-DNS incident). last_alerted_status dedupes ntfy alerts so
-- a persistently-down site pages once, not every few minutes.
CREATE TABLE IF NOT EXISTS project_health (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    last_status TEXT,
    last_http_status INTEGER,
    last_latency_ms INTEGER,
    last_checked_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_alerted_status TEXT,
    cert_days_left INTEGER,
    last_cert_alert_at TEXT
);
"""


@contextmanager
def get_conn():
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the initial schema, for the onboarding wizard.
# ALTER TABLE ADD COLUMN isn't naturally idempotent like CREATE TABLE IF NOT
# EXISTS — _ensure_column() below makes it safe to call on every startup.
WIZARD_COLUMNS = {
    "onboarding_step": "TEXT",
    "deploy_key_path": "TEXT",
    "dependency_file": "TEXT",
    "app_subdir": "TEXT",
    # Discovered live against a real repo: not every project calls these
    # ALLOWED_HOSTS/DEBUG — one used DJANGO_ALLOWED_HOSTS/DJANGO_DEBUG.
    # Writing the wrong name is exactly brief §4 bug #3 (Django silently
    # ignores an env var it never reads and falls back to its own
    # hardcoded default) — so the ACTUAL name from .env.example is recorded
    # here rather than assumed.
    "allowed_hosts_var_name": "TEXT",
    "debug_var_name": "TEXT",
    "redis_broker_var_names": "TEXT",
    "redis_cache_var_names": "TEXT",
    # The Celery app the `-A` flag points at (worker + beat units, health
    # check). NULL unless Celery is enabled; falls back to "config" (Django's
    # settings-package convention) everywhere it's read, so existing rows and
    # primary-store are unaffected. Captured per-project because a FastAPI
    # Celery app is usually something like "app.worker" or "worker:celery",
    # not "config" — hardcoding "config" broke FastAPI Celery entirely.
    "celery_app": "TEXT",
    # Parsed from certbot's own stdout at issuance time (dns_cert.
    # confirm_certbot_start), not read live from /etc/letsencrypt/ —
    # confirmed that directory is root-only (0700), unreadable by the
    # orchestrator SSH user even for a plain existence check, let alone an
    # expiry read. Capturing it once at the moment certbot reports it is
    # the only reliable source this app has.
    "cert_expiry_date": "TEXT",
}


def _ensure_column(conn, table: str, column: str, coltype: str):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    """Create schema if missing, and seed known real-world state from the
    infra brief's §2.4 ledger + the already-live primary-store project.
    Seeding is idempotent — INSERT OR IGNORE keyed on unique columns."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for column, coltype in WIZARD_COLUMNS.items():
            _ensure_column(conn, "projects", column, coltype)
        # Atomic double-submit guard for confirm actions (actions.py's
        # load_pending_for_confirm) — a separate unconstrained column
        # rather than a new `status` enum value, since status has a CHECK
        # constraint SQLite can't ALTER without a full table rebuild.
        _ensure_column(conn, "pending_actions", "claimed_at", "TEXT")

        existing_project = conn.execute(
            "SELECT id FROM projects WHERE name = 'primary-store'"
        ).fetchone()
        if existing_project is None:
            conn.execute(
                """INSERT INTO projects
                   (name, framework, vm_ip, wsgi_asgi_entrypoint, uses_celery,
                    uses_celerybeat, redis_broker_index, redis_cache_index,
                    allowed_hosts, duckdns_subdomain, is_live, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "primary-store", "django", "10.10.0.20", "config.wsgi:application",
                    1, 1, 2, 3,
                    "primary-store.duckdns.org,10.10.0.20,localhost,127.0.0.1",
                    "primary-store", 1, "running",
                ),
            )
            project_id = conn.execute(
                "SELECT id FROM projects WHERE name = 'primary-store'"
            ).fetchone()["id"]
        else:
            project_id = existing_project["id"]

        ledger_seed = [
            (0, None, "unused / Django default", "free"),
            (1, None, "retired — belonged to a decommissioned project; do not reassign without confirmation", "retired"),
            (2, project_id, "primary-store — Celery broker + result backend", "used"),
            (3, project_id, "primary-store — Django cache", "used"),
        ]
        for idx, pid, purpose, status in ledger_seed:
            conn.execute(
                "INSERT OR IGNORE INTO redis_ledger (idx, project_id, purpose, status) VALUES (?, ?, ?, ?)",
                (idx, pid, purpose, status),
            )

        if settings.ADMIN_BOOTSTRAP_PASSWORD:
            has_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if has_users == 0:
                from app.auth import hash_password
                conn.execute(
                    "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
                    (
                        settings.ADMIN_BOOTSTRAP_USERNAME,
                        settings.ADMIN_BOOTSTRAP_EMAIL,
                        hash_password(settings.ADMIN_BOOTSTRAP_PASSWORD),
                    ),
                )


def next_free_redis_index(exclude: frozenset = frozenset()) -> int:
    """Prefers reclaiming the lowest currently-FREE index (a decommissioned
    project's old slot) over always incrementing past the current max.
    Real bug found live: the old "MAX(idx)+1, forever, never reuse
    anything" behavior walked straight past Redis's actual configured
    database limit (16 by default on this instance, confirmed live via
    `CONFIG GET databases`) after this project's own extensive test-and-
    decommission cycles freed up indices 4-15 but kept counting past them
    anyway — silently handing a real project (`retired-app`, indices 16/17) a
    Redis index that Redis itself rejects with "DB index is out of range".
    Retired indices (status='retired', e.g. /1) are still never reused,
    matching brief §2.4's explicit call for that — only genuinely 'free'
    rows are reclaimed. Index /0 is ALSO excluded from reclaim even though
    the ledger may show it as 'free' — it's Redis's own protocol default
    database (what a bare `redis-cli` or an unconfigured client silently
    connects to), documented in BRIEF.md §2.4 as "unused / Django default"
    rather than a genuinely reusable slot. Caught live: the first version
    of this fix reclaimed /0 for a real project before it was ever written
    to disk anywhere, reverted before it touched VM 100.

    `exclude`: caller-supplied indices to also skip — needed when a caller
    wants two DISTINCT indices (broker + cache) in one go without an
    intervening commit. Real bug found live: `configure_services_and_redis`
    used to compute `cache_idx = broker_idx + 1`, which only ever worked
    under the old "always increment" allocator where consecutive indices
    were guaranteed related — now that this function can return a
    reclaimed, non-consecutive index, that assumption silently risked
    colliding with a DIFFERENT project's real index (exactly the brief §4
    bug #2 collision class this ledger exists to prevent). Callers needing
    two indices now call this twice, passing the first result in `exclude`
    for the second call, rather than doing arithmetic on the first result.

    Refuses (raises ValueError) rather than silently minting an index
    beyond the configured limit — better to surface this clearly than
    repeat the retired-app failure mode a second time."""
    with get_conn() as conn:
        skip = {0, *exclude}
        placeholders = ",".join("?" * len(skip))
        free_row = conn.execute(
            f"SELECT idx FROM redis_ledger WHERE status = 'free' AND idx NOT IN ({placeholders}) ORDER BY idx LIMIT 1",
            tuple(skip),
        ).fetchone()
        if free_row is not None:
            return free_row["idx"]
        row = conn.execute("SELECT MAX(idx) AS m FROM redis_ledger").fetchone()
        candidate = (row["m"] or -1) + 1
        while candidate in skip:
            candidate += 1
    if candidate >= settings.REDIS_MAX_DATABASES:
        raise ValueError(
            f"No free Redis index available, and the next one ({candidate}) would exceed "
            f"this Redis instance's configured limit of {settings.REDIS_MAX_DATABASES} databases "
            f"(checked via `redis-cli CONFIG GET databases`). Either free up an index by "
            f"decommissioning an unused project, or raise Redis's own `databases` config and "
            f"restart it (affects every project sharing this Redis instance, including "
            f"primary-store — needs explicit confirmation, not something to do lightly)."
        )
    return candidate
