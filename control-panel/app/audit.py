from app.db import get_conn


def log_action(actor: str, action: str, target: str = "", details: str = "", result: str = "ok"):
    """Every mutating action against a managed VM must call this. Read-only
    inspection does not need to be logged — this is for the 'what changed
    and who did it' trail the brief calls for in §5."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, details, result) VALUES (?, ?, ?, ?, ?)",
            (actor, action, target, details, result),
        )
