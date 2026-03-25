# DealMaker

Generates synthetic dealership CRM traffic so you can test `totrep` with realistic event streams.

## What it simulates

- `deal.created`
- `deal.status_changed`
- `activity.scheduled`
- `activity.completed`
- `rep_quota_updated`

All output follows the TOP REP canonical event envelope from `REALTIME_DATA_INGEST_REFERENCE.md`.

## API-first smoke test

### Step 1 — generate a tiny reproducible dataset

```bash
python dealmaker_generator.py \
  --days 1 --daily-leads 3 --seed 42 \
  --delivery file \
  --output output/events_smoke.jsonl
```

### Step 2 — validate the file against the TopRep contract

```bash
python dealmaker_generator.py \
  --days 1 --daily-leads 3 --seed 42 \
  --delivery file \
  --output output/events_smoke.jsonl \
  --validate
```

The `--validate` flag appends a `schema_validation` key to the JSON summary. When all events pass, `"passed": true` is printed and the process exits 0. Any violations print to the same JSON block and the process exits 1.

### Step 3 — post directly to the TopRep API

```bash
python dealmaker_generator.py \
  --days 1 --daily-leads 3 --seed 42 \
  --delivery api \
  --api-url https://<your-domain>/api/events \
  --auth-token <your-jwt>
```

Add `--validate` to also run the local schema check before posting:

```bash
python dealmaker_generator.py \
  --days 1 --daily-leads 3 --seed 42 \
  --delivery api \
  --api-url https://<your-domain>/api/events \
  --auth-token <your-jwt> \
  --validate
```

### Step 4 — run the automated contract tests

```bash
python -m pytest tests/test_schema_compliance.py -v
```

The test suite (56 tests) proves that:
- All envelope keys (`sales_rep_id`, `type`, `payload`, `created_at`) are present and valid.
- `created_at` is always UTC ISO-8601 with milliseconds and a `Z` suffix (e.g. `2026-03-03T15:04:05.000Z`).
- Every event type is one of the five allowed types.
- Required payload fields are present for every event type.
- `activity_type` is always one of `call|email|meeting|demo|note`.
- `outcome` is always one of the ten allowed outcome values.
- Status fields use only `lead|qualified|proposal|negotiation|closed_won|closed_lost`.
- `gross_profit` (optional per TopRep schema) is included by DealMaker.
- `created_at` (optional per TopRep schema) is always sent for backfill fidelity.
- All five event types are generated in a normal simulation run.

## Verification plan

1. **Local file check** — run Step 1 + Step 2 above; confirm `"passed": true`.
2. **Unit tests** — run Step 4; confirm `56 passed`.
3. **Live API post** — run Step 3 with real credentials; confirm `api_result.sent > 0` and `api_result.failed == 0`.
4. **TopRep side** — verify new rows appear in the `events` table and `rep_month_stats` updates as expected (see §6 of `REALTIME_DATA_INGEST_REFERENCE.md`).

## Quick start

From the project root:

```bash
python dealmaker_generator.py --output output/events.jsonl
```

This writes newline-delimited JSON events to `output/events.jsonl`.

## GUI (multi-store concurrent runner)

Launch the GUI:

```bash
python dealmaker_gui.py
```

The GUI lets you:

- Set `Dealership ID`, `Sales Reps`, `Managers`, and `BDC Agents`
- Configure traffic rate (`Daily Leads`), generation batch size (`Batch Days`), and loop interval (`Every Seconds`)
- Add and start multiple stores at once
- Keep each store running in the background while you add more stores
- Write each store's output continuously to its own file (`output/stores/<DEALERSHIP_ID>.jsonl`)
- Send events directly to TOP REP ingest API (`/api/events`) per store

Controls:

- **Add + Start Store**: opens a full settings dialog for that new store, then starts it
- **Stop Selected**: pauses one store
- **Start Selected**: resumes one store
- **Remove Selected**: stops and removes one store from the GUI list
- **Stop All**: stops every running store

## Common commands

Generate 30 days in JSONL:

```bash
python dealmaker_generator.py --days 30 --daily-leads 35 --format jsonl --output output/events_30d.jsonl
```

Generate CSV:

```bash
python dealmaker_generator.py --days 14 --format csv --output output/events.csv
```

Reproducible output with a fixed seed:

```bash
python dealmaker_generator.py --seed 1337 --output output/events_seeded.jsonl
```

Send directly to TOP REP API only:

```bash
python dealmaker_generator.py --delivery api --api-url https://<your-domain>/api/events --auth-token <jwt>
```

Write file and send to API at the same time:

