# DealMaker

Generates synthetic dealership CRM traffic so you can test `totrep` with realistic event streams.

## What it simulates

- Lead creation, scoring, assignment
- Outbound/inbound communications (calls, email, SMS)
- Appointment lifecycle (set, confirm, complete, no-show)
- In-store actions (test drive, trade appraisal, credit app)
- Deal desk actions (quote, manager approval)
- Outcomes (closed won/lost), follow-up tasks, notes, status updates

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

## Parameters

- `--start-date` (default: today, `YYYY-MM-DD`)
- `--days` (default: `14`)
- `--daily-leads` (default: `20`)
- `--salespeople` (default: `8`)
- `--managers` (default: `2`)
- `--bdc` (default: `3`)
- `--dealership-id` (default: `DLR-001`)
- `--seed` (default: `42`)
- `--format` (`jsonl` or `csv`, default: `jsonl`)
- `--output` (default: `output/events.jsonl`)

## Event schema

Each event includes:

- `event_id` UUID
- `event_ts` UTC ISO-8601 timestamp
- `source_system` (`dealmaker`)
- `dealership_id`
- `team_member_id`
- `team_member_role` (`sales`, `manager`, `bdc`)
- `action` (CRM activity type)
- `entity` and `entity_id`
- `lead_id`, `opportunity_id`, `customer_id`
- `channel` (`phone`, `email`, `sms`, `in_person`, `system`)
- `result`
- `value` (numeric value where applicable)
- `metadata` (JSON object; CSV stores this as stringified JSON)

## Using with toprep

- If `totrep` accepts JSONL event feeds directly, point it to `output/events.jsonl`.
- If `totrep` expects CSV, generate with `--format csv`.
- If `totrep` expects a different field map, transform from this stable schema (`action`, IDs, timestamp, metadata) in a lightweight adapter step.
- For multi-store streams, point `totrep` to one or more files under `output/stores/`.
