"""
audit.py

Append-only audit trail. Every agent proposal, every human approve/reject,
and every settings change gets logged here. Insert-only by design — nothing
here ever updates or deletes a row, which is the point of an audit trail.
"""
import json
import uuid
import logging
from datetime import datetime

from database import get_conn

logger = logging.getLogger("growth_copilot.audit")


def log_action(decision: str, reasoning: str, data_snapshot: dict,
                outcome: str, human_override: str = None) -> str:
    conn = get_conn()
    action_id = f"act_{uuid.uuid4().hex[:10]}"
    conn.execute(
        """INSERT INTO audit_log
           (action_id, timestamp, decision, reasoning, data_snapshot, outcome, human_override)
           VALUES (?,?,?,?,?,?,?)""",
        (
            action_id,
            datetime.utcnow().isoformat(),
            decision,
            reasoning,
            json.dumps(data_snapshot, default=str),
            outcome,
            human_override,
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Audit log: [%s] %s -> %s", action_id, decision, outcome)
    return action_id


def get_all_logs(limit: int = 200) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data_snapshot"] = json.loads(d["data_snapshot"])
        except (TypeError, json.JSONDecodeError):
            pass
        out.append(d)
    return out