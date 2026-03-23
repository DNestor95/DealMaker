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

### 3.2 Rep Archetypes & Performance Variance
> _National averages are the mean, but the real spread is what stress-tests your probability engine_

The existing generator uses a single store-wide close rate (`0.36`). That collapses all rep variance and makes it impossible to validate that probability scores actually differentiate between strong and struggling reps.

**Four archetypes to seed:**

| Archetype | `close_rate_mult` | `activity_mult` | `pipeline_advance_rate` | Notes |
|---|---|---|---|---|
| Rockstar | 2.5x | 1.2x | 0.97 | Top 5–10% of reps nationally |
| Solid Mid | 1.0x | 1.0x | 0.88 | The store baseline |
| Underperformer | 0.3x | 0.8x | 0.65 | At risk of missing quota every month |
| New Hire (Ramping) | 0.2x → 1.0x | 0.7x → 1.0x | 0.55 → 0.88 | Curve over 6 months in role |

**Ramping curve for New Hire:**
- Multiplier = `min(1.0, 0.2 + (months_in_role / 6) * 0.8)`
- Needs a `hire_date` parameter per rep so the generator can compute `months_in_role` at simulation time.

**Generator changes required:**
- `TeamMember` gets an optional `archetype: str` field and `hire_date: date | None`.
- `generate_deal_workflow` reads archetype multipliers before setting close probability and activity count.
- The `close_won = rng.random() < 0.36` line becomes `rng.random() < base_close_rate * archetype.close_rate_mult`.
- `activity_count = rng.randint(2, 6)` similarly scaled by `archetype.activity_mult`.

**Why it matters for TopRep:** if a Rockstar and an Underperformer fed through the same store produce indistinguishable posterior distributions, the Bayesian engine isn't learning anything useful. These archetypes give you a ground-truth spread to validate against.

### 3.3 Bayesian Prior Seeding
> _Directly write `source_stage_priors` rows into TopRep for a store_
- [ ] UI form to set prior_alpha / prior_beta per (source, stage) combination
- [ ] Or auto-calculate priors from the simulation parameters

### 3.3b Month-Shape Realism
> _Car dealership sales are famously back-loaded — mirror that in the generator_

The current generator distributes deals uniformly across days with Gaussian noise around `daily_leads`. That produces a flat month, which is nothing like reality. In real stores, the last 5 business days of a month often account for 30–40% of closed deals. The Monte Carlo simulation needs this pattern to get an honest workout.

**Implementation approach — weighted day sampling:**

Replace the flat loop in `generate_events` with a per-day weight drawn from the `MONTH_SHAPE_WEIGHTS` table below. Weights are cumulative pressure multipliers; they are normalised at runtime so total leads still equals the configured monthly target.

```
Day-of-month weight buckets (illustrative):
  Days  1–5   → weight 0.6   (slow open, carryover from prior month close)
  Days  6–15  → weight 0.8   (building)
  Days 16–22  → weight 1.0   (active pipeline)
  Days 23–26  → weight 1.5   (urgency starts)
  Days 27–28  → weight 2.2   (pre-crunch)
  Days 29–EOM → weight 3.5   (the push — last 2-3 days)
```

These become a `daily_weight(day_of_month, month_shape: str)` helper. `month_shape` is a named curve selectable per store:
- `"realistic"` — the back-loaded distribution above
- `"flat"` — today's uniform distribution (default, preserves backward compat)
- `"front_loaded"` — rare, but relevant for fleet/corporate accounts that front-run the month

**Why it matters for simulation:** a rep at 60% pace on day 20 of a realistic-curve month is in much better shape than on a flat month — your forecast engine's confidence intervals need to reflect that.

### 3.4 Historical Backfill Mode
> _Generate N months of historical data and push it all at once_
- [ ] Date range picker
- [ ] Bulk send to API or write to JSONL

### 3.5 Live Dashboard
> _See real-time event counts and error rates per store_
- [ ] Events sent / errors over time
- [ ] Per-source breakdown of simulated leads

### 3.5b Configurable Stress Scenarios
> _The edge cases where a rep is most at risk of missing quota — exactly where the engine needs to shine_

