# DealMaker

A Flask web app that generates synthetic dealership CRM event streams for testing sales-intelligence platforms. Simulate realistic deal lifecycles, rep activity, and quota events — then deliver them as JSONL files or push them directly to an API.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/DNestor95/DealMaker)

---

## Features

- Simulates five event types: `deal.created`, `deal.status_changed`, `activity.scheduled`, `activity.completed`, `rep_quota_updated`
- Deliver events to a **file** (JSONL or CSV), an **API endpoint**, or **both**
- **Flask web UI** for managing multiple stores and running live simulations
- **CLI generator** for scripted or reproducible runs
- Schema validation against the TopRep canonical event envelope
- Deployable to Vercel or Railway; runs locally with no database required

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials (all optional for local file output)
cp .env.example .env

# Start the Flask web app
python run.py
# → http://127.0.0.1:5000
```

Or use the CLI generator directly to write events to a file:

```bash
python dealmaker_generator.py --output output/events.jsonl
```

---

## CLI Usage

```bash
# Generate 14 days of JSONL events (default)
python dealmaker_generator.py --output output/events.jsonl

# Generate 30 days with higher traffic
python dealmaker_generator.py --days 30 --daily-leads 35 --output output/events_30d.jsonl

# Generate CSV
python dealmaker_generator.py --days 14 --format csv --output output/events.csv

# Reproducible output with a fixed seed
python dealmaker_generator.py --seed 1337 --output output/events_seeded.jsonl

# Post directly to an API
python dealmaker_generator.py --delivery api \
  --api-url https://<your-domain>/api/events \
  --auth-token <your-jwt>

# Write file and post to API simultaneously
python dealmaker_generator.py --delivery both \
  --api-url https://<your-domain>/api/events \
  --auth-token <your-jwt> \
  --output output/events_live.jsonl

# Validate generated events against the TopRep schema
python dealmaker_generator.py --validate --output output/events_smoke.jsonl
```

> `.env` is auto-loaded on startup — CLI flags override `.env` values.

### CLI Parameters

| Flag | Default | Description |
|---|---|---|
| `--start-date` | today | Start date (`YYYY-MM-DD`) |
| `--days` | `14` | Number of days to simulate |
| `--daily-leads` | `20` | Leads per day |
| `--salespeople` | `8` | Number of sales reps |
| `--managers` | `2` | Number of managers |
| `--bdc` | `3` | Number of BDC agents |
| `--dealership-id` | `DLR-001` | Dealership identifier |
| `--seed` | `42` | Random seed for reproducibility |
| `--delivery` | `file` | `file`, `api`, or `both` |
| `--api-url` | — | TopRep `/api/events` endpoint |
| `--auth-token` | — | JWT (or set `TOPREP_AUTH_TOKEN` in `.env`) |
| `--format` | `jsonl` | `jsonl` or `csv` |
| `--output` | `output/events.jsonl` | Output file path |
| `--validate` | flag | Run schema compliance check before/after generation |

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values you need.

| Variable | Required for | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | Vercel deployment | Random hex string for signing sessions |
| `TOPREP_AUTH_TOKEN` | API delivery | Your TopRep user JWT |
| `TOPREP_API_URL` | API delivery | TopRep `/api/events` or Supabase REST URL |
| `SUPABASE_ANON_KEY` | Direct Supabase mode | `sb_publishable_*` key |
| `SUPABASE_SERVICE_ROLE_KEY` | Rep provisioning | Supabase Admin key — **never expose to the browser** |
| `TOPREP_APP_URL` | Optional | Your deployed TopRep app URL |
| `DATABASE_URL` | Direct Postgres mode | Postgres connection string |

Generate a Flask secret key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Event Schema

Every event follows the TopRep canonical envelope:

```json
{
  "sales_rep_id": "<uuid>",
  "type": "<event_type>",
  "payload": { "...": "..." },
  "created_at": "2026-03-03T15:04:05.000Z"
}
```

---

## Tests

```bash
python -m pytest tests/test_schema_compliance.py -v
```

The suite validates envelope structure, field types, allowed values, and that all five event types are generated in a normal run.

---

## Deployment

### Vercel

Import the repo in the Vercel dashboard, set the environment variables above, and deploy. Vercel detects `vercel.json` automatically.

> **Note:** Live simulation (background threads) is not supported on Vercel's serverless platform. Use the **Backfill** feature for historical data, or run on [Railway](https://railway.app) for real-time simulation.

### Railway / local server

```bash
python run.py  # or: gunicorn wsgi:app
```

### Clear the database

```bash
python clear_db.py        # interactive
python clear_db.py --yes  # non-interactive
```

Requires `DATABASE_URL` in `.env`.