```bash
python dealmaker_generator.py --delivery both --api-url https://<your-domain>/api/events --auth-token <jwt> --output output/events_live.jsonl
```

Use `.env` (recommended, auto-loaded by CLI and GUI):

```bash
# copy .env.example to .env, then fill values
python dealmaker_generator.py --delivery both --output output/events_live.jsonl
```

## Parameters

- `--start-date` (default: today, `YYYY-MM-DD`)
- `--days` (default: `14`)
- `--daily-leads` (default: `20`)
- `--salespeople` (default: `8`)
- `--managers` (default: `2`)
- `--bdc` (default: `3`)
- `--dealership-id` (default: `DLR-001`)
- `--seed` (default: `42`)
- `--delivery` (`file`, `api`, or `both`; default: `file`)
- `--api-url` (required for `api`/`both`, e.g. `https://<domain>/api/events`)
- `--auth-token` (required for `api`/`both`, or set `TOPREP_AUTH_TOKEN` env var)
- `--format` (`jsonl` or `csv`, default: `jsonl`)
- `--output` (default: `output/events.jsonl`)
- `--validate` (flag; validate generated events against the TopRep API contract and print a compliance report)

## Event schema

Each event includes:

- `sales_rep_id` (UUID)
- `type` (one of allowed TOP REP event types)
- `payload` (type-specific object)
- `created_at` (UTC ISO-8601)

### Allowed event types

- `deal.created`
- `deal.status_changed`
- `activity.scheduled`
- `activity.completed`
- `rep_quota_updated`

## Using with toprep

- If `totrep` accepts JSONL event feeds directly, point it to `output/events.jsonl`.
- If `totrep` expects CSV, generate with `--format csv`.
- If posting directly to TOP REP API, send each line/event to `POST /api/events`.
- For multi-store streams, point `totrep` to one or more files under `output/stores/`.

## Database/API connection setup

Preferred path is TOP REP API (recommended by your reference):

1. Get a valid user JWT for TOP REP.
2. Create a `.env` file in this project (you can copy `.env.example`):
	- `TOPREP_API_URL=https://<your-domain>/api/events`
	- `TOPREP_AUTH_TOKEN=<your-jwt>`
3. Use `--delivery api` or `--delivery both` with `--api-url https://<your-domain>/api/events`.
4. In GUI Add Store dialog, set:
	- `Delivery` = `api` or `both`
	- `API URL` = TOP REP `/api/events` (auto-filled from `.env`)
	- `Auth Token` = JWT (auto-filled from `.env`)

Notes:

- `dealmaker_generator.py` and `dealmaker_gui.py` auto-load `.env` on startup.
- CLI flags still override `.env` values.

### Direct Supabase mode (without `/api/events`)

If `TOPREP_API_URL` is set to `https://<project-ref>.supabase.co`, DealMaker writes to `.../rest/v1/events`.

Required `.env` values for this mode:

- `TOPREP_API_URL=https://<project-ref>.supabase.co`
- `TOPREP_AUTH_TOKEN=<user-jwt>`
- `SUPABASE_ANON_KEY=<sb_publishable_...>`

Important for RLS:

- `TOPREP_AUTH_TOKEN` must be a user JWT (not the `sb_publishable_*` key).
- `sales_rep_id` is auto-derived from JWT `sub` so inserts can satisfy typical RLS policies.

### Supabase Edge Function mode

If your endpoint is a Supabase Edge Function URL like:

- `https://<project-ref>.functions.supabase.co/<function-name>`

DealMaker now sends a **batch** payload in this shape:

```json
{
	"actions": [
		{
			"rep_id": "<sales_rep_id>",
			"deal_id": "<deal_id|null>",
			"action_type": "<event type>",
			"outcome": "<outcome|null>",
			"source": "<source|null>",
			"created_at": "<iso timestamp>"
		}
	]
}
```

Required `.env` values for this mode:

- `TOPREP_API_URL=https://<project-ref>.functions.supabase.co/<function-name>`
- `TOPREP_AUTH_TOKEN=<user-jwt>`
- `SUPABASE_ANON_KEY=<sb_publishable_...>`

### Direct Postgres mode

If you want to bypass HTTP delivery entirely, DealMaker can insert directly into
the `events` table over Postgres.

Required `.env` values for this mode:

- `DATABASE_URL=postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres`

Notes:

- `TOPREP_AUTH_TOKEN` is not required for direct Postgres delivery.
- `TOPREP_API_URL` can be left unset when `DATABASE_URL` is configured.
- The app inserts the same `sales_rep_id`, `type`, `payload`, and `created_at`
  envelope used by the existing HTTP delivery path.
