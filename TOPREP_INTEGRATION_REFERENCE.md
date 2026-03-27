# DealMaker ↔ TopRep Integration Reference

This file is the authoritative source-of-truth for how DealMaker synthetic data
maps to the TopRep Supabase database.  Keep it updated whenever DealMaker's data
model or the DB schema changes.  The TopRep agent should read this file before
writing any queries, migrations, or UI code that touches DealMaker-generated rows.

---

## 1. Supabase Project

| Variable | Value |
|---|---|
| Project URL | `https://ahimfdfuuefesgbbnccr.supabase.co` |
| Publishable key | `sb_publishable_SABMCFFXgDOvyvTvJWH0_w_qREoAIpS` |
| Events REST endpoint | `https://ahimfdfuuefesgbbnccr.supabase.co/rest/v1/events` |

DealMaker writes with the **`SUPABASE_SERVICE_ROLE_KEY`** (bypasses RLS).
TopRep app uses the publishable key + user JWT for RLS-filtered reads.

---

## 2. UUID Derivation — The Master Rule

All IDs that DealMaker writes are **deterministic UUID5** values, not random UUIDs.
Both DealMaker and TopRep must derive IDs using the same formula:

```python
import uuid

def stable_uuid(*parts: str) -> str:
    """Deterministic UUID5 (NAMESPACE_URL). The canonical ID formula."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))

def store_uuid(store_id: str) -> str:
    """UUID for a store string like 'dlr-001'."""
    return stable_uuid("store", store_id)

def rep_uuid(dealership_id: str, member_id: str) -> str:
    """UUID for a rep. member_id format: 'S-001' through 'S-NNN'."""
    return stable_uuid("sales_rep", dealership_id, member_id)
```

**Example for `dlr-001`:**

| Entity | Input | UUID |
|---|---|---|
| Store | `stable_uuid("store", "dlr-001")` | `c1d7cbaf-49c6-5977-953c-5eb806f3f85c` |
| Rep S-001 | `stable_uuid("sales_rep", "dlr-001", "S-001")` | `131b00c0-79f5-5a8b-a883-9cff9d116bb0` |
| Rep S-002 | `stable_uuid("sales_rep", "dlr-001", "S-002")` | `2142c913-ab4c-5386-9b03-096c1fdd07af` |
| Rep S-003 | `stable_uuid("sales_rep", "dlr-001", "S-003")` | `eb0002da-9066-5dd2-a4ab-0605ed5c873f` |
| Rep S-004 | `stable_uuid("sales_rep", "dlr-001", "S-004")` | `f45fc38f-a7e1-5afa-b6f8-9da6ba0fa0a7` |
| Rep S-005 | `stable_uuid("sales_rep", "dlr-001", "S-005")` | `cdb7b843-b11a-56e6-94e8-a2baf7f03a7b` |

> **Critical:** DealMaker provisioned Supabase Auth users are created with these exact
> UUIDs via `POST /auth/v1/admin/users` with `{ "id": "<stable_uuid>" }`.
> This ensures `auth.uid() === sales_rep_id` for RLS to work correctly.

---

## 3. Full Database Schema

### `events`  — primary write target for simulations

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` default |
| `sales_rep_id` | uuid | NO | = `rep_uuid(dealership_id, "S-NNN")` |
| `type` | text | NO | See allowed values §4 |
| `payload` | jsonb | NO | See payload shapes §5 |
| `created_at` | timestamptz | NO | UTC; DealMaker sets explicitly |

**RLS INSERT policy:** `sales_rep_id = auth.uid()`
→ With a user JWT this enforces ownership; with the service role key it is bypassed.
→ DealMaker uses the service role key so it can insert events for any rep UUID.

**RLS SELECT policies:**
- Sales rep: `sales_rep_id = auth.uid()`
- Manager/admin: unrestricted (profile role must be `'manager'` or `'admin'`)

---

### `profiles`  — one row per provisioned rep

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | uuid | NO | = `rep_uuid(...)` — matches auth user UUID |
| `email` | text | NO | Pattern: `sim-{store_slug}-{abbrev}{N}@test.com` |
| `first_name` | text | YES | `"Sales"` |
| `last_name` | text | YES | `"Rep {i}"` |
| `role` | text | YES | `'sales_rep'` (default); use `'manager'` for cross-store observers |
| `store_id` | uuid | YES | = `store_uuid(dealership_id)` |
| `created_at` / `updated_at` | timestamptz | NO | auto |

**RLS INSERT policy:** `auth.uid() = id` — only service role can create profiles for others.
→ DealMaker upserts profiles using `_service_headers()`, `Prefer: resolution=merge-duplicates`.

---

### `reps`  — operational rep metadata

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | uuid | NO | = `rep_uuid(...)` |
| `store_id` | uuid | YES | = `store_uuid(dealership_id)` |
| `first_active_date` | date | YES | Set from `new_hire_dates` if provided |
| `active` | bool | NO | `true` default |

**RLS:** `id = auth.uid()` for ALL; managers read all.

---

### `deals`  — normalized deal records

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` — **NOT** the same as `deal_id` in events payload |
| `sales_rep_id` | uuid | NO | = `rep_uuid(...)` |
| `customer_name` | text | NO | |
| `deal_amount` | numeric | NO | Range: `deal_amount_min`–`deal_amount_max` |
| `gross_profit` | numeric | YES | Default 0 |
| `status` | text | YES | See §6 status values |
| `source` | text | YES | See §6 source values |
| `lead_source` / `lead_source_detail` | text | YES | |
| `close_date` | date | YES | |
| `appointment_date` | timestamptz | YES | |
| `appointment_showed` | bool | YES | Default false |

