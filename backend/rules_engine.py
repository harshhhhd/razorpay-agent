"""
rules_engine.py

Every rule here is plain, deterministic Python — NOT the LLM.
This is the "AI Judgment" line: the model can suggest, but only
this code can approve money movement or spend.

Settings (daily budget cap, max discount ceiling) are mutable at
runtime via /admin/settings so a merchant can tighten or loosen the
guardrails without redeploying — but they are still enforced here in
code, never by the agent itself.
"""
import os
from datetime import date
from database import get_conn

_SETTINGS = {
    "daily_budget_inr": float(os.getenv("DAILY_BUDGET_INR", "8000")),
    "max_discount_pct": float(os.getenv("MAX_DISCOUNT_PCT", "25")),
}


def get_settings() -> dict:
    return dict(_SETTINGS)


def update_settings(daily_budget_inr: float = None, max_discount_pct: float = None) -> dict:
    if daily_budget_inr is not None:
        _SETTINGS["daily_budget_inr"] = float(daily_budget_inr)
    if max_discount_pct is not None:
        _SETTINGS["max_discount_pct"] = float(max_discount_pct)
    return get_settings()


def get_spent_today() -> float:
    conn = get_conn()
    row = conn.execute(
        "SELECT spent_inr FROM budget_tracker WHERE date = ?", (str(date.today()),)
    ).fetchone()
    conn.close()
    return row["spent_inr"] if row else 0.0


def get_remaining_budget() -> float:
    return max(0.0, _SETTINGS["daily_budget_inr"] - get_spent_today())


def record_spend(amount_inr: float):
    conn = get_conn()
    today = str(date.today())
    conn.execute(
        "INSERT INTO budget_tracker (date, spent_inr) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET spent_inr = spent_inr + excluded.spent_inr",
        (today, amount_inr),
    )
    conn.commit()
    conn.close()


def get_spend_history(days: int = 7) -> list:
    """Returns spend per day for the last `days` days, zero-filled for days with no spend."""
    from datetime import timedelta
    conn = get_conn()
    rows = conn.execute("SELECT date, spent_inr FROM budget_tracker").fetchall()
    conn.close()
    by_date = {r["date"]: r["spent_inr"] for r in rows}
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"date": d, "spent_inr": by_date.get(d, 0.0)})
    return out


def check_campaign_against_rules(budget_required_inr: float, discount_pct: float):
    """
    Returns (allowed: bool, reason: str). This is the hard safety gate
    that runs BEFORE any campaign can move from 'approved' to 'executed',
    even after a human has clicked approve. A human approving something
    that violates a hard rule still gets blocked — the rule wins.
    """
    max_discount = _SETTINGS["max_discount_pct"]
    if discount_pct > max_discount:
        return False, f"Discount {discount_pct}% exceeds hard ceiling of {max_discount}%"

    remaining = get_remaining_budget()
    if budget_required_inr > remaining:
        return False, (
            f"Requires ₹{budget_required_inr:.0f} but only ₹{remaining:.0f} "
            f"remains of today's ₹{_SETTINGS['daily_budget_inr']:.0f} cap"
        )

    return True, "Within budget and discount limits"