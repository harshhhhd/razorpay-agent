"""
razorpay_client.py

Only create_offer() actually talks to Razorpay — that's the single
money-adjacent call, gated behind human approval + the rules engine.
Order-history mining reads from the store's own orders/order_items
tables, since Razorpay's API doesn't expose product-level co-purchase
history — that's the merchant's own sales data.
"""
import time
import json
import logging
from datetime import datetime

import razorpay

from database import get_conn
from config import settings

logger = logging.getLogger("growth_copilot.razorpay_client")

_client = None
if settings.razorpay_configured:
    _client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    logger.info("Razorpay client initialized (test mode key: %s...)", settings.RAZORPAY_KEY_ID[:12])
else:
    logger.warning("Razorpay not configured — create_offer() will raise until .env is set.")

_FORCE_FAILURE = {"enabled": False}


def set_simulated_failure(enabled: bool):
    _FORCE_FAILURE["enabled"] = enabled


def is_simulated_failure() -> bool:
    return _FORCE_FAILURE["enabled"]


class RazorpayAPIError(Exception):
    pass


def _fetch_orders_mock():
    if _FORCE_FAILURE["enabled"]:
        raise RazorpayAPIError("Simulated Razorpay API timeout")

    conn = get_conn()
    orders = conn.execute(
        """
        SELECT o.id as order_id, o.customer_id, o.created_at,
               oi.product_id, oi.quantity, oi.price,
               p.name as product_name, p.category, p.stock,
               c.segment
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        JOIN customers c ON c.id = o.customer_id
        ORDER BY o.created_at ASC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in orders]


def fetch_orders_with_retry(max_attempts=3, base_delay=1.0):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            data = _fetch_orders_mock()
            _write_cache("last_known_good_orders", data)
            return {"data": data, "stale": False, "source": "live"}
        except RazorpayAPIError as e:
            last_error = e
            logger.warning("Fetch attempt %s/%s failed: %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(min(base_delay * (2 ** (attempt - 1)), 2))

    cached = _read_cache("last_known_good_orders")
    if cached is not None:
        logger.error("All %s attempts failed. Falling back to cache.", max_attempts)
        return {"data": cached, "stale": True, "source": "cache", "error": str(last_error)}

    raise RazorpayAPIError(
        f"Razorpay API unreachable after {max_attempts} attempts and no cached data available: {last_error}"
    )


def _write_cache(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO cache (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def _read_cache(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
    conn.close()
    return json.loads(row["value"]) if row else None


def create_offer(title: str, description: str, discount_pct: float):
    """
    Creates a real Razorpay test-mode Payment Link for an approved campaign.
    Only ever called after human approval AND the rules engine has cleared
    it — this function has no knowledge of budgets or discount ceilings.
    """
    if _FORCE_FAILURE["enabled"]:
        raise RazorpayAPIError("Simulated failure creating offer")

    if _client is None:
        raise RazorpayAPIError(
            "Razorpay client not configured — set RAZORPAY_KEY_ID and "
            "RAZORPAY_KEY_SECRET in your .env file"
        )

    try:
        payload = {
            "amount": 100,  # ₹1.00 placeholder checkout amount for the test link
            "currency": "INR",
            "description": f"{title} — {description} ({discount_pct}% off)"[:2048],
            "notes": {
                "campaign_title": title,
                "discount_pct": str(discount_pct),
                "source": "growth-copilot",
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        link = _client.payment_link.create(payload)
        logger.info("Razorpay payment link created: %s", link.get("short_url"))
    except razorpay.errors.BadRequestError as e:
        raise RazorpayAPIError(f"Razorpay rejected the request: {e}")
    except Exception as e:
        raise RazorpayAPIError(f"Razorpay API call failed: {e}")

    return {
        "offer_id": link["id"],
        "title": title,
        "description": description,
        "discount_pct": discount_pct,
        "status": link.get("status", "created"),
        "payment_link_url": link.get("short_url"),
    }