> **Note:** DealMaker currently writes deals as `events` (type `deal.created`,
> `deal.status_changed`), **not** directly into the `deals` table.
> If TopRep has an ingest worker that materializes events → `deals` rows,
> the `deal_id` in the event payload is the stable UUID to join on.
> See §5 for the payload format.

**RLS INSERT:** `with_check: true` — any authenticated user can insert.

---

### `activities`

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` |
| `deal_id` | uuid | NO | Matches `activity_id` in event payload |
| `sales_rep_id` | uuid | NO | = `rep_uuid(...)` |
| `activity_type` | text | NO | See §6 |
| `outcome` | text | YES | See §6 |
| `scheduled_at` / `completed_at` | timestamptz | YES | |
| `contact_quality_score` | numeric | YES | Default 0.50 |
| `response_time_minutes` | integer | YES | |
| `follow_up_sequence` | integer | YES | Default 1 |

**RLS:** open INSERT/UPDATE for any authenticated user.

---

### `leads`

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `store_id` | uuid | YES | = `store_uuid(dealership_id)` |
| `rep_id` | uuid | YES | = `rep_uuid(...)` |
| `source` | text | NO | See §6 |
| `created_at` | timestamptz | NO | |
| `first_response_at` … `sold_at` / `lost_at` | timestamptz | YES | Funnel timestamps |
| `status` | text | YES | Mirrors deal status flow |
| `call_count` / `text_count` / `email_count` / `total_touch_count` | integer | YES | |

---

### `quotas`

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `rep_id` | uuid | NO | = `rep_uuid(...)` |
| `period_start` / `period_end` | date | NO | Monthly; day 1 of month → last day |
| `quota_units` | integer | NO | From `rep_quota_updated` event payload |

DealMaker emits `rep_quota_updated` events. If TopRep materializes them to `quotas`,
read from `payload.new_quota` and `payload.month`.

---

### `source_stage_priors`

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `store_id` | uuid | YES | = `store_uuid(dealership_id)` |
| `source` | text | NO | See §6 |
| `stage` | text | NO | See §6 status values (used as stage) |
| `prior_alpha` | numeric | NO | Beta distribution α |
| `prior_beta` | numeric | NO | Beta distribution β |
| `baseline_mean` | numeric | NO | `alpha / (alpha + beta)` |
| `prior_strength` | numeric | NO | Default 40 |

DealMaker can seed these via `/stores/<id>/backfill` or `seed_source_stage_priors()`.

---

## 4. Allowed Event Types

```
deal.created
deal.status_changed
activity.scheduled
activity.completed
rep_quota_updated
```

---

## 5. Event Payload Shapes

Every event has the envelope:
```json
{
  "sales_rep_id": "<uuid>",
  "type": "<event_type>",
  "payload": { ... },
  "created_at": "2026-03-26T14:30:00.000Z"
}
```

### `deal.created`
```json
{
  "deal_id":       "<uuid5 stable>",
  "customer_name": "Customer Name",
  "deal_amount":   45000,
  "source":        "internet",
  "stage":         "lead",
  "gross_profit":  2800
}
```

### `deal.status_changed`
```json
{
  "deal_id":    "<uuid>",
  "old_status": "lead",
  "new_status": "qualified"
}
```

### `activity.scheduled`
```json
{
  "activity_id":    "<uuid5 stable>",
  "activity_type":  "call",
  "scheduled_for":  "2026-03-26T10:00:00.000Z"
}
```

### `activity.completed`
```json
{
  "activity_id":   "<uuid>",
  "activity_type": "call",
  "outcome":       "connected"
}
```

### `rep_quota_updated`
```json
{
  "month":     "2026-03",
  "old_quota": 0,
  "new_quota": 12
}
```

**`deal_id` / `activity_id` are stable across events** — same deal generates
both `deal.created` and later `deal.status_changed` with identical `deal_id`.
Formula: `stable_uuid("deal", dealership_id, YYYYMMDD, str(deal_number))`.

---

## 6. Enum / Vocabulary Values

### Deal status / stage progression
```
lead → qualified → proposal → negotiation → closed_won | closed_lost
```

### Deal / lead sources
```
internet  phone  walk_in  referral  third_party
```

### Activity types
```
call  email  meeting  demo  note
```

### Activity outcomes
```
connected  no_answer  left_vm  appt_set  showed  no_show
sold  lost  negotiating  follow_up
```

### Rep archetypes (internal — affects event generation rates only)
| Key | Close rate multiplier | Activity multiplier |
|---|---|---|
| `rockstar` | 2.5× | 1.2× |
| `solid_mid` | 1.0× | 1.0× |
| `underperformer` | 0.4× | 0.7× |
| `new_hire` | 0.3× (ramps up over tenure) | 0.8× |

---

## 7. Provisioned User Convention

When a store is created in DealMaker UI with `SUPABASE_SERVICE_ROLE_KEY` set:

| Item | Value |
|---|---|
| Email pattern | `sim-{store_slug}-{abbrev}{N}@test.com` |
| Password | `test123` |
| Auth UUID | Forced = `rep_uuid(dealership_id, "S-NNN")` (deterministic) |
| Profile role | `sales_rep` |
| Valid email domains (safe test) | `@test.com`, `@example.com`, `@dealmaker.dev`, `@toprep.dev` |

**Archetype abbreviations in email:**
- `rockstar` → `rock`
- `solid_mid` → `mid`
- `underperformer` → `under`
- `new_hire` → `new`

**Example store `dlr-001` with 2 rockstar + 2 solid_mid:**
```
sim-dlr-001-rock1@test.com   →  UUID 131b00c0-...  (S-001)
sim-dlr-001-rock2@test.com   →  UUID 2142c913-...  (S-002)
sim-dlr-001-mid1@test.com    →  UUID eb0002da-...  (S-003)
sim-dlr-001-mid2@test.com    →  UUID f45fc38f-...  (S-004)
```

---

## 8. RLS Summary: What Requires the Service Role Key

| Table | Operation | Requires service role? |
|---|---|---|
| `events` | INSERT (multi-rep) | **Yes** — policy: `sales_rep_id = auth.uid()` |
| `profiles` | INSERT for other users | **Yes** — policy: `auth.uid() = id` |
| `reps` | INSERT for other users | **Yes** — policy: `id = auth.uid()` |
| `deals` | INSERT | No — `with_check: true` |
| `activities` | INSERT | No — `with_check: true` |
| `leads` | INSERT | No (own or manager) |
| `source_stage_priors` | UPSERT | No — any authenticated user |

Use `SUPABASE_SERVICE_ROLE_KEY` as the `Authorization: Bearer` header and the
`apikey` header value for all DealMaker writes.  Never expose this key to the
browser.

---

## 9. Useful Diagnostic Queries

```sql
-- Count synthetic events
SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM events;

