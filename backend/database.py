"""
database.py

SQLite persistence layer + demo data seeding. All other modules call
get_conn() for a fresh connection (sqlite3 connections aren't safely
shared across FastAPI's threadpool, so we open/close per call rather
than holding one global connection).
"""
import sqlite3
import random
import logging
from datetime import datetime, timedelta

from config import settings

logger = logging.getLogger("growth_copilot.database")


def get_conn():
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            segment TEXT NOT NULL DEFAULT 'regular'
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            created_at TEXT NOT NULL,
            razorpay_order_id TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            data_snapshot TEXT NOT NULL,
            discount_pct REAL NOT NULL,
            audience_estimate INTEGER NOT NULL,
            expected_lift_inr REAL NOT NULL,
            budget_required_inr REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS budget_tracker (
            date TEXT PRIMARY KEY,
            spent_inr REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            decision TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            data_snapshot TEXT NOT NULL,
            outcome TEXT NOT NULL,
            human_override TEXT
        );
        """
    )
    conn.commit()

    existing = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    if existing == 0:
        logger.info("Empty database detected — seeding demo data.")
        _seed_demo_data(conn)
    else:
        logger.info("Database already populated (%s products) — skipping seed.", existing)

    conn.close()


def _seed_demo_data(conn):
    random.seed(42)

    products = [
        ("Cotton Kurta - Indigo", "Apparel", 1299, 45),
        ("Cotton Kurta - Rust", "Apparel", 1299, 12),
        ("Handloom Dupatta", "Apparel", 799, 60),
        ("Leather Sandals", "Footwear", 1899, 30),
        ("Canvas Sneakers", "Footwear", 2199, 8),
        ("Brass Table Lamp", "Home", 2499, 18),
        ("Ceramic Dinner Set", "Home", 3499, 22),
        ("Jute Tote Bag", "Accessories", 599, 70),
        ("Silver Oxidised Earrings", "Accessories", 899, 15),
        ("Block Print Bedsheet", "Home", 1599, 9),
        ("Wooden Coasters Set", "Home", 449, 55),
        ("Linen Table Runner", "Home", 699, 40),
    ]
    conn.executemany(
        "INSERT INTO products (name, category, price, stock) VALUES (?,?,?,?)",
        products,
    )

    first_names = ["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Sneha",
                   "Karan", "Isha", "Aditya", "Meera", "Nikhil", "Divya",
                   "Rahul", "Pooja", "Arjun", "Kavya"]
    last_names = ["Sharma", "Verma", "Patel", "Gupta", "Nair", "Reddy",
                  "Iyer", "Singh", "Rao", "Menon"]

    customers = []
    for i in range(40):
        fn, ln = random.choice(first_names), random.choice(last_names)
        customers.append((
            f"{fn} {ln}",
            f"{fn.lower()}.{ln.lower()}{i}@example.com",
            "high_value" if i < 8 else "regular",
        ))
    conn.executemany(
        "INSERT INTO customers (name, email, segment) VALUES (?,?,?)",
        customers,
    )

    n_customers = 40
    n_products = len(products)
    now = datetime.utcnow()

    for order_num in range(220):
        cust_id = random.randint(1, n_customers)
        days_ago = random.randint(0, 45)
        created_at = (now - timedelta(days=days_ago, hours=random.randint(0, 23))).isoformat()
        cur = conn.execute(
            "INSERT INTO orders (customer_id, created_at) VALUES (?, ?)",
            (cust_id, created_at),
        )
        order_id = cur.lastrowid

        if random.random() < 0.35:
            pid = 1
            qty = random.randint(1, 2)
            price = products[pid - 1][2]
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?,?,?,?)",
                (order_id, pid, qty, price),
            )
            if random.random() < 0.6:
                follow_up_days = random.randint(0, 6)
                follow_created = (now - timedelta(days=max(0, days_ago - follow_up_days))).isoformat()
                cur2 = conn.execute(
                    "INSERT INTO orders (customer_id, created_at) VALUES (?, ?)",
                    (cust_id, follow_created),
                )
                order_id2 = cur2.lastrowid
                pid2 = 3
                conn.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?,?,?,?)",
                    (order_id2, pid2, 1, products[pid2 - 1][2]),
                )
        else:
            n_items = random.randint(1, 3)
            for _ in range(n_items):
                pid = random.randint(1, n_products)
                qty = random.randint(1, 2)
                price = products[pid - 1][2]
                conn.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?,?,?,?)",
                    (order_id, pid, qty, price),
                )

    conn.commit()