A "scenario" is a named parameter overlay that modifies one or more simulation dimensions for a given run. Scenarios can be stacked (e.g. "slow month" + "BDC underperforming" simultaneously). The generator applies them after base store params are loaded, so they require no new data model — just a `scenarios: list[str]` argument.

**Built-in scenarios:**

| Scenario key | What it modifies | Realistic trigger |
|---|---|---|
| `slow_industry_month` | Lead volume × 0.65, close rate × 0.85 | Macro headwinds, rate spike |
| `manager_on_vacation` | Disables manager-event generation; pipeline advance rate × 0.80 | Literally a vacation |
| `bdc_underperforming` | BDC appointment-set outcome probability × 0.25; scheduled activity volume halved | Staff turnover, phone system issues |
| `inventory_shortage` | `vehicle_unavailable` loss reason probability → 40% of all losses | Supply chain gap |
| `strong_incentive_month` | Close rate × 1.30, deal amount floor raised +$2k | Manufacturer cash on hood |
| `high_heat_weekend` | Days 1–2 of sim get 2.5x leads (weekend event) | Sales event / tent sale |

**Config structure (JSON):**
```json
{
  "scenarios": ["slow_industry_month", "bdc_underperforming"],
  "scenario_overrides": {
    "slow_industry_month": { "lead_volume_mult": 0.60 }
  }
}
```

**Generator changes required:**
- A `ScenarioConfig` dataclass that holds multipliers for each dimension.
- A `SCENARIO_REGISTRY: dict[str, ScenarioConfig]` mapping scenario keys to defaults.
- `apply_scenarios(base_params, scenarios, overrides)` merges them left-to-right.
- `generate_events` accepts a `scenarios` argument and passes the resolved config into `generate_deal_workflow`.

**Why it matters for TopRep:** good quota forecasting is most valuable precisely when the environment degrades. A simulation that only generates "normal" months never validates whether the engine degrades gracefully or produces false confidence.

### 3.6 Store Persistence
> _Stores currently live in memory; they disappear on server restart_
- [ ] Persist store configs to a local SQLite/JSON file
- [ ] Or persist to Supabase as a `sim_stores` config table

### 3.7 Rep User Provisioning & QA Login Credentials
> _Each simulated rep should be a real, login-able TopRep user so QA can verify their perspective inside the app_

When DealMaker creates a store, it currently generates synthetic UUIDs that don't correspond to real `auth.users` rows. That means no one can actually log in as that rep to see what they'd see in TopRep. This feature provisions real Supabase auth users, wires them to the store's lead pool, and returns a credential sheet for QA.

---

**Provisioning flow (per store creation):**

```
For each rep in the store config:
  1. POST /auth/v1/admin/users          ← Supabase Admin Auth API (service_role key)
       { email, password, email_confirm: true }
  2. UPSERT profiles                    ← set role, first_name, last_name, store_id
  3. UPSERT reps                        ← set store_id (routes lead pool assignment)
  4. Collect { email, password, user_id } → credential sheet
```

**Email naming convention:**
```
sim-{store_slug}-{archetype_prefix}{n}@dealmaker.test
  e.g. sim-riverside-ford-rock1@dealmaker.test   (Rockstar #1)
       sim-riverside-ford-mid2@dealmaker.test    (Solid Mid #2)
       sim-riverside-ford-new1@dealmaker.test    (New Hire #1)
```

The `@dealmaker.test` domain signals these are test accounts. DealMaker should refuse to provision against an email domain that isn't an obvious test domain (or require explicit confirmation) to avoid polluting production.

**Password policy for generated credentials:**
- 16-char random alphanumeric — strong enough to satisfy Supabase's default policy
- Generated with `secrets.token_urlsafe(12)` (stdlib, no dependencies)
- Displayed once in the credential sheet; never stored in DealMaker's own persistence layer

---

**Lead pool assignment:**

The `leads` table routes inbound leads to a store via `store_id`, and assigns them to reps via `rep_id`. After provisioning:
- Each rep's `profiles.store_id` is set to the store UUID
- Each rep's `reps.store_id` is set to the store UUID
- This makes them eligible for TopRep's existing round-robin / smart assignment logic without any new schema changes

---

**Credential sheet — UI output:**

After provisioning, the store detail page shows a collapsible **QA Login Credentials** panel:

