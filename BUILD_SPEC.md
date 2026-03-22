# DealMaker v2 — Build Spec

> Status: **DRAFT** — Fill this in with your vision before development begins.

---

## 1. Overview

DealMaker v2 is a **web-based** dealership simulation and data management tool.
Where v1 was a local Python/Tkinter desktop app, v2 is a Flask web interface
designed to give deep, store-level control over the synthetic data that flows
into the TopRep database.

---

## 2. Core Goals

- [ ] Web UI to **create and manage dealership stores**
- [ ] Per-store control over **rep types** (sales rep, manager, BDC), counts, and behaviours
- [ ] Per-store control over **deal types**: pipeline stages, lead sources, financial ranges
- [ ] Per-store control over **activity patterns**: types, outcome distributions, touch frequencies
- [ ] Push simulated events into the **TopRep Supabase DB** in real time via `/api/events`
- [ ] Seed / pre-populate Bayesian priors (`source_stage_priors`) per store
- [ ] View live rep profiles already in TopRep and assign them to stores

---

## 3. New Feature Ideas (fill in your vision here)

### 3.1 Store Templates
> _e.g., "High-volume internet store", "Rural walk-in store", "BDC-heavy phone store"_
- [ ] Pre-built store archetypes the user can select as a starting point
- [ ] Templates set sensible defaults for lead mix, close rates, activity counts, etc.

### 3.2 Rep Persona Profiles
> _Fine-grained control over individual rep behaviour, not just store averages_
- [ ] Define rep personas: "Closer", "High-volume low-close", "Slow starter"
- [ ] Assign personas to reps within a store
- [ ] Each persona has its own close_rate, activities_per_deal, response_time

### 3.3 Bayesian Prior Seeding
> _Directly write `source_stage_priors` rows into TopRep for a store_
- [ ] UI form to set prior_alpha / prior_beta per (source, stage) combination
- [ ] Or auto-calculate priors from the simulation parameters

### 3.4 Historical Backfill Mode
> _Generate N months of historical data and push it all at once_
- [ ] Date range picker
- [ ] Bulk send to API or write to JSONL

### 3.5 Live Dashboard
> _See real-time event counts and error rates per store_
- [ ] Events sent / errors over time
- [ ] Per-source breakdown of simulated leads

### 3.6 Store Persistence
> _Stores currently live in memory; they disappear on server restart_
- [ ] Persist store configs to a local SQLite/JSON file
- [ ] Or persist to Supabase as a `sim_stores` config table

---

## 4. Database Tables in Scope

From `APPLY_ALL_MIGRATIONS.sql`, these are the tables this app will interact with:

| Table | Purpose |
|---|---|
| `profiles` | Read rep UUIDs and roles; optionally create test profiles |
| `events` | Primary write target — all simulated activity goes here |
| `deals` | Indirectly populated via events trigger |
| `activities` | Indirectly populated via events |
| `rep_month_stats` | Auto-updated by `events_to_stats_trigger()` |
| `source_stage_priors` | **New** — seed Bayesian priors per store/source/stage |
| `rep_stage_posteriors` | Read-only: monitor how simulation affects posteriors |
| `forecast_runs` | Read-only: verify forecasts are being generated |
| `quotas` | Optional: create quota rows for simulated reps |
| `reps` | Optional: create/update `store_id` on rep rows |

---

## 5. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Flask 3.x | Already scaffolded in v2 |
| Frontend | HTML + vanilla JS + CSS | Dark theme, no framework dependency |
| DB client | stdlib `urllib` → Supabase REST | No extra packages needed |
| Persistence | In-memory (v2.0) → file/DB (later) | Store configs |
| Auth | `.env` JWT token | Same as v1 |

---

## 6. File Structure (current)

```
DealMaker_v2/
  run.py                        ← Flask entry point  (python run.py)
  requirements.txt
  APPLY_ALL_MIGRATIONS.sql      ← Schema reference
  BUILD_SPEC.md                 ← This file
  .env                          ← Credentials (gitignored)

  dealmaker_generator.py        ← v1 core generator (reused)
  dealmaker_gui.py              ← v1 Tkinter GUI (kept as reference)

  app/
    __init__.py                 ← Flask app factory
    supabase_client.py          ← REST helpers for TopRep DB
    routes/
      stores.py                 ← Store CRUD routes
      simulation.py             ← Start/stop background runners
      settings.py               ← API credentials management

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
    stores/                     ← Per-store JSONL output files
```

---

## 7. Open Questions

- [ ] Should stores be persisted to a local file or to Supabase?
- [ ] Do we want to **create** test profiles in TopRep, or only use existing ones?
- [ ] Should rep persona weights map directly to Bayesian prior values?
- [ ] What level of historical backfill is needed (days / months / years)?
- [ ] Multi-user support needed, or single-user local tool?

---

## 8. Next Steps

1. Fill in section 3 with your vision
2. Answer section 7 open questions
3. Build out the generator extension in `dealmaker_generator.py` to accept
   per-store lead source weights, rep persona close rates, and activity distributions
4. Wire the `source_stage_priors` seeding form to the Supabase REST API
5. Add store persistence (SQLite or Supabase config table)
