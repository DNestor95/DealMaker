# Real-Time Data Ingest Reference (TOP REP)

Use this as the source-of-truth contract when building an external app that continuously sends sales activity into TOP REP.

## 1) Canonical event envelope

Every event sent to TOP REP must use this shape:

{
  "sales_rep_id": "uuid",
  "type": "event.type",
  "payload": {},
  "created_at": "2026-03-03T15:04:05.000Z"
}

Notes:
- sales_rep_id must be a valid UUID and should match the authenticated user for RLS-safe client writes.
- type must be one of the allowed event types below.
- payload must match the schema for that type.
- created_at is optional (server will fill it if omitted), but sending it is recommended for historical replay/backfill fidelity.

## 2) Allowed event types and payloads

### deal.created
payload:
{
  "deal_id": "uuid",
  "customer_name": "string",
  "deal_amount": 28500,
  "gross_profit": 3500,
  "source": "internet"
}

### deal.status_changed
payload:
{
  "deal_id": "uuid",
  "old_status": "lead|qualified|proposal|negotiation|closed_won|closed_lost",
  "new_status": "lead|qualified|proposal|negotiation|closed_won|closed_lost",
  "reason": "optional string"
}

### activity.scheduled
payload:
{
  "activity_id": "uuid",
  "deal_id": "optional uuid",
  "activity_type": "call|email|meeting|demo|note",
  "scheduled_for": "ISO timestamp"
}

### activity.completed
payload:
{
  "activity_id": "uuid",
  "deal_id": "optional uuid",
  "activity_type": "call|email|meeting|demo|note",
  "outcome": "connected|no_answer|left_vm|appt_set|showed|no_show|sold|lost|negotiating|follow_up"
}

### rep_quota_updated
payload:
{
  "month": "YYYY-MM",
  "old_quota": 50,
  "new_quota": 55,
  "reason": "optional string"
}

## 3) How to send data so the app reads it correctly

Preferred write path:
- POST /api/events

Why this path:
- Uses server-side validation in app/api/events/route.ts.
- Uses shared schema validation in lib/events/schemas.ts.
- Writes into events (append-only), which triggers live stats updates in rep_month_stats via events_to_stats_trigger().

Direct table writes to events can work for admin scripts, but app/API writes are safer and keep behavior consistent.

## 4) Minimal real-time sender example

Use this in your generator app loop:

const event = {
  sales_rep_id: "11111111-1111-1111-1111-111111111111",
  type: "activity.completed",
  payload: {
    activity_id: "22222222-2222-2222-2222-222222222222",
    deal_id: "33333333-3333-3333-3333-333333333333",
    activity_type: "call",
    outcome: "connected"
  },
  created_at: new Date().toISOString()
}

await fetch("https://<your-domain>/api/events", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer <supabase-user-jwt>"
  },
  body: JSON.stringify(event)
})

## 5) Ordering, IDs, and reliability

To keep downstream stats accurate and debuggable:
- Always generate stable UUIDs for deal_id and activity_id.
- Send deal.created before deal.status_changed for the same deal when possible.
- Keep timestamps in UTC ISO-8601.
- Retry transient failures with exponential backoff.
- Do not update/delete events; events are append-only by design.

Important:
- The current events table does not enforce idempotency keys.
- If your sender can retry the same event, keep your own dedupe key in the generator and avoid duplicate inserts.

## 6) Fast validation checklist

Before going live, verify:
- sales_rep_id exists in profiles.id.
- type is valid and payload fields strictly match schema.
- API returns success: true.
- New rows appear in events.
- rep_month_stats changes as expected for the event outcome.

Helpful scripts for auditing and verification are in scripts/check-audit.

## 7) Source-of-truth files in this repo

- app/api/events/route.ts
- lib/events/schemas.ts
- lib/events/log.ts
- supabase/schema.sql
- supabase/forecast_schema.sql
- Events.md
