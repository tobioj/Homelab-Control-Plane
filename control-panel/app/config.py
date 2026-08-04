import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name} (see .env.example)")
    return val


class Settings:
    SECRET_KEY = _require("SECRET_KEY")
    ENCRYPTION_KEY = _require("ENCRYPTION_KEY")
    DB_PATH = BASE_DIR / os.environ.get("DB_PATH", "data/panel.db")

    VM100_IP = os.environ.get("VM100_IP", "10.10.0.20")
    VM100_SSH_USER = os.environ.get("VM100_SSH_USER", "deploy")
    ORCHESTRATOR_KEY_PATH = os.environ.get(
        "ORCHESTRATOR_KEY_PATH", str(Path.home() / ".ssh" / "orchestrator_vm100")
    )

    # Shared Redis lives on VM 103 (this machine, per BRIEF.md §2.4), NOT on
    # whatever VM a given project is deployed to — every project's
    # REDIS_URL/CELERY_BROKER_URL points back here regardless of vm_ip.
    REDIS_HOST = os.environ.get("REDIS_HOST", "10.10.0.21")
    REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
    # Confirmed live via `redis-cli CONFIG GET databases` — this Redis
    # instance's actual configured limit (indices 0 through this-1). Real
    # bug this guards against: db.next_free_redis_index() used to hand out
    # indices beyond this with no awareness of the real ceiling. If Redis's
    # own `databases` config is ever raised, bump this to match — otherwise
    # this becomes an artificially low ceiling instead of a correct one.
    REDIS_MAX_DATABASES = int(os.environ.get("REDIS_MAX_DATABASES", "16"))

    ADMIN_BOOTSTRAP_USERNAME = os.environ.get("ADMIN_BOOTSTRAP_USERNAME", "admin")
    ADMIN_BOOTSTRAP_EMAIL = os.environ.get("ADMIN_BOOTSTRAP_EMAIL", "")
    ADMIN_BOOTSTRAP_PASSWORD = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD", "")

    # certbot's -m contact address (Phase 3). Defaults to the bootstrap
    # admin email so no .env edit is strictly required, but can be
    # overridden independently if that's ever not the right contact for
    # cert expiry notices.
    CERTBOT_CONTACT_EMAIL = os.environ.get("CERTBOT_CONTACT_EMAIL", "") or ADMIN_BOOTSTRAP_EMAIL

    # DuckDNS account token (Phase 3) — used by the certbot DNS-01 auth hook
    # to set the _acme-challenge TXT record automatically via DuckDNS's own
    # update API (the same one duck.sh already uses on VM 100). Optional at
    # import time (not _require()'d) so the rest of the app keeps working
    # for anyone who hasn't set it yet; Phase 3's certbot-start step raises
    # its own clear error if it's blank when actually needed. This value is
    # only ever embedded in a hook script staged via SFTP on VM 100 for the
    # duration of one certbot run — never in a pending_actions.command
    # string or audit_log entry, same discipline as the .env write.
    DUCKDNS_TOKEN = os.environ.get("DUCKDNS_TOKEN", "")

    # Panel-owned dynamic-DNS updater (app/ddns.py, run by the
    # control-panel-ddns systemd timer). The updater derives the set of
    # DuckDNS subdomains to keep current from the projects table itself
    # (every non-decommissioned project with a recorded duckdns_subdomain),
    # so every wizard-onboarded project is auto-maintained and can never
    # again go stale the way app-alpha/app-beta did on 2026-07-24 when the
    # WAN IP changed. This OPTIONAL, comma-separated list folds in any extra
    # subdomains that AREN'T tracked in the DB — left empty by default so the
    # panel touches nothing primary-store depends on (primary-store's A-record
    # stays owned by VM 100's ~/duckdns/duck.sh; primary-store has no
    # duckdns_subdomain row and is a hard-protected project). Only set this if
    # you deliberately want to retire duck.sh and let the panel own those
    # names too, e.g. "primary-store,legacy-host".
    DDNS_EXTRA_SUBDOMAINS = os.environ.get("DDNS_EXTRA_SUBDOMAINS", "")

    # Per-project observability opt-in (GlitchTip + Loki + ntfy, all local
    # to VM 103 — see ~/dev-infra/INFRA.md). All optional at import time,
    # same as DUCKDNS_TOKEN — the "enable observability" action raises its
    # own clear error if these are blank when actually needed. GlitchTip's
    # org/team must already exist (created manually once in its UI) — this
    # tool only ever creates PROJECTS under them via the API, never orgs or
    # teams. `primary-store` already has its own real GlitchTip project
    # under this same org — this integration must NEVER create a duplicate
    # or touch it; see safety.assert_project_not_protected(), called first
    # in every observability action same as every other mutating action.
    GLITCHTIP_BASE_URL = os.environ.get("GLITCHTIP_BASE_URL", "http://10.10.0.21:8002")
    GLITCHTIP_API_TOKEN = os.environ.get("GLITCHTIP_API_TOKEN", "")
    GLITCHTIP_ORG_SLUG = os.environ.get("GLITCHTIP_ORG_SLUG", "")
    GLITCHTIP_TEAM_SLUG = os.environ.get("GLITCHTIP_TEAM_SLUG", "")

    # ntfy's own base URL — used only to build the topic subscribe link
    # shown to the operator, never for anything that mutates config outside
    # a per-topic ACL grant (this app never touches ntfy's own docker-compose
    # setup, users, or global auth settings).
    NTFY_BASE_URL = os.environ.get("NTFY_BASE_URL", "http://10.10.0.21:8080")

    # Loki's push endpoint, via loki-proxy (see ~/dev-infra/loki-proxy/) —
    # a single shared credential for every project, confirmed live against
    # the real nginx config, not something this tool can set per-project.
    LOKI_PUSH_URL = os.environ.get("LOKI_PUSH_URL", "http://10.10.0.21:3100/loki/api/v1/push")

    # Grafana base URL (VM 103) — used only to build "Search logs in Grafana"
    # deep-links for projects that ship logs to Loki. Never used to mutate
    # anything. Port confirmed against BRIEF.md / the observability wizard.
    GRAFANA_BASE_URL = os.environ.get("GRAFANA_BASE_URL", "http://10.10.0.21:3000")

    # Uptime prober (app/uptime.py, run by the control-panel-uptime timer).
    # Alerts go to this ntfy topic; a cert within UPTIME_CERT_ALERT_DAYS of
    # expiry alerts too (deliberately below the 14-day renewal window, so a
    # cert alert means "renewal probably failed", not "renewal is due").
    # UPTIME_FAIL_THRESHOLD consecutive failures are required before a
    # down-alert fires, to ride out a single transient blip.
    UPTIME_NTFY_TOPIC = os.environ.get("UPTIME_NTFY_TOPIC", "control-panel-health")
    UPTIME_CERT_ALERT_DAYS = int(os.environ.get("UPTIME_CERT_ALERT_DAYS", "7"))
    UPTIME_FAIL_THRESHOLD = int(os.environ.get("UPTIME_FAIL_THRESHOLD", "2"))


settings = Settings()
