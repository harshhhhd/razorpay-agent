
An AI agent that analyzes a merchant's order data, finds upsell and
clearance opportunities, and proposes campaigns — but every rupee of
spend is gated by hard-coded rules and a human approval click, with
a full audit trail.


---

## 1. What's inside

```
merchant-growth-copilot/
├── backend/
│   ├── main.py             
│   ├── agent.py            Pattern mining + recommendation generation
│   ├── rules_engine.py     Hard-coded budget caps & discount ceiling (NOT LLM)
│   ├── razorpay_client.py  
│   ├── audit.py            Append-only audit log writer/reader
│   ├── database.py         SQLite schema + seeded demo data (products, orders)
│   └── requirements.txt
└── frontend/
    └── index.html         
```

No API keys are required to run this. Razorpay access is mocked against
seeded SQLite data that mimics what you'd get back from Razorpay's
Orders/Items APIs — the interface (`razorpay_client.py`) is written so you
can swap in the real `razorpay` Python SDK later without touching the
agent or rules logic.

---

## 2. Run it — exact terminal commands

Use `python3` on macOS/Linux, or `python` on Windows — whichever runs on
your machine. If you're not sure, run `python3 --version` or
`python --version` and use whichever one responds.

**Terminal 1 — backend:**
```bash
cd merchant-growth-copilot/backend

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 database.py               # creates & seeds copilot.db (only needed once)
uvicorn main:app --reload --port 8000

# Windows (PowerShell or cmd)
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python database.py
uvicorn main:app --reload --port 8000
```
Leave this running. Visit `http://localhost:8000/docs` to see the raw API (FastAPI's auto-generated Swagger UI).

**Terminal 2 — frontend:**
No build step needed — it's a static file that calls the API directly.
```bash
cd merchant-growth-copilot/frontend

# macOS / Linux
python3 -m http.server 5500

# Windows
python -m http.server 5500
```
You'll see `Serving HTTP on :: port 5500 ...` — that means it's working.
Now open **`http://localhost:5500`** in your browser (not the terminal —
the terminal just keeps the server running; the site opens in the browser).


---

## 3. Demo script (matches the failure-recovery requirement)

1. Click **Run Analysis** — the agent mines the seeded order history and
   proposes 2–3 campaigns (a co-purchase upsell and a slow-inventory
   clearance), each showing its reasoning, data source, expected lift,
   and required budget.
2. Click **Approve** on one — it passes the rules engine (budget +
   discount ceiling check) and gets "executed" via the mock
   `create_offer()` call. Check the **Audit Log** tab — you'll see the
   full decision trail.
3. Approve a second, higher-cost campaign — if it would exceed the
   ₹5,000/day cap, it gets auto-rejected **even though you approved it**,
   because the rules engine has final say over the LLM/human layer. This
   is the "AI Judgment" line from the brief: a human or model can suggest
   spend, only deterministic code can authorize it.
4. Click **Simulate Razorpay API Failure**, then **Run Analysis** again.
   You'll see the agent retry with exponential backoff, then fall back to
   the last-known-good cached data — flagged as stale in both the UI and
   the audit log — rather than crashing or inventing numbers.
5. Click **Restore Razorpay API** to turn the simulated outage off again.

---

## 4. Where the LLM is (and isn't) used

- `agent.py`'s `_llm_copy()` optionally calls the Anthropic API to turn a
  mined pattern into punchy campaign copy. Set `ANTHROPIC_API_KEY` as an
  environment variable to enable it:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  ```
  Without a key, it falls back to a deterministic template — the app is
  fully functional either way, which is worth calling out in your demo
  video as a deliberate design choice (never let a copy-generation call
  become a single point of failure for the whole pipeline).
- Pattern mining (co-purchase detection, slow-inventory detection),
  budget checks, discount ceilings, and spend recording are **all plain
  Python in `rules_engine.py` and `agent.py`'s mining functions** — no
  LLM call touches a number that affects money.

---

## 5. Config knobs

Environment variables (all optional, sensible defaults baked in):

| Variable | Default | Effect |
|---|---|---|
| `DAILY_BUDGET_INR` | 8000 | Hard daily spend cap enforced by `rules_engine.py` |
| `MAX_DISCOUNT_PCT` | 25 | Hard ceiling on any discount the agent can propose |
| `ANTHROPIC_API_KEY` | unset | Enables LLM-generated campaign copy (optional) |

Example:
```bash
DAILY_BUDGET_INR=10000 MAX_DISCOUNT_PCT=20 uvicorn main:app --reload --port 8000
```



