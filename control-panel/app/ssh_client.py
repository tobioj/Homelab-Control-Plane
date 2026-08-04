import asyncssh

from app.config import settings

# 15s was fine for read-only status checks but far too short for a real
# `pip install` (discovered live: installing Django + DRF + everything else
# for a real repo blew straight through it and crashed the route with an
# unhandled asyncssh.process.TimeoutError -> 500). Every mutating action in
# this tool is deliberate and admin-triggered, never high-frequency, so a
# generous default costs nothing.
DEFAULT_TIMEOUT = 300


async def run(command: str, host: str = None, timeout: int = DEFAULT_TIMEOUT):
    """Run a command on a managed VM via the orchestrator key. This is the
    ONLY place that opens an SSH connection to a managed VM — every remote
    command, read-only or mutating, goes through here, which keeps them all
    auditable/interceptable in one spot.

    Never raises for a timeout or connection failure — always returns a
    result dict, with exit_status=124 and a clear stderr message if the
    command didn't finish in time. A crashed route (500) is worse than a
    cleanly-recorded failed action; the caller (actions.confirm_and_run)
    already handles a nonzero exit_status correctly."""
    target = host or settings.VM100_IP
    try:
        async with asyncssh.connect(
            target,
            username=settings.VM100_SSH_USER,
            client_keys=[settings.ORCHESTRATOR_KEY_PATH],
            known_hosts=None,
        ) as conn:
            result = await conn.run(command, check=False, timeout=timeout)
            return {
                "exit_status": result.exit_status,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
    except asyncssh.process.TimeoutError:
        return {"exit_status": 124, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except (asyncssh.Error, OSError) as e:
        return {"exit_status": 255, "stdout": "", "stderr": f"SSH connection error: {e}"}


async def write_file(path: str, content: str, host: str = None, mode: int = 0o600):
    """Writes file content directly via SFTP — no shell command string is
    ever built from `content`. This is the ONLY way secret-bearing content
    (a project's real .env) gets onto a managed VM: passing it through
    run() would put the plaintext into pending_actions.command and
    audit_log, defeating the whole point of encrypting it at rest in our
    own DB (brief §5). Never raises, same contract as run()."""
    target = host or settings.VM100_IP
    try:
        async with asyncssh.connect(
            target,
            username=settings.VM100_SSH_USER,
            client_keys=[settings.ORCHESTRATOR_KEY_PATH],
            known_hosts=None,
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "w") as f:
                    await f.write(content)
                await sftp.chmod(path, mode)
            return {"exit_status": 0, "stdout": "", "stderr": ""}
    except (asyncssh.Error, OSError) as e:
        return {"exit_status": 255, "stdout": "", "stderr": f"SFTP write error: {e}"}


async def check_reachable(host: str = None) -> bool:
    try:
        result = await run("echo ok", host=host, timeout=5)
        return result["exit_status"] == 0
    except Exception:
        return False


async def unit_status(unit: str, host: str = None) -> str:
    """e.g. unit_status('gunicorn@primary-store') -> 'active' / 'inactive' / 'failed' / 'unknown'"""
    result = await run(f"systemctl is-active {unit}", host=host)
    return result["stdout"].strip() or "unknown"


# Short-TTL cache, used ONLY by the dashboard's per-project-per-page-load
# status check (brief §5's noted gap: "dashboard issues a live SSH status
# check for every project on every page load"). Deliberately NOT used by
# unit_status() itself, or by any page the operator navigates to expecting
# fresh truth right after taking an action (project hub, deployment
# summary, wizard health checks) — those must stay always-live. A few
# seconds of staleness on the dashboard's grid view is an acceptable
# tradeoff for not hammering every managed VM with N SSH round trips on
# every dashboard refresh; nothing here is ever used to gate a mutating
# decision, only to render a status label.
_STATUS_CACHE_TTL_SECONDS = 8
_status_cache: dict = {}


async def cached_unit_status(unit: str, host: str = None) -> str:
    import time
    key = (unit, host)
    now = time.monotonic()
    cached = _status_cache.get(key)
    if cached is not None and now - cached[1] < _STATUS_CACHE_TTL_SECONDS:
        return cached[0]
    value = await unit_status(unit, host=host)
    _status_cache[key] = (value, now)
    return value