| Rep Name | Archetype | Email | Password | TopRep Direct Login |
|---|---|---|---|---|
| Sales Rep 1 | Rockstar | sim-riverside-ford-rock1@dealmaker.test | `Xk9mP2…` | [link] |
| Sales Rep 2 | Solid Mid | sim-riverside-ford-mid1@dealmaker.test | `rT4nJw…` | [link] |

- Passwords are shown as masked text with a reveal toggle (eye icon)
- A **"Copy all as JSON"** button dumps the full credential array to the clipboard
- A **"Download CSV"** button produces a file safe to share with QA team
- The direct login link is `{TOPREP_APP_URL}/login?email={encoded_email}` — pre-fills the email, QA only needs to paste the password

---

**New `.env` variable required:**

```
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

The Admin Auth API requires the `service_role` key, not the anon key. DealMaker's settings page should:
- Show a validation warning if `SUPABASE_SERVICE_ROLE_KEY` is missing
- Confirm the key is present before enabling the "Provision Users" button on a store
- **Never** send the service role key to the frontend — all provisioning calls must happen server-side in Flask

**TopRep app URL variable (for the login link):**
```
TOPREP_APP_URL=https://your-toprep-app.vercel.app
```

---

**`supabase_client.py` additions needed:**
```python
def admin_create_user(email: str, password: str) -> dict:
    """POST /auth/v1/admin/users using service_role key."""
    ...

def provision_store_reps(store_config: dict) -> list[dict]:
    """Create auth users + profiles + reps rows for all reps in a store."""
    ...

def deprovision_store_reps(store_id: str) -> dict:
    """Delete auth users whose email matches sim-{store_slug}-* pattern."""
    ...
```

The `deprovision` function is critical — stores have a "Delete users" button so test accounts don't accumulate indefinitely in the Supabase project.

---

**Security notes:**
- Service role key is read from env server-side only; no route should expose it in a response
- Generated passwords are ephemeral and only transmitted over HTTPS to the provisioning API
- Credential sheet download sets `Content-Disposition: attachment` — never rendered inline
- Provisioned users should have `email_confirm: true` set at creation so they bypass email verification flow in TopRep

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
- [ ] Should rep archetype weights map directly to Bayesian prior alpha/beta values? (Rockstar → high alpha; Underperformer → low alpha, higher beta?)
- [ ] What level of historical backfill is needed (days / months / years)?
- [ ] Multi-user support needed, or single-user local tool?
- [ ] Should `month_shape` be configurable per-store in the UI, or only per simulation run?
- [ ] Do scenarios need to be saveable as part of store config, or are they always run-time options?
- [ ] Should provisioned users be re-usable across simulation runs, or torn down and re-created each time a store is reset?
- [ ] What is the TopRep app URL (`TOPREP_APP_URL`) for the direct login links in the credential sheet?
- [ ] Should the credential CSV be encrypted / password-protected, or is plain CSV acceptable for the QA workflow?

---

## 8. Next Steps

### Phase 1 — Generator enhancements (no UI changes needed)
1. Add `archetype` field to `TeamMember`; define `ARCHETYPES` registry with the four multiplier sets
2. Thread archetype close rate and activity multipliers through `generate_deal_workflow`
3. Implement `daily_weight()` helper; add `month_shape` param to `generate_events`
4. Add `ScenarioConfig` + `SCENARIO_REGISTRY`; wire `apply_scenarios()` into `generate_events`

### Phase 2 — Bayesian prior seeding
5. Wire the `source_stage_priors` seeding form to the Supabase REST API
6. Auto-calculate priors from archetype close rates (Rockstar archetype → prior alpha = close_rate × N; Underperformer → lower alpha)

### Phase 3 — UI + persistence
7. Add archetype and scenario controls to the store detail page
8. Add store persistence (SQLite or Supabase config table)
9. Build historical backfill flow with date range picker

### Phase 4 — Rep user provisioning
10. Add `SUPABASE_SERVICE_ROLE_KEY` and `TOPREP_APP_URL` to settings page validation
11. Implement `admin_create_user` + `provision_store_reps` in `supabase_client.py`
12. Add "Provision Users" button to store detail page; display QA credential sheet with reveal/copy/download
13. Implement `deprovision_store_reps` + "Delete users" button for cleanup
