# DealMaker v2 — Learning Guide

> **Purpose:** This document is a teaching reference for understanding how DealMaker v2 is
> built, why each design decision was made, and what you need to learn to extend it.
> It is intended to evolve alongside the codebase.

---

## Table of Contents

1. [What This Program Does (and Why)](#1-what-this-program-does-and-why)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Python Concepts Used in This Codebase](#3-python-concepts-used-in-this-codebase)
4. [Flask — The Web Framework](#4-flask--the-web-framework)
5. [The Generator Deep Dive](#5-the-generator-deep-dive)
6. [Talking to Supabase Without Extra Libraries](#6-talking-to-supabase-without-extra-libraries)
7. [Background Threads in a Web Server](#7-background-threads-in-a-web-server)
8. [The Jinja2 Template System](#8-the-jinja2-template-system)
9. [Environment Variables and Secrets](#9-environment-variables-and-secrets)
10. [The Data Model — What Gets Stored Where](#10-the-data-model--what-gets-stored-where)
11. [The Event-Driven Pattern](#11-the-event-driven-pattern)
12. [UUID Determinism — Why the Generator Uses uuid5](#12-uuid-determinism--why-the-generator-uses-uuid5)
13. [How the Frontend Talks to the Backend (Fetch API)](#13-how-the-frontend-talks-to-the-backend-fetch-api)
14. [Planned Features — How They Fit the Architecture](#14-planned-features--how-they-fit-the-architecture)
15. [Glossary of Domain Terms](#15-glossary-of-domain-terms)
16. [Recommended Study Path](#16-recommended-study-path)

---

## 1. What This Program Does (and Why)

### The Problem It Solves

**TopRep** is a sales-intelligence platform for car dealerships. Its core engine runs
Bayesian statistics to predict whether a sales rep will hit their monthly quota. To
develop, test, and train that engine, you need realistic sales data — deals flowing in,
activities being logged, quotas changing. But real dealership data is private, messy,
and time-gated (you can't rewind to last January).

**DealMaker** is a *synthetic data generator* — it pretends to be a busy car dealership,
creates fictional deals and sales activity events, and pushes them into the TopRep
database. This lets you:

- Test the Bayesian engine with controlled, known inputs
- Simulate edge cases (a rep having a terrible month, a BDC team going down)
- Create realistic QA users you can actually log in as
- Backfill months of "history" in seconds instead of waiting months

### v1 vs v2

| | v1 | v2 |
|---|---|---|
| Interface | Tkinter desktop GUI | Flask web app |
| Deployment | Run locally, one user | Server-backed, browser-based |
| Store management | Single store at a time | Multiple stores with per-store config |
| Generator | Lives in `dealmaker_generator.py` | **Same file** — reused as a library |

The most important design decision in v2 is that the generator was **not rewritten**.
The Flask app imports it as a module. This is a principle called *separation of concerns*:
the logic for generating data and the logic for displaying a web UI are kept independent.

---

## 2. High-Level Architecture

```
Browser (you)
    │
    │  HTTP  (GET /stores, POST /stores/new, etc.)
    ▼
Flask Web Server  ←── run.py starts this on localhost:5050
    │
    ├── app/__init__.py          create_app() wires everything together
    │
    ├── app/routes/stores.py     Store CRUD — creates/lists stores
    ├── app/routes/simulation.py Start/stop background simulation threads
    ├── app/routes/settings.py   .env credential management
    │
    ├── app/supabase_client.py   HTTP calls to TopRep / Supabase REST API
    │
    └── dealmaker_generator.py   Core simulation logic (imported, not web-aware)
          │
          └── generates Event objects → pushed to API or written to .jsonl files


TopRep Supabase Database  (remote, cloud)
    ├── auth.users           Real login accounts
    ├── profiles             Rep metadata + store_id
    ├── events               The main write target for simulated data
    ├── deals                Updated by Supabase triggers from events
    ├── activities           Updated by Supabase triggers from events
    ├── rep_month_stats      Auto-calculated; read by forecast engine
    └── source_stage_priors  Bayesian prior seeds
```

**Data flow for one simulation tick:**

```
_StoreThread.run()
  → generate_events()           (pure Python, no network)
       → generate_deal_workflow() × N deals
            → returns list of Event objects
  → post_event() per event      (HTTP POST to Supabase)
       → supabase_client.post_event()
            → urllib.request.urlopen()
  → sleep(every_seconds)
  → repeat
```

---

## 3. Python Concepts Used in This Codebase

### 3.1 `from __future__ import annotations`

You'll see this at the top of almost every file. In Python 3.10 and below, you can't write:

```python
def foo() -> list[str] | None:
```

...because `list[str]` and `X | Y` union syntax weren't valid at runtime. The
`from __future__ import annotations` import makes Python treat all type hints as strings
(evaluated lazily), so you get modern syntax on Python 3.9+.

**Rule of thumb:** Always put it at the top of any file that uses type hints.

---

### 3.2 `@dataclass`

```python
from dataclasses import dataclass, field

@dataclass
class TeamMember:
    member_id: str
    role: str
    name: str
```

`@dataclass` is a decorator that auto-generates `__init__`, `__repr__`, and `__eq__`
for a class based on its annotated fields. Without it, you'd write:

```python
class TeamMember:
    def __init__(self, member_id: str, role: str, name: str):
        self.member_id = member_id
        self.role = role
        self.name = name
```

The `field(default_factory=dict)` pattern is used when the default value is mutable
(like a list or dict). If you wrote `payload: dict = {}` directly, **all instances would
share the same dict object** — a classic Python gotcha.

---

### 3.3 Type Hints

```python
def generate_events(
    start_date: datetime,
    days: int,
    daily_leads: int,
    team: list[TeamMember],
    dealership_id: str,
    seed: int,
    sales_rep_id_override: str | None = None,
    sales_rep_ids: list[str] | None = None,
) -> list[Event]:
```

Type hints are not enforced at runtime — Python doesn't check them. Their value is:
- **Readability:** you know what each parameter expects without reading the body
- **Editor support:** VS Code uses them for autocomplete and error highlighting
- **Documentation:** they serve as a form of self-documenting code

`str | None` means "either a string or None" — this is the modern Python 3.10+ union
syntax (enabled on older versions via `from __future__ import annotations`).

---

### 3.4 The `random.Random` Class (Seeded Randomness)

```python
rng = random.Random(seed)
rng.randint(12000, 68000)
rng.choice(DEAL_SOURCES)
rng.gauss(daily_leads, daily_leads * 0.25)
```

`random.Random(seed)` creates an **isolated random number generator** with a fixed
starting point. Given the same seed, it will always produce the exact same sequence
of numbers. This is crucial for:

- **Reproducibility:** run the generator twice with `--seed 42` → identical output
- **Debugging:** if a strange edge case appears, you can reproduce it exactly
- **Isolation:** the generator's randomness doesn't affect other parts of the program
  that use Python's global `random` module

`rng.gauss(mean, stddev)` generates Gaussian (bell-curve) distributed numbers.
`daily_leads * 0.25` as the standard deviation means the daily volume varies by ±25%,
which models natural variation in dealership traffic.

---

### 3.5 `pathlib.Path`

```python
from pathlib import Path
output_dir = Path("output/stores")
output_dir.mkdir(parents=True, exist_ok=True)
output_file = output_dir / f"{store_id}.jsonl"
```

`Path` objects are object-oriented wrappers around file system paths. The `/` operator
joins path segments (like `os.path.join` but cleaner). `exist_ok=True` prevents an
error if the directory already exists.

---

### 3.6 Generators vs Lists

You'll see both `list[Event]` and patterns that build lists incrementally with
`events.extend(...)`. The codebase uses plain lists rather than Python generator
functions because all events need to be in memory for sorting by timestamp at the end.

---

## 4. Flask — The Web Framework

### 4.1 What Flask Is

Flask is a **micro web framework** — it handles HTTP request/response routing but
deliberately leaves everything else (database, auth, templates beyond basics) up to
you. This contrasts with Django, which is "batteries included" and opinionated about
everything.

Flask receives an HTTP request, figures out which Python function should handle it
(routing), calls that function, and returns whatever the function returns as an HTTP
response.

### 4.2 The Application Factory Pattern

```python
# app/__init__.py
def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.register_blueprint(stores_bp)
    app.register_blueprint(simulation_bp)
    app.register_blueprint(settings_bp)
    return app
```

Instead of creating the Flask app at module level (`app = Flask(__name__)`), the app is
created inside a function called `create_app()`. This pattern has major benefits:

- **Testability:** you can call `create_app()` in tests with different config
- **Multiple instances:** possible to run two apps with different settings in one process
- **Avoids circular imports:** blueprints can be registered after all modules are loaded

`__name__` is a Python built-in — in `app/__init__.py`, it evaluates to `"app"`, which
Flask uses to find the `templates/` and `static/` directories.

---

### 4.3 Blueprints

```python
# app/routes/stores.py
bp = Blueprint("stores", __name__)

@bp.route("/")
def index():
    return render_template("stores/list.html", stores=list(_stores.values()))
```

A **Blueprint** is a collection of routes and handlers that can be registered onto an
app. Think of it as a "mini-app" that gets attached to the main app. Benefits:

- Routes can be organized into logical groups (stores, simulation, settings)
- The same blueprint could theoretically be mounted at different URL prefixes
- Each blueprint file is independently understandable

The string `"stores"` is the blueprint's name — it's used as a namespace when
generating URLs with `url_for("stores.index")`.

---

### 4.4 Route Decorators and HTTP Methods

```python
@bp.route("/stores/new", methods=["GET"])
def new_store():
    return render_template("stores/new.html", ...)

@bp.route("/stores/new", methods=["POST"])
def create_store():
    data = request.form
    ...
    return redirect(url_for("stores.store_detail", store_id=store_id))
```

The same URL `/stores/new` is handled by **two different functions** depending on the
HTTP method:
- `GET` → show the form (read-only, no side effects)
- `POST` → process the submitted form (creates data, has side effects)

This is the standard web convention (HTML forms use GET and POST). `request.form` is
Flask's dict-like object containing submitted form field values.

---

### 4.5 `url_for()` — Never Hard-Code URLs

```python
return redirect(url_for("stores.store_detail", store_id=store_id))
```

`url_for()` generates a URL from a function name. **Never** write `redirect("/stores/abc")` —
if you rename a route or add a URL prefix, `url_for()` updates automatically everywhere.
The format is `"blueprint_name.function_name"`.

---

### 4.6 JSON Responses

```python
return jsonify({"status": "started"})
```

`jsonify()` creates an HTTP response with `Content-Type: application/json`. The
simulation routes return JSON because they're called by JavaScript (`fetch()`), not by
the browser navigating to a URL.

---

### 4.7 The Request/Response Cycle

Every web interaction follows this exact sequence:

```
1. Browser sends HTTP request  → GET /stores/mystore
2. Flask matches the URL       → routes.stores.store_detail(store_id="mystore")
3. Python function runs        → looks up store, fetches profiles
4. Function returns response   → render_template("stores/detail.html", store=store)
5. Browser receives HTML       → displays the page
```

For simulation start/stop, step 4 returns JSON, and step 5 is JavaScript updating the
page without a full reload.

---

## 5. The Generator Deep Dive

### 5.1 What `generate_events()` Does

This is the core of the whole program. Here's the algorithm:

```
generate_events(start_date, days, daily_leads, team, dealership_id, seed)
│
├── Create isolated RNG with seed
├── Initialize deal_counter = 1
│
└── For each day in range(days):
    │   leads_today = Gaussian random ≈ daily_leads (±25% variance)
    │
    └── For each lead today:
        │   Pick which rep to assign (round-robin from rep pool)
        │
        └── generate_deal_workflow(day, deal_number, team, ...)
            │
            ├── Create deal.created event
            ├── Walk the pipeline: lead → qualified → proposal → negotiation
            │     (each advance has 88% chance of succeeding)
            ├── Generate 2–6 activity events (scheduled + completed pairs)
            ├── Close the deal: 36% chance closed_won, 64% closed_lost
            └── 6% chance: emit a rep_quota_updated event
```

### 5.2 The Pipeline Advance Logic

```python
status_path = ["qualified", "proposal", "negotiation"]
for next_status in status_path:
    if rng.random() < 0.88:
        # emit deal.status_changed event
        current_status = next_status
```

This is a **Markov chain** — at each step, there's a fixed probability of advancing.
A deal that reaches "negotiation" will have passed three 88% checks, so it had:
`0.88 × 0.88 × 0.88 = 68%` probability of getting to negotiation at all.

The 36% close rate applies to the final `closed_won` vs `closed_lost` decision — but
realistic close rate from initial lead is much lower: `0.68 × 0.36 ≈ 24%`.

### 5.3 Why Events Are Sorted at the End

```python
events.sort(key=lambda event: event.created_at)
```

Within a single day, multiple deals and activities are created with random timestamps.
Because each deal's events are generated sequentially but multiple deals are interleaved,
the events list would be out of chronological order without the final sort. The sort
ensures that when events are sent to the API or written to JSONL, they arrive in the
correct time sequence.

### 5.4 The Rep Assignment Pool

```python
rep_pool: list[str] | None = None
if sales_rep_ids:
    rep_pool = sales_rep_ids
elif sales_rep_id_override:
    rep_pool = [sales_rep_id_override]

# In the loop:
assigned = rep_pool[(deal_counter - 1) % len(rep_pool)] if rep_pool else None
```

**Round-robin** distribution: deal 1 goes to rep[0], deal 2 to rep[1], ..., deal N+1
wraps back to rep[0]. The modulo operator `%` handles the wrap-around.

Three modes:
1. `sales_rep_ids` provided → use that exact list (real UUIDs from TopRep)
2. `sales_rep_id_override` provided → always use this one UUID
3. Neither → generate UUIDs deterministically from `stable_uuid()`

### 5.5 Deterministic UUIDs (`stable_uuid`)

```python
def stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))
```

`uuid5` generates a **deterministic UUID** from a namespace + name string.
`uuid5("sales_rep", "store-A", "S-001")` will always produce the same UUID.

This is critical: if you run the generator on Monday and again on Tuesday with the
same seed, the same rep gets the same UUID, so TopRep can accumulate stats for that
"rep" across multiple runs. Random UUIDs would create a new phantom rep every run.

---

## 6. Talking to Supabase Without Extra Libraries

### 6.1 Why `urllib` Instead of `requests`

The `requests` library is the go-to for HTTP in Python, but it's a third-party package.
`urllib` is part of Python's standard library — it requires nothing to install.

The project deliberately uses `urllib` to minimize dependencies. The `requirements.txt`
only has two entries: `flask` and `python-dotenv`. Every other capability uses the
standard library.

### 6.2 How a Manual HTTP Request Works

```python
import json
from urllib import request, error

payload = json.dumps({"key": "value"}).encode("utf-8")   # → bytes

req = request.Request(
    url,
    data=payload,          # body (POST if present, GET if None)
    headers=headers,
    method="POST"
)

try:
    with request.urlopen(req, timeout=10) as response:
        body = response.read().decode("utf-8")  # bytes → string
        data = json.loads(body)                  # string → dict
except error.HTTPError as exc:
    # 4xx / 5xx response
    detail = exc.read().decode("utf-8")
except error.URLError as exc:
    # Network failure — DNS, timeout, refused connection
    pass
```

The key steps:
1. **Encode** the dict to JSON bytes (`json.dumps` → `encode`)
2. **Wrap** the request in `urllib.request.Request` with headers and method
3. **Send** with `urlopen` and use it as a context manager (`with`)
4. **Decode** the response bytes back to a string, then parse JSON
5. **Handle errors** separately: `HTTPError` = server responded with error code;
   `URLError` = couldn't reach the server at all

### 6.3 The Supabase REST API Pattern

Supabase exposes every database table as a REST endpoint:

```
GET  https://<project>.supabase.co/rest/v1/profiles?role=eq.sales_rep
POST https://<project>.supabase.co/rest/v1/events
```

Every request needs two headers:
- `Authorization: Bearer <JWT token>` — who you are
- `apikey: <anon key>` — which Supabase project you're accessing

The `Prefer: return=minimal` header on POST tells Supabase not to return the inserted
row in the response body — cheaper for high-volume inserts.

### 6.4 Supabase Edge Functions

```python
if "/functions/v1/" in api_url:
    post_actions_batch_to_edge(...)
```

Supabase also has serverless **Edge Functions** (similar to AWS Lambda) that run custom
TypeScript code. The generator can post to these instead of the raw REST API. Edge
Functions add business logic (validation, transformation) before writing to the database.
The URL pattern distinguishes them: it contains `/functions/v1/`.

---

## 7. Background Threads in a Web Server

### 7.1 The Problem

Flask handles one HTTP request at a time per worker. A simulation that runs indefinitely
(sleeping, generating events, posting to API, sleeping again) would block the entire web
server — no other page would load while a simulation was running.

### 7.2 Python Threading

```python
import threading

class _StoreThread(threading.Thread):
    def __init__(self, store: dict) -> None:
        super().__init__(daemon=True)   # ← important
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            # ... do work ...
            if self._stop_event.wait(every_seconds):
                break   # stop_event was set during the sleep

thread = _StoreThread(store)
thread.start()   # calls thread.run() in a new OS thread
```

Key concepts:

**`daemon=True`:** When the main process exits, daemon threads are killed automatically.
Without this, the Python process would hang waiting for all threads to finish.

**`threading.Event`:** A flag that threads can check. `event.set()` raises the flag;
`event.is_set()` checks it; `event.wait(timeout)` blocks until the flag is set OR the
timeout expires — it returns `True` if the flag was set, `False` if it timed out. This
is how the simulation "sleeps for N seconds but wakes up immediately if asked to stop."

**`threading.Thread` vs `threading.Event` vs `threading.Lock`:**
- `Thread` — runs code in parallel
- `Event` — signals between threads (used here for stop)
- `Lock` — prevents two threads from modifying shared data simultaneously (will be
  needed when `_stores` dict is accessed from both Flask request threads and
  simulation threads)

### 7.3 The GIL (Global Interpreter Lock)

Python has a **Global Interpreter Lock** that prevents true parallel execution of
Python bytecode. In practice, for this application, that's fine: the simulation threads
spend most of their time waiting for HTTP responses (I/O-bound work), during which the
GIL is released and other threads can run. CPU-bound work would be a different story.

### 7.4 Thread Registry

```python
_runners: dict[str, "_StoreThread"] = {}
```

This module-level dict is the "thread registry" — it maps `store_id → _StoreThread`.
When the browser asks "is store X running?", the status route checks if
`_runners["X"].is_alive()`. When the browser says "stop store X", the stop route calls
`_runners["X"].stop()`.

---

## 8. The Jinja2 Template System

Flask uses **Jinja2** as its template engine. HTML files in `templates/` contain
special syntax that Flask replaces with real values before sending the response.

### 8.1 Template Inheritance

```html
<!-- base.html -->
<html>
  <body>
    <nav>...</nav>
    <main class="container">
      {% block content %}{% endblock %}  ← child fills this in
    </main>
  </body>
</html>
```

```html
<!-- stores/detail.html -->
{% extends "base.html" %}
{% block content %}
  <h1>{{ store.dealership_id }}</h1>  ← double braces = output a value
{% endblock %}
```

`{% extends %}` tells Jinja2 this template builds on `base.html`. Only the `{% block %}`
sections are replaced. This means the navbar, CSS link, and JS script tag are defined
once in `base.html` and shared across every page.

### 8.2 Template Variables and Filters

```html
{{ store.status }}                          ← output store["status"]
{{ store.close_rate_pct }}%                 ← output number, append %
{{ src | replace('_',' ') | title }}        ← filters: replace then titlecase
{% if profiles %}...{% else %}...{% endif %} ← conditional
{% for p in profiles %}...{% endfor %}       ← loop
```

Filters (`|`) transform values. They chain left-to-right. `"walk_in" | replace('_',' ')
| title` → `"Walk In"`.

### 8.3 `render_template()` and Context Variables

```python
return render_template(
    "stores/detail.html",
    store=store,           # ← store dict is available as {{ store }} in template
    profiles=store_profiles
)
```

Flask serializes the keyword arguments and injects them into the Jinja2 context.
The template can access any key of the `store` dict using dot notation (`store.status`)
or bracket notation (`store['status']`).

---

## 9. Environment Variables and Secrets

### 9.1 Why `.env` Files

Hard-coding credentials in source code is a **critical security mistake** — anyone who
can read the code (or the git history) gets the credentials. Environment variables
keep secrets out of code entirely.

```
# .env file (never committed to git)
TOPREP_API_URL="https://abc.supabase.co/functions/v1/ingest"
TOPREP_AUTH_TOKEN="eyJhbGci..."
SUPABASE_ANON_KEY="sb_publishable_..."
```

### 9.2 How `.env` Is Loaded

Both `dealmaker_generator.py` and `app/supabase_client.py` have their own `load_env()`
functions. They read the `.env` file and call `os.environ[key] = value` for each entry,
but only if the key isn't already set:

```python
if key not in os.environ:
    os.environ[key] = value
```

This means **real environment variables always win** over `.env` values. This is
important for production deployments where secrets are injected via the platform
(not files).

### 9.3 Secret Masking in the UI

```python
def _mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]
```

The settings page never displays the raw token — it shows `eyJh****k9mP`. This prevents
the token from being visible to someone watching over your shoulder or in a screenshot.

### 9.4 The Service Role Key — Special Danger

The `SUPABASE_SERVICE_ROLE_KEY` bypasses all Row Level Security (RLS) policies in the
database. Whoever has it can read and write any row in any table. Rules:

- Read it **only** in server-side code that never returns its value to the browser
- Never log it
- Never include it in a JSON response
- Never put it in a JavaScript variable

---

## 10. The Data Model — What Gets Stored Where

### 10.1 In-Memory (DealMaker's own state)

```python
_stores: dict[str, dict] = {}    # in stores.py
_runners: dict[str, _StoreThread] = {}  # in simulation.py
```

These dicts live in Python process memory. They are reset every time the Flask server
restarts. This is the current "persistence layer" — deliberately simple for now, but
it is the first thing to replace with a real database.

### 10.2 Files (JSONL output)

```
output/stores/riverdale-ford.jsonl
```

When delivery mode is `"file"` or `"both"`, events are appended to a `.jsonl` file.
**JSONL** (JSON Lines) means one JSON object per line. This format is easy to stream,
easy to append to, and easy to replay from.

### 10.3 TopRep's Supabase Database (remote)

The tables DealMaker writes to:

| Table | How DealMaker writes to it |
|---|---|
| `events` | Direct REST POST or Edge Function call |
| `deals` | Created by Supabase trigger from `events` |
| `activities` | Created by Supabase trigger from `events` |
| `rep_month_stats` | Updated by `events_to_stats_trigger()` in PostgreSQL |
| `profiles` | Will be upserted during rep provisioning (Phase 4) |
| `reps` | Will be upserted during rep provisioning (Phase 4) |
| `source_stage_priors` | Will be written by prior seeding feature (Phase 2) |

### 10.4 The Event Envelope

Every event written to TopRep follows this shape:

```json
{
  "sales_rep_id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "deal.created",
  "payload": {
    "deal_id": "a1b2c3d4-...",
    "customer_name": "Customer 00042",
    "deal_amount": 34500,
    "gross_profit": 2100,
    "source": "internet"
  },
  "created_at": "2026-03-15T14:23:45.000Z"
}
```

The `payload` field is a free-form JSON object — its schema depends on the `type`. This
is the **envelope pattern**: the outer structure is always the same (who, what type,
when), but the inner `payload` varies by event type. This is common in event-driven
systems and message queues.

---

## 11. The Event-Driven Pattern

DealMaker emits discrete events describing things that happened:
> "Rep X created a deal", "Deal Y moved to proposal", "Activity Z was completed"

TopRep ingests these events and derives state from them. This is called **event sourcing**
— the events are the source of truth, not a table that gets updated in place.

**Advantages:**
- You can replay events to rebuild state from scratch
- You get a complete audit trail of every change and its timestamp
- Multiple downstream tables (`deals`, `activities`, `rep_month_stats`) can be updated
  from the same event without the sender needing to know about all of them

**In practice in TopRep:** when DealMaker posts a `deal.status_changed` event with
`new_status: "closed_won"`, a Supabase database trigger (`events_to_stats_trigger`)
automatically increments `rep_month_stats.sold_units` for that rep/month. DealMaker
never writes to `rep_month_stats` directly.

---

## 12. UUID Determinism — Why the Generator Uses `uuid5`

There are several UUID versions. The two most common in application code:

| Version | How Generated | Deterministic? |
|---|---|---|
| `uuid4` | Pure random | No — different every call |
| `uuid5` | Hash of (namespace + name) | Yes — same input → same UUID |

The generator uses `uuid5` for deal IDs, activity IDs, and rep IDs:

```python
def stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))

deal_id = stable_uuid("deal", dealership_id, "20260315", "42")
# → always the same UUID for this store, date, and deal number
```

This matters because the generator runs in **batches**. If you push 3 days of events,
then push the same 3 days again (maybe to test something), `uuid5` means the second
run produces identical deal IDs — so Supabase's `ON CONFLICT DO NOTHING` prevents
duplicate rows instead of creating phantom duplicates.

---

## 13. How the Frontend Talks to the Backend (Fetch API)

### 13.1 The Full Flow

When you click "Start" on the store detail page:

```
User clicks button
  → onclick="simAction('riverside-ford', 'start')"
  → JavaScript function runs in browser
  → fetch('/simulation/riverside-ford/start', { method: 'POST' })
  → HTTP POST request sent to Flask
  → Flask route: @bp.route("/<store_id>/start", methods=["POST"])
  → Python: creates _StoreThread, starts it, updates store["status"]
  → Python: return jsonify({"status": "started"})
  → HTTP 200 response with JSON body
  → JavaScript receives response: data = { status: "started" }
  → setTimeout(() => window.location.reload(), 1200)
  → Page reloads, showing updated status
```

### 13.2 The `async/await` Pattern

```javascript
async function simAction(storeId, action) {
  try {
    const resp = await fetch(`/simulation/${storeId}/${action}`, { method: 'POST' });
    const data = await resp.json();
    // ...
  } catch (err) {
    console.error('simAction error:', err);
  }
}
```

`fetch()` is asynchronous — it returns a Promise immediately and resolves later when
the HTTP response arrives. `await` pauses the function until the Promise resolves.
`async` marks the function as one that contains `await` calls. Without `async/await`,
you'd write nested `.then()` callbacks — harder to read.

### 13.3 Live Status Polling

```javascript
setInterval(() => {
  fetch('/simulation/{{ store.dealership_id }}/status')
    .then(r => r.json())
    .then(d => {
      document.getElementById('events-count').textContent = d.events_sent;
    });
}, 3000);
```

Every 3 seconds, the page silently asks Flask for the latest status and updates the
"Events sent" counter. This is **polling** — simpler than WebSockets but less efficient
for high-frequency updates. For this use case (update every 3 seconds), it's perfectly
adequate.

---

## 14. Planned Features — How They Fit the Architecture

### 14.1 Rep Archetypes (Phase 1)

**What changes:** `TeamMember` gets an `archetype` field. `generate_deal_workflow`
multiplies the base close rate (0.36) and activity count by archetype-specific factors.

**Pattern used:** a `ARCHETYPES: dict[str, ArchetypeConfig]` registry — a dict mapping
archetype name to a config object with multipliers. Looking up `ARCHETYPES["rockstar"]`
gives you the config; applying it is just multiplication.

**Nothing in the Flask layer changes** — this is purely a generator change.

---

### 14.2 Month-Shape Realism (Phase 1)

**What changes:** The `generate_events` loop adds a `daily_weight()` helper. Instead of
`rng.gauss(daily_leads, ...)` for all days equally, the target leads on day 28 of the
month is higher than day 2.

**Pattern used:** weight tables and normalization. Multiply each day's target by a
weight, then normalize so the total reaches the configured monthly target.

---

### 14.3 Scenarios (Phase 1)

**What changes:** A `ScenarioConfig` dataclass and `SCENARIO_REGISTRY` dict. Before
generating, `apply_scenarios()` merges the scenario overrides into the base store
params. The rest of the generator uses the merged params.

**Pattern used:** the **strategy** or **overlay** pattern — base config + overrides =
effective config. Scenarios are additive overlays, not replacements.

---

### 14.4 Rep User Provisioning (Phase 4)

**What changes:** New functions in `supabase_client.py` that call Supabase's
**Admin Auth API** (different endpoint, requires `service_role` key).

```
POST https://<project>.supabase.co/auth/v1/admin/users
Authorization: Bearer <SERVICE_ROLE_KEY>
Body: { "email": "sim-..@dealmaker.test", "password": "...", "email_confirm": true }
```

Then upsert `profiles` and `reps` rows with the new user's UUID.

**A new route** in `stores.py` handles the button click and returns the credential
sheet as a template render. The credential download is a second route returning
`Content-Disposition: attachment` CSV.

**Security invariant:** `SUPABASE_SERVICE_ROLE_KEY` is read in the Flask route handler
but **never** included in the template context or JSON response.

---

### 14.5 Store Persistence (Phase 3)

**What changes:** `_stores` dict gets backed by a JSON file (simplest) or a Supabase
config table. The dict stays as the runtime cache — reads check the dict first, writes
go to dict + persistent store simultaneously.

**Why keep the dict?** Database calls have latency. Keeping an in-memory cache means
the simulation thread reads store config without blocking on I/O.

---

## 15. Glossary of Domain Terms

| Term | Meaning |
|---|---|
| **Lead** | A potential customer who has made contact with the dealership |
| **Deal** | A specific buying opportunity — one customer, one vehicle |
| **Pipeline stage** | Where a deal is in the sales process (lead → qualified → proposal → negotiation → closed) |
| **Closed Won** | The deal was sold — customer bought the car |
| **Closed Lost** | The deal fell apart — customer walked, bad credit, price disagreement, etc. |
| **Close rate** | % of deals that end as closed_won vs closed_lost |
| **BDC** | Business Development Center — the call/email team that handles inbound internet leads and appointment setting |
| **Activity** | A touchpoint — call, email, meeting, demo, or note |
| **Outcome** | Result of an activity — connected, no_answer, appt_set, showed, sold, etc. |
| **Quota** | Monthly target for number of units (cars) sold |
| **Gross profit** | Revenue minus cost on a single deal — what the dealership actually keeps |
| **Prior** | In Bayesian statistics, your belief about a probability before seeing new evidence |
| **Posterior** | Your updated belief after incorporating new evidence |
| **Source stage prior** | TopRep's per-store, per-lead-source, per-stage Bayesian prior — seeds the forecast engine |
| **Rep month stats** | Aggregated counters for one rep for one calendar month |
| **JSONL** | JSON Lines — one JSON object per line; newline-delimited |
| **Seed** | A starting value for a random number generator that makes it reproducible |
| **Round-robin** | Distributing items in a fixed rotating cycle |
| **Edge Function** | A serverless function deployed to Supabase's infrastructure (TypeScript) |
| **RLS** | Row Level Security — Postgres feature where SQL policies control which rows each user can see/write |
| **JWT** | JSON Web Token — a signed token used to authenticate API requests |
| **Archetype** | A rep performance profile (Rockstar, Solid Mid, Underperformer, New Hire) |

---

## 16. Recommended Study Path

Work through these in order. Each topic builds on the previous ones.

### Week 1 — Python foundations relevant to this codebase
1. `@dataclass` and when to use it vs plain classes
2. Type hints — `str | None`, `list[dict]`, `tuple[bool, str]`
3. `pathlib.Path` — replacing old-style `os.path` usage
4. `random.Random` and reproducible randomness
5. Reading/writing files with `open()`, `json.dumps/loads`, encoding

### Week 2 — HTTP and APIs
6. How HTTP works: methods (GET/POST), status codes, headers, body
7. `urllib.request` — build and send a request manually, handle errors
8. JSON as the universal API data format
9. Authentication headers: `Bearer` tokens, `apikey`
10. What a REST API is; CRUD operations mapped to HTTP methods

### Week 3 — Flask
11. Flask quickstart: routes, `render_template`, `request.form`
12. The application factory pattern (`create_app`)
13. Blueprints and URL namespacing
14. `url_for` — why you never hard-code URLs
15. `jsonify` — returning JSON from Flask for JavaScript callers

### Week 4 — Frontend basics
16. Jinja2 template syntax: `{{ }}`, `{% %}`, filters, inheritance
17. `fetch()` and `async/await` in JavaScript
18. `setInterval` — polling for live updates
19. How HTML forms submit data (GET vs POST, `request.form`)

### Week 5 — Concurrency and persistence
20. `threading.Thread` — running code in parallel
21. `threading.Event` — signaling between threads
22. The GIL — when Python threading helps (I/O) vs doesn't (CPU)
23. Why in-memory state resets on server restart
24. Options for persistence: JSON files, SQLite, cloud databases

### Week 6 — Supabase and the database layer
25. Supabase architecture: auth, REST API, Edge Functions, Storage
26. Row Level Security (RLS) — how policies protect your data
27. Database triggers — how `events_to_stats_trigger` works
28. JWT tokens — what they are, how they're decoded, what `sub` means
29. The Admin Auth API and why it needs the service role key

---

*This document should be updated as features are built. Each section corresponds to
real code in this repository — read the source file alongside the relevant section.*