-- Events by rep and type
SELECT sales_rep_id, type, COUNT(*) FROM events
GROUP BY sales_rep_id, type ORDER BY sales_rep_id, type;

-- Provisioned reps
SELECT r.id, p.email, p.role, r.store_id, r.active
FROM reps r LEFT JOIN profiles p ON p.id = r.id
ORDER BY r.created_at;

-- Store → UUID lookup
-- For 'dlr-001': stable_uuid("store", "dlr-001") = c1d7cbaf-49c6-5977-953c-5eb806f3f85c
SELECT * FROM profiles WHERE store_id = 'c1d7cbaf-49c6-5977-953c-5eb806f3f85c';

-- Verify rep UUID matches events
SELECT DISTINCT sales_rep_id FROM events
WHERE sales_rep_id = '131b00c0-79f5-5a8b-a883-9cff9d116bb0'; -- dlr-001 / S-001

-- Delete all synthetic test data (safe reset)
DELETE FROM events WHERE sales_rep_id IN (
  SELECT id FROM profiles WHERE email LIKE 'sim-%@test.com'
);
DELETE FROM reps   WHERE id IN (SELECT id FROM profiles WHERE email LIKE 'sim-%@test.com');
DELETE FROM profiles WHERE email LIKE 'sim-%@test.com';
```

---

## 10. Adding a New Store — Checklist

For TopRep to correctly display data from a new DealMaker store:

1. **Create the store in DealMaker UI** (`/stores/new`) — fills `_stores` in-memory store, provisions auth users, upserts `profiles` + `reps` rows.
2. **Verify provisioning** — check `profiles` and `reps` tables for rows with `store_id = store_uuid(dealership_id)`.
3. **Run simulation** — start a store simulation (delivery: `api`) or run a backfill. Events will appear in the `events` table with `sales_rep_id` matching the provisioned UUIDs.
4. **TopRep: seed store priors** (optional) — call `seed_source_stage_priors` or trigger via DealMaker backfill to populate `source_stage_priors` for the store.
5. **Log in as a simulated rep** — use `sim-{store_slug}-{abbrev}N@test.com` / `test123`. They will see their own events. Use a `manager`-role profile to see all reps.

---

## 11. Changing the Schema — Rules for Both Agents

- **`events.sales_rep_id`** must remain `uuid NOT NULL`. DealMaker always sets it.
- **Never rename `events.type`** — DealMaker validates against `ALLOWED_EVENT_TYPES`.
- **Never rename `events.payload`** — DealMaker sends all data there.
- **`profiles.store_id` and `reps.store_id`** are `uuid` — DealMaker converts string IDs via `stable_uuid("store", ...)`. Do not change to `text`.
- **If you add a NOT NULL column** to `events`, `profiles`, or `reps`, coordinate with DealMaker first — its insert bodies are hardcoded.
- **Migrations** should be additive (new columns nullable or with defaults).
