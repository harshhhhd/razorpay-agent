"""
main.py — FastAPI app.

Run with:  python -m uvicorn main:app --reload --port 8000
Then open frontend/index.html, or http://localhost:8000/docs for the
interactive OpenAPI docs.
"""
import json
import logging
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import settings as app_settings
from database import init_db, get_conn
from agent import run_analysis_and_generate_campaigns
from rules_engine import (
    check_campaign_against_rules, record_spend, get_remaining_budget,
    get_settings, update_settings, get_spend_history,
)
from razorpay_client import create_offer, set_simulated_failure, is_simulated_failure, RazorpayAPIError
from audit import log_action, get_all_logs
from models import SettingsUpdate, SettingsOut, AdminStatusOut, AnalyzeResultOut

logger = logging.getLogger("growth_copilot.main")

app = FastAPI(
    title="Merchant Growth Copilot",
    description="A bounded agent that mines order data for upsell/clearance "
                 "opportunities. It can only propose campaigns — a rules "
                 "engine and human approval gate every rupee it spends.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=app_settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


@app.get("/health")
def health():
    s = get_settings()
    return {"status": "ok", "budget_remaining_inr": get_remaining_budget(), "daily_budget_inr": s["daily_budget_inr"]}


@app.post("/analyze", response_model=AnalyzeResultOut)
def analyze():
    """Triggers a fresh analysis run. Read-only against order data; never spends money."""
    try:
        return run_analysis_and_generate_campaigns()
    except RazorpayAPIError as e:
        log_action(
            decision="Analysis run failed",
            reasoning=str(e),
            data_snapshot={},
            outcome="failed_no_cache",
        )
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/campaigns")
def list_campaigns(status: str = None):
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM campaigns WHERE status = ? ORDER BY id DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["data_snapshot"] = json.loads(d["data_snapshot"])
        out.append(d)
    return out


@app.post("/campaigns/{campaign_id}/approve")
def approve_campaign(campaign_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign = dict(row)
    if campaign["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Campaign is already '{campaign['status']}'")

    discount_pct = campaign["discount_pct"]

    # HARD GATE: rules engine has final say even after human approval.
    allowed, reason = check_campaign_against_rules(campaign["budget_required_inr"], discount_pct)

    if not allowed:
        conn.execute(
            "UPDATE campaigns SET status='auto_rejected', decided_at=? WHERE id=?",
            (date.today().isoformat(), campaign_id),
        )
        conn.commit()
        conn.close()
        log_action(
            decision=f"Campaign #{campaign_id} human-approved but AUTO-REJECTED by rules engine",
            reasoning=reason,
            data_snapshot=campaign,
            outcome="auto_rejected",
            human_override="merchant_approved",
        )
        raise HTTPException(status_code=409, detail=f"Blocked by safety rules: {reason}")

    try:
        offer = create_offer(campaign["title"], campaign["description"], discount_pct)
    except RazorpayAPIError as e:
        conn.close()
        log_action(
            decision=f"Campaign #{campaign_id} approved but execution failed",
            reasoning=str(e),
            data_snapshot=campaign,
            outcome="execution_failed",
            human_override="merchant_approved",
        )
        raise HTTPException(status_code=503, detail=f"Razorpay offer creation failed: {e}")

    record_spend(campaign["budget_required_inr"])
    conn.execute(
        "UPDATE campaigns SET status='executed', decided_at=? WHERE id=?",
        (date.today().isoformat(), campaign_id),
    )
    conn.commit()
    conn.close()

    log_action(
        decision=f"Campaign #{campaign_id} approved and executed",
        reasoning=f"Merchant approved. Rules engine cleared it: {reason}",
        data_snapshot={"campaign": campaign, "offer": offer},
        outcome="executed",
        human_override="merchant_approved",
    )
    return {"status": "executed", "offer": offer}


@app.post("/campaigns/{campaign_id}/reject")
def reject_campaign(campaign_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign = dict(row)
    conn.execute(
        "UPDATE campaigns SET status='rejected', decided_at=? WHERE id=?",
        (date.today().isoformat(), campaign_id),
    )
    conn.commit()
    conn.close()
    log_action(
        decision=f"Campaign #{campaign_id} rejected by merchant",
        reasoning="Merchant chose not to run this campaign.",
        data_snapshot=campaign,
        outcome="rejected",
        human_override="merchant_rejected",
    )
    return {"status": "rejected"}


@app.get("/audit")
def audit_log(limit: int = 200):
    return get_all_logs(limit)


@app.get("/analytics")
def analytics():
    conn = get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) as n, COALESCE(SUM(expected_lift_inr),0) as lift, "
        "COALESCE(SUM(budget_required_inr),0) as budget "
        "FROM campaigns GROUP BY status"
    ).fetchall()
    conn.close()
    by_status = {r["status"]: {"count": r["n"], "expected_lift_inr": r["lift"], "budget_inr": r["budget"]} for r in rows}
    executed = by_status.get("executed", {"count": 0, "expected_lift_inr": 0, "budget_inr": 0})
    return {
        "by_status": by_status,
        "executed_count": executed["count"],
        "executed_projected_lift_inr": executed["expected_lift_inr"],
        "executed_spend_inr": executed["budget_inr"],
    }


@app.get("/admin/status", response_model=AdminStatusOut)
def admin_status():
    s = get_settings()
    return {
        "simulated_failure_enabled": is_simulated_failure(),
        "budget_remaining_inr": get_remaining_budget(),
        "daily_budget_inr": s["daily_budget_inr"],
        "max_discount_pct": s["max_discount_pct"],
    }


@app.post("/admin/simulate-failure")
def simulate_failure(enabled: bool = True):
    """Flip this on to demo the retry/backoff + stale-cache-fallback flow live."""
    set_simulated_failure(enabled)
    return {"simulated_failure_enabled": enabled}


@app.post("/admin/settings", response_model=SettingsOut)
def change_settings(payload: SettingsUpdate):
    """Lets a merchant tighten or loosen safety guardrails at runtime. Still
    just editing numbers the rules engine enforces in code — the agent
    never gets a say in what these limits are."""
    updated = update_settings(payload.daily_budget_inr, payload.max_discount_pct)
    log_action(
        decision="Merchant updated safety settings",
        reasoning=f"Daily budget cap set to ₹{updated['daily_budget_inr']:.0f}, "
                  f"max discount ceiling set to {updated['max_discount_pct']}%",
        data_snapshot=updated,
        outcome="settings_updated",
        human_override="merchant_configured",
    )
    return updated


@app.get("/analytics/spend-history")
def spend_history(days: int = 7):
    return get_spend_history(days)


@app.get("/products")
def list_products():
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.category, p.price, p.stock,
               COALESCE(SUM(CASE WHEN o.created_at >= ? THEN oi.quantity ELSE 0 END), 0) AS units_sold_30d,
               COALESCE(SUM(CASE WHEN o.created_at >= ? THEN oi.quantity * oi.price ELSE 0 END), 0) AS revenue_30d
        FROM products p
        LEFT JOIN order_items oi ON oi.product_id = p.id
        LEFT JOIN orders o ON o.id = oi.order_id
        GROUP BY p.id
        ORDER BY revenue_30d DESC
        """,
        (cutoff, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/customers")
def list_customers():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.email, c.segment,
               COUNT(DISTINCT o.id) AS order_count,
               COALESCE(SUM(oi.quantity * oi.price), 0) AS total_spend,
               MAX(o.created_at) AS last_order_at
        FROM customers c
        LEFT JOIN orders o ON o.customer_id = c.id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        GROUP BY c.id
        ORDER BY total_spend DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/analytics/revenue-trend")
def revenue_trend(days: int = 14):
    from datetime import date, timedelta
    cutoff_date = date.today() - timedelta(days=days - 1)
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT substr(o.created_at, 1, 10) AS day,
               SUM(oi.quantity * oi.price) AS revenue,
               COUNT(DISTINCT o.id) AS orders
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE substr(o.created_at, 1, 10) >= ?
        GROUP BY day
        ORDER BY day
        """,
        (cutoff_date.isoformat(),),
    ).fetchall()
    conn.close()
    by_day = {r["day"]: {"revenue": r["revenue"], "orders": r["orders"]} for r in rows}
    out = []
    for i in range(days - 1, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        entry = by_day.get(d, {"revenue": 0, "orders": 0})
        out.append({"date": d, "revenue": entry["revenue"], "orders": entry["orders"]})
    return out


@app.get("/analytics/overview")
def analytics_overview():
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    conn = get_conn()
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT o.id) AS orders_30d,
               COALESCE(SUM(oi.quantity * oi.price), 0) AS revenue_30d,
               COUNT(DISTINCT o.customer_id) AS active_customers_30d
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.created_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    total_customers = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    total_products = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    low_stock = conn.execute("SELECT COUNT(*) AS n FROM products WHERE stock <= 20").fetchone()["n"]
    conn.close()

    orders_30d = row["orders_30d"] or 0
    revenue_30d = row["revenue_30d"] or 0
    avg_order_value = round(revenue_30d / orders_30d, 2) if orders_30d else 0

    return {
        "orders_30d": orders_30d,
        "revenue_30d": revenue_30d,
        "avg_order_value": avg_order_value,
        "active_customers_30d": row["active_customers_30d"] or 0,
        "total_customers": total_customers,
        "total_products": total_products,
        "low_stock_products": low_stock,
    }