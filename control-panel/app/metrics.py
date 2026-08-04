"""Per-project resource metrics for the hub page — memory, restart count,
and uptime for each systemd unit, gathered in ONE `systemctl show` call over
the existing SSH channel (app/ssh_client.run). Read-only, no sudo (same class
as the journalctl/is-active calls already used), no agent, no new dependency.

This deliberately REPLACES the per-unit ssh_client.unit_status() loop the hub
page used to do (3 round trips → 1) and returns richer data — the status
string it yields maps onto the same status-active/status-failed/status-inactive
CSS classes the templates already use.
"""

import datetime
import shlex

from app import ssh_client

_PROPS = "ActiveState,SubState,NRestarts,MemoryCurrent,CPUUsageNSec,TasksCurrent,ActiveEnterTimestamp,MainPID"
_UINT64_MAX = 2 ** 64 - 1  # systemd's "unset/accounting-off" sentinel for MemoryCurrent


def service_labels(project: dict) -> list:
    labels = ["gunicorn"]
    if project["uses_celery"]:
        labels.append("celery")
    if project["uses_celerybeat"]:
        labels.append("celerybeat")
    return labels


def _fmt_mem(raw: str) -> str:
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return "n/a"
    # 0, empty, or the uint64 sentinel all mean "no accounting / not running".
    if n <= 0 or n >= _UINT64_MAX:
        return "n/a"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.0f} {unit}" if unit in ("B", "KB") else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _fmt_uptime(ts: str) -> str:
    """ActiveEnterTimestamp looks like 'Fri 2026-07-24 12:58:34 UTC'. VM 100
    runs on UTC (confirmed against its own `date`/journal output), so parse the
    date+time portion as UTC and show a coarse relative age. Falls back to the
    raw string if the format is ever unexpected — never raises."""
    if not ts or ts.strip() in ("", "0"):
        return "—"
    parts = ts.split()
    try:
        dt = datetime.datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return ts
    secs = int((datetime.datetime.utcnow() - dt).total_seconds())
    if secs < 0:
        return "just now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{secs}s"


def _blank(status: str = "n/a") -> dict:
    return {"status": status, "sub": "", "restarts": "—", "mem": "n/a", "uptime": "—"}


async def get_project_metrics(project: dict) -> dict:
    """label -> {status, sub, restarts, mem, uptime}. One SSH round trip for
    every unit the project has. Never raises — on an unreachable host or a
    parse mismatch, every label gets an 'n/a'/'unreachable' placeholder so the
    hub page still renders."""
    labels = service_labels(project)
    units = [f"{label}@{project['name']}" for label in labels]
    cmd = f"systemctl show {' '.join(shlex.quote(u) for u in units)} --no-pager --property={_PROPS}"
    result = await ssh_client.run(cmd, host=project["vm_ip"], timeout=15)

    out = {}
    if result["exit_status"] != 0 and not result["stdout"].strip():
        for label in labels:
            out[label] = _blank("unreachable")
        return out

    # `systemctl show a b` prints one KEY=VALUE block per unit, in argument
    # order, separated by a blank line.
    blocks = [b for b in result["stdout"].split("\n\n") if b.strip()]
    for label, block in zip(labels, blocks):
        props = {}
        for line in block.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        out[label] = {
            "status": props.get("ActiveState", "n/a"),
            "sub": props.get("SubState", ""),
            "restarts": props.get("NRestarts", "0"),
            "mem": _fmt_mem(props.get("MemoryCurrent", "")),
            "uptime": _fmt_uptime(props.get("ActiveEnterTimestamp", "")),
        }
    for label in labels:
        out.setdefault(label, _blank())
    return out
