# Copilot Instructions for DealMaker

## Overview

DealMaker is a **Flask web application** that generates synthetic dealership CRM event streams for testing the TopRep sales-intelligence platform. It simulates realistic deal lifecycles, rep activity, and quota events and can deliver them either as JSONL files or directly to the TopRep ingest API (`POST /api/events`).

A legacy Tkinter desktop GUI (`dealmaker_gui.py`) is kept as reference; all active development targets the Flask web interface.

---

## Architecture

```
run.py                        ← Flask entry point  (`python run.py`)
dealmaker_generator.py        ← Core event generator (shared by CLI, GUI, and Flask)
dealmaker_gui.py              ← Legacy Tkinter GUI (reference only)
requirements.txt              ← Flask 3.x + python-dotenv

app/
  __init__.py                 ← Flask app factory (create_app)
  supabase_client.py          ← Supabase/TopRep REST helpers
  routes/
    stores.py                 ← Store CRUD (/, /stores/new, /stores/<id>)
    simulation.py             ← Start/stop background simulation runners
    settings.py               ← .env credential management (/settings)

templates/
  base.html
  settings.html
  404.html
  stores/
    list.html
    new.html
    detail.html

static/
  css/main.css
  js/main.js

output/
  stores/                     ← Per-store JSONL output files (git-ignored)
```

**Key design decisions:**
- Stores are currently persisted in-memory (`_stores: dict` in `routes/stores.py`); they are reset on server restart. A JSON file or Supabase config table is planned.
- The Flask app uses the **application factory** pattern (`create_app()`); blueprints register routes.
- The generator (`dealmaker_generator.py`) is intentionally decoupled — it can be run as a CLI, imported by the Flask app, or driven by the legacy GUI.

---

## Tech Stack

| Layer       | Choice                          | Notes                                   |
|-------------|---------------------------------|-----------------------------------------|
| Backend     | Flask 3.x                       | App factory + blueprints                |
| Frontend    | HTML + vanilla JS + CSS         | Dark theme, no framework dependency     |
| DB client   | `urllib` → Supabase REST        | No extra packages; stdlib only          |
| Persistence | In-memory (current) → file/DB   | Planned: JSON file or Supabase table    |
| Auth        | `.env` JWT token                | `TOPREP_AUTH_TOKEN` env var             |

---

## Running the App

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env

# Start the Flask dev server
python run.py
```

The app listens on `http://127.0.0.1:5000` by default.

### CLI generator (no Flask needed)

```bash
# Generate 14 days of JSONL events to file
python dealmaker_generator.py --output output/events.jsonl

# Generate 30 days, send to TopRep API
python dealmaker_generator.py --days 30 --delivery api \
  --api-url https://<domain>/api/events --auth-token <jwt>

# Reproducible run with fixed seed
python dealmaker_generator.py --seed 1337 --output output/events_seeded.jsonl
```

### Legacy GUI

```bash
python dealmaker_gui.py
```

---

## Environment Variables

Defined in `.env` (copy from `.env.example`). All are optional — the app degrades gracefully when they are absent.

| Variable                  | Required for           | Notes                                      |
|---------------------------|------------------------|--------------------------------------------|
| `TOPREP_API_URL`          | API delivery           | TopRep `/api/events` or Supabase REST URL  |
| `TOPREP_AUTH_TOKEN`       | API delivery           | User JWT (not the anon key)                |
| `SUPABASE_ANON_KEY`       | Direct Supabase mode   | `sb_publishable_*` key                     |
| `SUPABASE_SERVICE_ROLE_KEY` | Rep provisioning     | Admin Auth API — **never expose to frontend** |
| `TOPREP_APP_URL`          | QA credential links    | e.g. `https://your-toprep-app.vercel.app`  |
| `TOPREP_SALES_REP_ID`     | Optional override      | Normally auto-derived from JWT `sub`       |

> **Security rule:** `SUPABASE_SERVICE_ROLE_KEY` must never be read in a route handler that returns a response to the browser. All Admin Auth API calls must happen entirely server-side.

---

## Coding Conventions

- **Python version:** 3.11+; use `from __future__ import annotations` for forward references.
- **Type hints:** add type hints to all new functions and methods.
- **Imports:** stdlib → third-party → local, separated by blank lines.
- **Blueprints:** each route module exposes a single `bp = Blueprint(...)`. Register it in `create_app()`.
- **Templates:** use `render_template` with explicit keyword arguments; keep logic out of templates.
- **Secrets:** read credentials from `os.getenv(...)` only; never hard-code tokens or keys.
- **Error handling:** return a rendered `404.html` (with HTTP 404) for missing resources; use Flask `abort()` for other HTTP errors.
- **In-memory state:** the `_stores` dict in `stores.py` is intentionally simple. When adding persistence, keep the dict as the authoritative runtime cache and sync to/from the persistent store on reads/writes.
- **Generator:** `dealmaker_generator.py` must remain runnable as a standalone CLI script (`if __name__ == "__main__": main()`). Do not add Flask-specific imports to it.

---

## Simulated Event Types

All events follow the TopRep canonical envelope (see `REALTIME_DATA_INGEST_REFERENCE.md`):

| Event type           | Key payload fields                              |
|----------------------|-------------------------------------------------|
| `deal.created`       | `deal_id`, `source`, `stage`, `amount`          |
| `deal.status_changed`| `deal_id`, `from_stage`, `to_stage`             |
| `activity.scheduled` | `deal_id`, `activity_type`                      |
| `activity.completed` | `deal_id`, `activity_type`, `outcome`           |
| `rep_quota_updated`  | `quota_amount`, `period`                        |

Each event includes: `sales_rep_id` (UUID), `type`, `payload`, `created_at` (UTC ISO-8601).

---

## Key Files to Know

| File                        | What it does                                              |
|-----------------------------|-----------------------------------------------------------|
| `dealmaker_generator.py`    | All simulation logic: deals, activities, quotas           |
| `app/supabase_client.py`    | REST wrappers: `get_profiles()`, `rest_get()`, etc.       |
| `app/routes/stores.py`      | Store CRUD + in-memory `_stores` registry                 |
| `app/routes/simulation.py`  | Background thread management for per-store runners        |
| `app/routes/settings.py`    | `.env` read/write; never exposes raw secrets in responses |
| `BUILD_SPEC.md`             | Full feature roadmap and planned architecture             |
| `APPLY_ALL_MIGRATIONS.sql`  | TopRep database schema reference                          |
