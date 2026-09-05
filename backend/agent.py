"""
agent.py

The "brain" — and what it's explicitly NOT allowed to do:
  - it never touches money directly
  - it never calls create_offer() itself
  - it never sets discount % beyond the hard max_discount_pct ceiling

Two responsibilities:
  1. Deterministic pattern mining over order data (co-purchase pairs,
     slow-moving inventory) — plain Python, no LLM, fully reproducible.
  2. Optional LLM call (Anthropic) purely to phrase campaign copy. If no
     ANTHROPIC_API_KEY is set, falls back to a deterministic template so
     the app works with zero external dependencies for a demo.

Every recommendation carries the data it looked at, the plain-English
reasoning, and a budget/lift estimate — nothing here is a black box.
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from razorpay_client import fetch_orders_with_retry
from rules_engine import get_settings
from database import get_conn
from audit import log_action
from config import settings as app_settings


from config import settings as app_settings

logger = logging.getLogger("growth_copilot.agent")


def _llm_copy(prompt: str, fallback: str) -> str:
    """
    Optional: use an LLM (Groq, llama-3.3-70b-versatile) to write nicer
    campaign copy. Deliberately scoped to text generation only — it never
    touches discount %, budget, or audience numbers, all of which come
    from the deterministic pattern-mining above. If Groq is unreachable,
    slow, or returns something unusable, this falls back to a template
    string so a flaky LLM call can never break the pipeline.
    """
    if not app_settings.groq_configured:
        return fallback
    try:
        import httpx
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {app_settings.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": app_settings.GROQ_MODEL,
                "temperature": 0.1,
                "max_tokens": 80,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=8,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip accidental markdown fences / quotes the model sometimes adds
        text = text.strip("`\"' \n")
        return text if text else fallback
    except Exception as e:
        # Network error, timeout, rate limit, malformed response — any of
        # these degrade gracefully to the deterministic template rather
        # than surfacing a 500 to the merchant.
        logger.warning("Groq copy generation failed, using fallback: %s", e)
        return fallback


def _mine_copurchase_pairs(orders: list, window_days=7, min_support=4):
    by_customer = defaultdict(list)
    for row in orders:
        by_customer[row["customer_id"]].append(row)

    pair_counts = defaultdict(int)
    pair_examples = {}

    for cust_id, items in by_customer.items():
        items_sorted = sorted(items, key=lambda r: r["created_at"])
        for i, a in enumerate(items_sorted):
            a_time = datetime.fromisoformat(a["created_at"])
            for b in items_sorted[i + 1:]:
                b_time = datetime.fromisoformat(b["created_at"])
                delta = (b_time - a_time).days
                if delta < 0:
                    continue
                if delta > window_days:
                    break
                if a["product_id"] == b["product_id"]:
                    continue
                pair = tuple(sorted((a["product_id"], b["product_id"])))
                pair_counts[pair] += 1
                pair_examples[pair] = (a, b)

    results = []
    for pair, count in pair_counts.items():
        if count >= min_support:
            results.append({"pair": pair, "count": count, "example": pair_examples[pair]})
    results.sort(key=lambda r: -r["count"])
    return results


def _find_slow_moving_inventory(orders: list, stock_threshold=20, lookback_days=30, max_sales=5):
    conn = get_conn()
    products = {p["id"]: dict(p) for p in conn.execute("SELECT * FROM products").fetchall()}
    conn.close()

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    sales_count = defaultdict(int)
    for row in orders:
        if datetime.fromisoformat(row["created_at"]) >= cutoff:
            sales_count[row["product_id"]] += row["quantity"]

    slow = []
    for pid, p in products.items():
        sold = sales_count.get(pid, 0)
        if sold <= max_sales and p["stock"] > 0:
            slow.append({"product": p, "units_sold_last_30d": sold})
    return slow


def run_analysis_and_generate_campaigns():
    fetch_result = fetch_orders_with_retry()
    orders = fetch_result["data"]
    stale = fetch_result["stale"]
    logger.info("Analysis run: %s order-lines, stale=%s", len(orders), stale)

    conn = get_conn()
    products = {p["id"]: dict(p) for p in conn.execute("SELECT * FROM products").fetchall()}

    max_discount = get_settings()["max_discount_pct"]
    created = []

    pairs = _mine_copurchase_pairs(orders)
    for p in pairs[:2]:
        pid_a, pid_b = p["pair"]
        prod_a, prod_b = products[pid_a], products[pid_b]
        support_pct = round(100 * p["count"] / max(1, len({o["customer_id"] for o in orders})), 1)

        discount_pct = min(15, max_discount)
        avg_order_value = prod_b["price"]
        estimated_audience = p["count"] * 3
        expected_lift = round(estimated_audience * avg_order_value * 0.35, 2)
        budget_required = round(estimated_audience * avg_order_value * (discount_pct / 100), 2)

        reasoning = (
            f"Found {p['count']} instances (~{support_pct}% of active customers) where a customer "
            f"bought '{prod_a['name']}' and then '{prod_b['name']}' within 7 days. "
            f"Pattern mined from {len(orders)} order-line records."
        )
        fallback_desc = (
            f"Offer {discount_pct}% off '{prod_b['name']}' to customers who bought "
            f"'{prod_a['name']}' in the last 30 days."
        )
        description = _llm_copy(
            f"Write one punchy sentence (under 25 words) pitching this ecommerce upsell offer to a merchant: "
            f"{discount_pct}% off {prod_b['name']} for recent buyers of {prod_a['name']}.",
            fallback_desc,
        )

        snapshot = {
            "product_a": prod_a["name"], "product_b": prod_b["name"],
            "pattern_count": p["count"], "orders_analyzed": len(orders),
            "data_freshness": "stale_cache" if stale else "live",
        }

        cid = _insert_campaign(
            conn, "copurchase_upsell",
            title=f"Upsell: {prod_a['name']} buyers -> {prod_b['name']}",
            description=description, reasoning=reasoning, snapshot=snapshot,
            discount_pct=discount_pct, audience_estimate=estimated_audience,
            expected_lift=expected_lift, budget_required=budget_required,
        )
        created.append(cid)
        log_action(
            decision=f"Proposed campaign #{cid}: co-purchase upsell",
            reasoning=reasoning, data_snapshot=snapshot, outcome="pending_approval",
        )

    slow_items = _find_slow_moving_inventory(orders)
    for item in slow_items[:1]:
        prod = item["product"]
        discount_pct = min(20, max_discount)
        estimated_audience = 25
        expected_lift = round(estimated_audience * prod["price"] * 0.30, 2)
        budget_required = round(estimated_audience * prod["price"] * (discount_pct / 100), 2)

        reasoning = (
            f"'{prod['name']}' sold only {item['units_sold_last_30d']} units in the last 30 days "
            f"against {prod['stock']} units in stock — below the 5-unit velocity threshold used to flag slow movers."
        )
        fallback_desc = f"Clear slow-moving stock: {discount_pct}% off '{prod['name']}' for high-value customers."
        description = _llm_copy(
            f"Write one punchy sentence (under 25 words) pitching a clearance offer: "
            f"{discount_pct}% off {prod['name']}, currently slow-moving inventory.",
            fallback_desc,
        )
        snapshot = {
            "product": prod["name"], "stock": prod["stock"],
            "units_sold_last_30d": item["units_sold_last_30d"],
            "data_freshness": "stale_cache" if stale else "live",
        }
        cid = _insert_campaign(
            conn, "slow_inventory_clearance",
            title=f"Clearance: {prod['name']}",
            description=description, reasoning=reasoning, snapshot=snapshot,
            discount_pct=discount_pct, audience_estimate=estimated_audience,
            expected_lift=expected_lift, budget_required=budget_required,
        )
        created.append(cid)
        log_action(
            decision=f"Proposed campaign #{cid}: slow-moving inventory clearance",
            reasoning=reasoning, data_snapshot=snapshot, outcome="pending_approval",
        )

    conn.close()
    return {"created_campaign_ids": created, "stale_data": stale, "orders_analyzed": len(orders)}


def _insert_campaign(conn, ctype, title, description, reasoning, snapshot,
                      discount_pct, audience_estimate, expected_lift, budget_required):
    cur = conn.execute(
        """INSERT INTO campaigns
           (campaign_type, title, description, reasoning, data_snapshot,
            discount_pct, audience_estimate,
            expected_lift_inr, budget_required_inr, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)""",
        (ctype, title, description, reasoning, json.dumps(snapshot),
         discount_pct, audience_estimate,
         expected_lift, budget_required, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return cur.lastrowid