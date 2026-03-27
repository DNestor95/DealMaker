from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tkinter import END, StringVar, Tk, Toplevel
from tkinter import messagebox
from tkinter import ttk

from dealmaker_generator import (
    Event,
    build_team,
    extract_user_id_from_jwt,
    fetch_profiles_from_supabase,
    generate_events,
    load_env_file,
    normalize_delivery_url,
    send_events_to_api,
    validate_api_settings,
)
from app.supabase_client import (
    _anon_key as _supabase_anon_key,
    _api_url as _supabase_api_url,
    _TOPREP_SUPABASE_URL,
    check_connection,
    provision_store_reps,
)


STORE_TEMPLATES: dict[str, dict] = {
    "custom": {
        "label": "Custom (blank)",
        "salespeople": 8,
        "managers": 2,
        "bdc_agents": 3,
        "daily_leads": 20,
        "close_rate_pct": 36,
        "month_shape": "flat",
        "archetype_dist": {"rockstar": 1, "solid_mid": 5, "underperformer": 1, "new_hire": 1},
    },
    "high_volume_internet": {
        "label": "High-Volume Internet Store",
        "salespeople": 12,
        "managers": 3,
        "bdc_agents": 5,
        "daily_leads": 40,
        "close_rate_pct": 30,
        "month_shape": "realistic",
        "archetype_dist": {"rockstar": 2, "solid_mid": 7, "underperformer": 2, "new_hire": 1},
    },
    "rural_walkin": {
        "label": "Rural Walk-In Store",
        "salespeople": 4,
        "managers": 1,
        "bdc_agents": 1,
        "daily_leads": 8,
        "close_rate_pct": 45,
        "month_shape": "realistic",
        "archetype_dist": {"rockstar": 1, "solid_mid": 2, "underperformer": 1, "new_hire": 0},
    },
    "bdc_heavy_phone": {
        "label": "BDC-Heavy Phone Store",
        "salespeople": 6,
        "managers": 2,
        "bdc_agents": 8,
        "daily_leads": 25,
        "close_rate_pct": 33,
        "month_shape": "realistic",
        "archetype_dist": {"rockstar": 1, "solid_mid": 4, "underperformer": 1, "new_hire": 0},
    },
}

SPEED_PRESETS: dict[str, dict] = {
    "realtime": {"label": "Realtime (1x)", "multiplier": 1.0},
    "1day_per_minute": {"label": "1 day per minute (1,440x)", "multiplier": 1440.0},
    "1week_per_hour": {"label": "1 week per hour (168x)", "multiplier": 168.0},
    "1month_per_hour": {"label": "1 month per hour (720x)", "multiplier": 720.0},
    "1month_per_10min": {"label": "1 month per 10 min (4,320x)", "multiplier": 4320.0},
    "custom": {"label": "Custom multiplier", "multiplier": None},
}

LEAD_SOURCES = ["internet", "phone", "showroom", "referral", "service", "walkin"]
DEAL_STATUSES = ["lead", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
ACTIVITY_TYPES = ["call", "email", "meeting", "demo", "note"]
SCENARIO_KEYS = [
    "slow_industry_month",
    "manager_on_vacation",
    "bdc_underperforming",
    "inventory_shortage",
    "strong_incentive_month",
    "high_heat_weekend",
]


@dataclass
class StoreConfig:
    dealership_id: str
    salespeople: int
    managers: int
    bdc_agents: int
    daily_leads: int
    batch_days: int
    every_seconds: int
    seed: int
    delivery: str
    api_url: str
    auth_token: str
    supabase_apikey: str
    sales_rep_ids: list[str]   # round-robin pool; empty = auto-fetch from profiles
    close_rate_pct: int
    status_advance_pct: int
    activities_per_deal_min: int
    activities_per_deal_max: int
    deal_amount_min: int
    deal_amount_max: int
    gross_profit_min: int
    gross_profit_max: int
    lead_sources: list[str]
    deal_statuses: list[str]
    activity_types: list[str]
    default_scenarios: list[str]
    month_shape: str
    archetype_dist: dict[str, int]
    new_hire_dates: list[date | None]
    sim_speed_preset: str
    sim_speed_multiplier: float
    sim_days_total: int
    sim_start_date: str
    output_file: Path


class StoreRunner:
    def __init__(self, config: StoreConfig) -> None:
        self.config = config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.status = "stopped"
        self.events_written = 0
        self.last_write_at: str | None = None
        self.last_api_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.status = "running"
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.status = "stopping"

    def _run_loop(self) -> None:
        team = build_team(
            salespeople=self.config.salespeople,
            managers=self.config.managers,
            bdc_agents=self.config.bdc_agents,
            archetype_dist=self.config.archetype_dist,
            new_hire_dates=self.config.new_hire_dates,
        )
        batch_counter = 0
        self.config.output_file.parent.mkdir(parents=True, exist_ok=True)

        preset = self.config.sim_speed_preset
        if preset == "custom":
            speed_mult = max(1.0, float(self.config.sim_speed_multiplier or 1.0))
        else:
            speed_mult = SPEED_PRESETS.get(preset, SPEED_PRESETS["realtime"])["multiplier"] or 1.0

        if speed_mult <= 1.0:
            sleep_seconds = float(self.config.every_seconds)
        else:
            sleep_seconds = (self.config.batch_days * 86400.0) / speed_mult

        if self.config.sim_start_date:
            try:
                sim_current = datetime.fromisoformat(self.config.sim_start_date).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                sim_current = datetime.now(timezone.utc)
        else:
            sim_current = datetime.now(timezone.utc)

        while not self._stop_event.is_set():
            if self.config.sim_days_total > 0 and (batch_counter * self.config.batch_days) >= self.config.sim_days_total:
                break

            seed = self.config.seed + batch_counter
            start_date = sim_current

            # Resolve rep pool: use configured IDs or auto-fetch from Supabase
            rep_ids = self.config.sales_rep_ids
            if not rep_ids and self.config.delivery in {"api", "both"}:
                api_base = self.config.api_url.rstrip("/").split("/functions/")[0].split("/rest/")[0]
                profiles = fetch_profiles_from_supabase(api_base, self.config.auth_token, self.config.supabase_apikey)
                rep_ids = [p["id"] for p in profiles if isinstance(p, dict) and p.get("id")]

            events = generate_events(
                start_date=start_date,
                days=self.config.batch_days,
                daily_leads=self.config.daily_leads,
                team=team,
                dealership_id=self.config.dealership_id,
                seed=seed,
                sales_rep_ids=rep_ids if rep_ids else None,
                base_close_rate=self.config.close_rate_pct / 100.0,
                deal_amount_min=self.config.deal_amount_min,
                deal_amount_max=self.config.deal_amount_max,
                gross_profit_min=self.config.gross_profit_min,
                gross_profit_max=self.config.gross_profit_max,
                activities_min=self.config.activities_per_deal_min,
                activities_max=self.config.activities_per_deal_max,
                month_shape=self.config.month_shape,
                scenarios=self.config.default_scenarios,
            )

            if self.config.delivery in {"file", "both"}:
                self._append_jsonl(events)

            if self.config.delivery in {"api", "both"}:
                result = send_events_to_api(
                    events=events,
                    api_url=self.config.api_url,
                    auth_token=self.config.auth_token,
                    supabase_apikey=self.config.supabase_apikey,
                )
                if result["failed"] > 0:
                    self.status = f"api_errors:{result['failed']}"
                    first_error = result["errors"][0] if result["errors"] else "unknown api error"
                    self.last_api_error = first_error[:140]
                elif self.status.startswith("api_errors"):
                    self.status = "running"
                    self.last_api_error = None

            batch_counter += 1
            sim_current += timedelta(days=self.config.batch_days)

            if self._stop_event.wait(sleep_seconds):
                break

        self.status = "stopped"

    def _append_jsonl(self, events: list[Event]) -> None:
        with self._lock:
            with self.config.output_file.open("a", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")
            self.events_written += len(events)
            self.last_write_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DealMakerGUI:
    def __init__(self, root: Tk) -> None:
        load_env_file()
        self.root = root
        self.root.title("DealMaker v2 Desktop")
        self.root.geometry("1240x820")

        self.runners: dict[str, StoreRunner] = {}
        self.store_credentials: dict[str, list[dict]] = {}

        self.selected_template_var = StringVar(value="custom")
        self.dealership_id_var = StringVar(value="DLR-001")
        self.salespeople_var = StringVar(value="8")
        self.managers_var = StringVar(value="2")
        self.bdc_var = StringVar(value="3")
        self.daily_leads_var = StringVar(value="20")
        self.batch_days_var = StringVar(value="1")
        self.every_seconds_var = StringVar(value="10")
        self.seed_var = StringVar(value="42")
        self.delivery_var = StringVar(value="file")
        self.api_url_var = StringVar(value=os.getenv("TOPREP_API_URL", ""))
        self.auth_token_var = StringVar(value=os.getenv("TOPREP_AUTH_TOKEN", ""))
        self.supabase_apikey_var = StringVar(value=os.getenv("SUPABASE_ANON_KEY", ""))
        self.output_dir_var = StringVar(value="output/stores")

        self.arch_rockstar_var = StringVar(value="1")
        self.arch_solid_mid_var = StringVar(value="5")
        self.arch_underperformer_var = StringVar(value="1")
        self.arch_new_hire_var = StringVar(value="1")
        self.new_hire_dates_var = StringVar(value="")
        self.deal_amount_min_var = StringVar(value="12000")
        self.deal_amount_max_var = StringVar(value="68000")
        self.gross_profit_min_var = StringVar(value="700")
        self.gross_profit_max_var = StringVar(value="6000")
        self.close_rate_pct_var = StringVar(value="36")
        self.status_advance_pct_var = StringVar(value="88")
        self.activities_per_deal_min_var = StringVar(value="2")
        self.activities_per_deal_max_var = StringVar(value="6")
        self.month_shape_var = StringVar(value="flat")
        self.sim_speed_preset_var = StringVar(value="realtime")
        self.sim_speed_multiplier_var = StringVar(value="720")
        self.sim_days_total_var = StringVar(value="30")
        self.sim_start_date_var = StringVar(value="")
        self.sales_rep_ids_var = StringVar(value="")

        self.settings_toprep_auth_token_var = StringVar(value=os.getenv("TOPREP_AUTH_TOKEN", ""))
        self.settings_database_url_var = StringVar(value=os.getenv("DATABASE_URL", ""))
        self.settings_service_role_var = StringVar(value=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
        self.settings_toprep_app_url_var = StringVar(value=os.getenv("TOPREP_APP_URL", ""))
        self.settings_login_email_var = StringVar(value="")
        self.settings_login_password_var = StringVar(value="")

        self.lead_source_vars: dict[str, StringVar] = {}
        self.deal_status_vars: dict[str, StringVar] = {}
        self.activity_type_vars: dict[str, StringVar] = {}
        self.scenario_vars: dict[str, StringVar] = {}
        self._init_option_vars()

        self._build_ui()
        self._refresh_loop()

    def _init_option_vars(self) -> None:
        self.lead_source_vars = {
            key: StringVar(value="1" if key in {"internet", "phone", "showroom"} else "0")
            for key in LEAD_SOURCES
        }
        self.deal_status_vars = {key: StringVar(value="1") for key in DEAL_STATUSES}
        self.activity_type_vars = {key: StringVar(value="1") for key in ACTIVITY_TYPES}
        self.scenario_vars = {key: StringVar(value="0") for key in SCENARIO_KEYS}

    @staticmethod
    def _credentials_file_path(store_id: str) -> Path:
        return Path("output/stores") / f"{store_id}.credentials.json"

    def _save_store_credentials(self, store_id: str, credentials: list[dict]) -> None:
        path = self._credentials_file_path(store_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "store_id": store_id,
            "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "credentials": credentials,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_store_credentials(self, store_id: str) -> list[dict]:
        path = self._credentials_file_path(store_id)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        rows = payload.get("credentials", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _resolve_api_url(raw_api_url: str) -> str:
        """Resolve delivery URL with the same fallback behavior as Flask routes."""
        fallback_url = raw_api_url.strip() or os.getenv("TOPREP_API_URL", "").strip() or _supabase_api_url()
        return normalize_delivery_url(fallback_url)

    @staticmethod
    def _resolve_api_keys(raw_auth_token: str, raw_supabase_apikey: str) -> tuple[str, str]:
        """Resolve auth/apikey values for Supabase delivery.

        Prefers explicit GUI values, then env vars, and finally project defaults.
        If no user JWT is configured, falls back to SUPABASE_SERVICE_ROLE_KEY so
        synthetic multi-rep event inserts can pass RLS.
        """
        auth_token = (
            raw_auth_token.strip()
            or os.getenv("TOPREP_AUTH_TOKEN", "").strip()
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        supabase_apikey = (
            raw_supabase_apikey.strip()
            or os.getenv("SUPABASE_ANON_KEY", "").strip()
            or _supabase_anon_key()
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        return auth_token, supabase_apikey

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        nav = ttk.Frame(outer)
        nav.pack(fill="x", pady=(0, 6))
        ttk.Label(nav, text="DealMaker v2 Desktop", font=("Segoe UI", 14, "bold")).pack(side="left")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        stores_tab = ttk.Frame(self.notebook, padding=10)
        new_store_tab = ttk.Frame(self.notebook, padding=10)
        settings_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(stores_tab, text="Stores")
        self.notebook.add(new_store_tab, text="+ New Store")
        self.notebook.add(settings_tab, text="Settings")

        inputs = ttk.LabelFrame(stores_tab, text="Store Controls", padding=10)
        inputs.pack(fill="x")

        button_row = ttk.Frame(inputs)
        button_row.grid(row=0, column=0, columnspan=6, sticky="w", pady=(2, 2))
        ttk.Button(button_row, text="Add + Start Store", command=self.create_store_from_form).pack(side="left")
        ttk.Button(button_row, text="Stop Selected", command=self.stop_selected).pack(side="left", padx=8)
        ttk.Button(button_row, text="Start Selected", command=self.start_selected).pack(side="left", padx=8)
        ttk.Button(button_row, text="View Credentials", command=self.show_selected_credentials).pack(side="left", padx=8)
        ttk.Button(button_row, text="Remove Selected", command=self.remove_selected).pack(side="left", padx=8)
        ttk.Button(button_row, text="Stop All", command=self.stop_all).pack(side="left", padx=8)

        table_frame = ttk.LabelFrame(stores_tab, text="Running Stores", padding=10)
        table_frame.pack(fill="both", expand=True, pady=(12, 0))

        columns = (
            "dealership_id",
            "status",
            "delivery",
            "salespeople",
            "managers",
            "bdc_agents",
            "daily_leads",
            "every_seconds",
            "events_written",
            "last_write_at",
            "last_api_error",
            "output_file",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)

        headings = {
            "dealership_id": "Store",
            "status": "Status",
            "delivery": "Delivery",
            "salespeople": "Reps",
            "managers": "Mgrs",
            "bdc_agents": "BDC",
            "daily_leads": "Leads/Day",
            "every_seconds": "Every(s)",
            "events_written": "Events",
            "last_write_at": "Last Write (UTC)",
            "last_api_error": "Last API Error",
            "output_file": "Output File",
        }

        widths = {
            "dealership_id": 100,
            "status": 90,
            "delivery": 80,
            "salespeople": 60,
            "managers": 60,
            "bdc_agents": 60,
            "daily_leads": 80,
            "every_seconds": 70,
            "events_written": 80,
            "last_write_at": 180,
            "last_api_error": 280,
            "output_file": 260,
        }

        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.pack(fill="both", expand=True)

        self._build_new_store_tab(new_store_tab)
        self._build_settings_tab(settings_tab)

    def _build_new_store_tab(self, parent: ttk.Frame) -> None:
        canvas = ttk.Frame(parent)
        canvas.pack(fill="both", expand=True)

        left = ttk.Frame(canvas)
        left.pack(fill="both", expand=True)

        header = ttk.LabelFrame(left, text="Store Template", padding=10)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Template").grid(row=0, column=0, sticky="w")
        template_options = [f"{key} - {cfg['label']}" for key, cfg in STORE_TEMPLATES.items()]
        template_combo = ttk.Combobox(
            header,
            textvariable=self.selected_template_var,
            values=[key for key in STORE_TEMPLATES.keys()],
            state="readonly",
            width=26,
        )
        template_combo.grid(row=0, column=1, sticky="w", padx=(8, 8))
        ttk.Button(header, text="Apply Template", command=self.apply_selected_template).grid(row=0, column=2, sticky="w")
        ttk.Label(header, text="Use template then override any fields below.").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        identity = ttk.LabelFrame(left, text="Identity", padding=10)
        identity.pack(fill="x", pady=(0, 8))
        self._add_field(identity, "Dealership ID", self.dealership_id_var, 0, 0)

        team = ttk.LabelFrame(left, text="Team Composition", padding=10)
        team.pack(fill="x", pady=(0, 8))
        self._add_field(team, "Sales Reps", self.salespeople_var, 0, 0)
        self._add_field(team, "Managers", self.managers_var, 0, 2)
        self._add_field(team, "BDC Agents", self.bdc_var, 0, 4)

        arch = ttk.LabelFrame(left, text="Rep Archetypes", padding=10)
        arch.pack(fill="x", pady=(0, 8))
        self._add_field(arch, "Rockstar", self.arch_rockstar_var, 0, 0)
        self._add_field(arch, "Solid Mid", self.arch_solid_mid_var, 0, 2)
        self._add_field(arch, "Underperformer", self.arch_underperformer_var, 1, 0)
        self._add_field(arch, "New Hire", self.arch_new_hire_var, 1, 2)
        ttk.Label(arch, text="New Hire Dates (comma-separated YYYY-MM-DD)").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 2))
        ttk.Entry(arch, textvariable=self.new_hire_dates_var, width=56).grid(row=2, column=2, columnspan=4, sticky="w")

        sources = ttk.LabelFrame(left, text="Lead Sources", padding=10)
        sources.pack(fill="x", pady=(0, 8))
        self._build_checkbox_grid(sources, self.lead_source_vars, columns=3)

        pipeline = ttk.LabelFrame(left, text="Deal Pipeline Stages", padding=10)
        pipeline.pack(fill="x", pady=(0, 8))
        self._build_checkbox_grid(pipeline, self.deal_status_vars, columns=3)

        activities = ttk.LabelFrame(left, text="Activity Types", padding=10)
        activities.pack(fill="x", pady=(0, 8))
        self._build_checkbox_grid(activities, self.activity_type_vars, columns=3)

        financials = ttk.LabelFrame(left, text="Deal Financial Ranges", padding=10)
        financials.pack(fill="x", pady=(0, 8))
        self._add_field(financials, "Deal Amount Min", self.deal_amount_min_var, 0, 0)
        self._add_field(financials, "Deal Amount Max", self.deal_amount_max_var, 0, 2)
        self._add_field(financials, "Gross Profit Min", self.gross_profit_min_var, 1, 0)
        self._add_field(financials, "Gross Profit Max", self.gross_profit_max_var, 1, 2)

        behavior = ttk.LabelFrame(left, text="Rep Behaviour Weights", padding=10)
        behavior.pack(fill="x", pady=(0, 8))
        self._add_field(behavior, "Base Close Rate %", self.close_rate_pct_var, 0, 0)
        self._add_field(behavior, "Status Advance %", self.status_advance_pct_var, 0, 2)
        self._add_field(behavior, "Activities/Deal Min", self.activities_per_deal_min_var, 1, 0)
        self._add_field(behavior, "Activities/Deal Max", self.activities_per_deal_max_var, 1, 2)

        shape = ttk.LabelFrame(left, text="Month Shape", padding=10)
        shape.pack(fill="x", pady=(0, 8))
        ttk.Combobox(shape, textvariable=self.month_shape_var, values=["flat", "realistic", "front_loaded"], state="readonly", width=22).grid(row=0, column=0, sticky="w")

        scenarios = ttk.LabelFrame(left, text="Default Stress Scenarios", padding=10)
        scenarios.pack(fill="x", pady=(0, 8))
        self._build_checkbox_grid(scenarios, self.scenario_vars, columns=2)

        speed = ttk.LabelFrame(left, text="Time Acceleration", padding=10)
        speed.pack(fill="x", pady=(0, 8))
        ttk.Label(speed, text="Speed Preset").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            speed,
            textvariable=self.sim_speed_preset_var,
            values=[key for key in SPEED_PRESETS.keys()],
            state="readonly",
            width=22,
        ).grid(row=0, column=1, sticky="w", padx=(8, 12))
        self._add_field(speed, "Custom Multiplier", self.sim_speed_multiplier_var, 1, 0)
        self._add_field(speed, "Total Sim Days (0=indef)", self.sim_days_total_var, 1, 2)
        self._add_field(speed, "Simulation Start Date", self.sim_start_date_var, 2, 0)

        runner = ttk.LabelFrame(left, text="Runner Configuration", padding=10)
        runner.pack(fill="x", pady=(0, 8))
        self._add_field(runner, "Daily Leads", self.daily_leads_var, 0, 0)
        self._add_field(runner, "Batch Days", self.batch_days_var, 0, 2)
        self._add_field(runner, "Loop Interval (s)", self.every_seconds_var, 0, 4)
        self._add_field(runner, "Seed", self.seed_var, 1, 0)
        self._add_field(runner, "Output Dir", self.output_dir_var, 1, 2)
        self._add_field(runner, "Rep IDs (optional)", self.sales_rep_ids_var, 2, 0)
        ttk.Label(runner, text="Delivery").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(runner, textvariable=self.delivery_var, values=["file", "api", "both"], state="readonly", width=14).grid(row=3, column=1, sticky="w")
        self._add_field(runner, "API URL", self.api_url_var, 4, 0)
        self._add_field(runner, "Auth Token", self.auth_token_var, 5, 0)
        self._add_field(runner, "Supabase API Key", self.supabase_apikey_var, 6, 0)

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Create Store", command=self.create_store_from_form).pack(side="left")
        ttk.Button(actions, text="Apply Template", command=self.apply_selected_template).pack(side="left", padx=8)

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        destination = ttk.LabelFrame(parent, text="Data Destination", padding=10)
        destination.pack(fill="x", pady=(0, 8))
        ttk.Label(destination, text=f"TopRep Server URL (fixed): {_TOPREP_SUPABASE_URL}").pack(anchor="w")

        token = ttk.LabelFrame(parent, text="Step 1 - Get Auth Token", padding=10)
        token.pack(fill="x", pady=(0, 8))
        self._add_field(token, "TopRep Email", self.settings_login_email_var, 0, 0)
        self._add_field(token, "Password", self.settings_login_password_var, 0, 2)
        ttk.Button(token, text="Sign In & Save Token", command=self.fetch_and_save_token).grid(row=1, column=0, sticky="w", pady=(6, 0))

        additional = ttk.LabelFrame(parent, text="Step 2 - Additional Credentials", padding=10)
        additional.pack(fill="x", pady=(0, 8))
        self._add_field(additional, "TOPREP_AUTH_TOKEN", self.settings_toprep_auth_token_var, 0, 0)
        self._add_field(additional, "DATABASE_URL", self.settings_database_url_var, 1, 0)
        self._add_field(additional, "SUPABASE_SERVICE_ROLE_KEY", self.settings_service_role_var, 2, 0)
        self._add_field(additional, "TOPREP_APP_URL", self.settings_toprep_app_url_var, 3, 0)

        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        ttk.Button(actions, text="Save Settings", command=self.save_settings_from_form).pack(side="left")
        ttk.Button(actions, text="Test Connection", command=self.test_settings_connection).pack(side="left", padx=8)

    def _build_checkbox_grid(self, parent: ttk.LabelFrame, vars_dict: dict[str, StringVar], columns: int = 3) -> None:
        for idx, key in enumerate(vars_dict.keys()):
            r = idx // columns
            c = idx % columns
            ttk.Checkbutton(parent, text=key.replace("_", " ").title(), variable=vars_dict[key], onvalue="1", offvalue="0").grid(
                row=r,
                column=c,
                sticky="w",
                padx=(0, 12),
                pady=2,
            )

    def apply_selected_template(self) -> None:
        key = self.selected_template_var.get().strip() or "custom"
        tpl = STORE_TEMPLATES.get(key)
        if not tpl:
            return
        self.salespeople_var.set(str(tpl.get("salespeople", 8)))
        self.managers_var.set(str(tpl.get("managers", 2)))
        self.bdc_var.set(str(tpl.get("bdc_agents", 3)))
        self.daily_leads_var.set(str(tpl.get("daily_leads", 20)))
        self.close_rate_pct_var.set(str(tpl.get("close_rate_pct", 36)))
        self.month_shape_var.set(str(tpl.get("month_shape", "flat")))
        dist = tpl.get("archetype_dist", {})
        self.arch_rockstar_var.set(str(dist.get("rockstar", 1)))
        self.arch_solid_mid_var.set(str(dist.get("solid_mid", 5)))
        self.arch_underperformer_var.set(str(dist.get("underperformer", 1)))
        self.arch_new_hire_var.set(str(dist.get("new_hire", 1)))

    def _parse_new_hire_dates(self) -> list[date | None]:
        raw = self.new_hire_dates_var.get().strip()
        if not raw:
            return []
        items = [piece.strip() for piece in raw.split(",") if piece.strip()]
        parsed: list[date | None] = []
        for token in items:
            try:
                parsed.append(date.fromisoformat(token))
            except ValueError:
                parsed.append(None)
        return parsed

    def create_store_from_form(self) -> None:
        try:
            config = self._build_config_from_values(
                dealership_id=self.dealership_id_var.get(),
                salespeople=self.salespeople_var.get(),
                managers=self.managers_var.get(),
                bdc_agents=self.bdc_var.get(),
                daily_leads=self.daily_leads_var.get(),
                batch_days=self.batch_days_var.get(),
                every_seconds=self.every_seconds_var.get(),
                seed=self.seed_var.get(),
                delivery=self.delivery_var.get(),
                api_url=self.api_url_var.get(),
                auth_token=self.auth_token_var.get(),
                supabase_apikey=self.supabase_apikey_var.get(),
                sales_rep_ids=self.sales_rep_ids_var.get(),
                output_dir=self.output_dir_var.get(),
                close_rate_pct=self.close_rate_pct_var.get(),
                status_advance_pct=self.status_advance_pct_var.get(),
                activities_per_deal_min=self.activities_per_deal_min_var.get(),
                activities_per_deal_max=self.activities_per_deal_max_var.get(),
                deal_amount_min=self.deal_amount_min_var.get(),
                deal_amount_max=self.deal_amount_max_var.get(),
                gross_profit_min=self.gross_profit_min_var.get(),
                gross_profit_max=self.gross_profit_max_var.get(),
                lead_sources=[k for k, v in self.lead_source_vars.items() if v.get() == "1"],
                deal_statuses=[k for k, v in self.deal_status_vars.items() if v.get() == "1"],
                activity_types=[k for k, v in self.activity_type_vars.items() if v.get() == "1"],
                default_scenarios=[k for k, v in self.scenario_vars.items() if v.get() == "1"],
                month_shape=self.month_shape_var.get(),
                arch_rockstar=self.arch_rockstar_var.get(),
                arch_solid_mid=self.arch_solid_mid_var.get(),
                arch_underperformer=self.arch_underperformer_var.get(),
                arch_new_hire=self.arch_new_hire_var.get(),
                new_hire_dates=self._parse_new_hire_dates(),
                sim_speed_preset=self.sim_speed_preset_var.get(),
                sim_speed_multiplier=self.sim_speed_multiplier_var.get(),
                sim_days_total=self.sim_days_total_var.get(),
                sim_start_date=self.sim_start_date_var.get(),
            )
        except ValueError as err:
            messagebox.showerror("Invalid input", str(err))
            return

        self._start_store_with_config(config)
        self.notebook.select(0)

    def save_settings_from_form(self) -> None:
        env_path = Path(__file__).parent / ".env"
        if not env_path.exists():
            env_path = Path(__file__).parent.parent / ".env"
        if not env_path.exists():
            env_path = Path(__file__).parent.parent.parent / ".env"

        existing: dict[str, str] = {}
        if env_path.exists():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip().strip('"').strip("'")

        updates = {
            "TOPREP_AUTH_TOKEN": self.settings_toprep_auth_token_var.get().strip(),
            "DATABASE_URL": self.settings_database_url_var.get().strip(),
            "SUPABASE_SERVICE_ROLE_KEY": self.settings_service_role_var.get().strip(),
            "TOPREP_APP_URL": self.settings_toprep_app_url_var.get().strip(),
        }
        for key, value in updates.items():
            if value:
                existing[key] = value
                os.environ[key] = value

        env_path.write_text("\n".join([f'{k}="{v}"' for k, v in existing.items()]) + "\n", encoding="utf-8")
        messagebox.showinfo("Settings", "Settings saved.")

    def fetch_and_save_token(self) -> None:
        email = self.settings_login_email_var.get().strip()
        password = self.settings_login_password_var.get()
        if not email or not password:
            messagebox.showerror("Missing credentials", "TopRep email and password are required.")
            return

        import json as _json
        from urllib import error as _url_error, request as _url_request

        auth_url = f"{_TOPREP_SUPABASE_URL}/auth/v1/token?grant_type=password"
        body = _json.dumps({"email": email, "password": password}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "apikey": _supabase_anon_key(),
        }
        req = _url_request.Request(auth_url, data=body, headers=headers, method="POST")
        try:
            with _url_request.urlopen(req, timeout=15) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        except _url_error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:300]
            messagebox.showerror("Token fetch failed", f"HTTP {exc.code}: {body_text}")
            return
        except Exception as exc:
            messagebox.showerror("Token fetch failed", str(exc))
            return

        token = payload.get("access_token", "")
        if len(token.split(".")) != 3:
            messagebox.showerror("Invalid token", "Unexpected token format returned by Supabase.")
            return

        self.settings_toprep_auth_token_var.set(token)
        self.auth_token_var.set(token)
        os.environ["TOPREP_AUTH_TOKEN"] = token
        self.save_settings_from_form()
        messagebox.showinfo("Auth token", "Token fetched and saved.")

    def test_settings_connection(self) -> None:
        result = check_connection()
        if result.get("ok"):
            messagebox.showinfo("Connection", str(result.get("message", "Connected")))
        else:
            messagebox.showerror("Connection", str(result.get("error", "Connection failed")))

    def _add_field(self, parent: ttk.LabelFrame, label: str, var: StringVar, row: int, col: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(parent, textvariable=var, width=18).grid(row=row, column=col + 1, sticky="w", padx=(0, 14), pady=4)

    def _read_positive_int(self, value: str, label: str, minimum: int = 1) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if parsed < minimum:
            raise ValueError(f"{label} must be >= {minimum}")
        return parsed

    def _build_config_from_values(
        self,
        dealership_id: str,
        salespeople: str,
        managers: str,
        bdc_agents: str,
        daily_leads: str,
        batch_days: str,
        every_seconds: str,
        seed: str,
        delivery: str,
        api_url: str,
        auth_token: str,
        supabase_apikey: str,
        sales_rep_ids: str,
        output_dir: str,
        close_rate_pct: str = "36",
        status_advance_pct: str = "88",
        activities_per_deal_min: str = "2",
        activities_per_deal_max: str = "6",
        deal_amount_min: str = "12000",
        deal_amount_max: str = "68000",
        gross_profit_min: str = "700",
        gross_profit_max: str = "6000",
        lead_sources: list[str] | None = None,
        deal_statuses: list[str] | None = None,
        activity_types: list[str] | None = None,
        default_scenarios: list[str] | None = None,
        month_shape: str = "flat",
        arch_rockstar: str = "1",
        arch_solid_mid: str = "5",
        arch_underperformer: str = "1",
        arch_new_hire: str = "1",
        new_hire_dates: list[date | None] | None = None,
        sim_speed_preset: str = "realtime",
        sim_speed_multiplier: str = "1",
        sim_days_total: str = "0",
        sim_start_date: str = "",
    ) -> StoreConfig:
        dealership_id = dealership_id.strip()
        if not dealership_id:
            raise ValueError("Dealership ID is required")

        salespeople_int = self._read_positive_int(salespeople, "Sales Reps")
        managers_int = self._read_positive_int(managers, "Managers")
        bdc_agents_int = self._read_positive_int(bdc_agents, "BDC Agents")
        daily_leads_int = self._read_positive_int(daily_leads, "Daily Leads")
        batch_days_int = self._read_positive_int(batch_days, "Batch Days")
        every_seconds_int = self._read_positive_int(every_seconds, "Every Seconds")

        try:
            seed_int = int(seed)
        except ValueError as exc:
            raise ValueError("Seed must be an integer") from exc

        close_rate_pct_int = self._read_positive_int(close_rate_pct, "Base Close Rate", minimum=1)
        status_advance_pct_int = self._read_positive_int(status_advance_pct, "Status Advance Rate", minimum=1)
        activities_min_int = self._read_positive_int(activities_per_deal_min, "Activities / Deal min", minimum=1)
        activities_max_int = self._read_positive_int(activities_per_deal_max, "Activities / Deal max", minimum=1)
        deal_amount_min_int = self._read_positive_int(deal_amount_min, "Deal Amount Min", minimum=1)
        deal_amount_max_int = self._read_positive_int(deal_amount_max, "Deal Amount Max", minimum=1)
        gross_profit_min_int = self._read_positive_int(gross_profit_min, "Gross Profit Min", minimum=0)
        gross_profit_max_int = self._read_positive_int(gross_profit_max, "Gross Profit Max", minimum=0)

        if activities_max_int < activities_min_int:
            raise ValueError("Activities / Deal max must be >= min")
        if deal_amount_max_int < deal_amount_min_int:
            raise ValueError("Deal Amount Max must be >= Deal Amount Min")
        if gross_profit_max_int < gross_profit_min_int:
            raise ValueError("Gross Profit Max must be >= Gross Profit Min")

        try:
            sim_speed_multiplier_value = max(1.0, float(sim_speed_multiplier)) if sim_speed_multiplier else 1.0
        except ValueError as exc:
            raise ValueError("Custom speed multiplier must be a number") from exc

        try:
            sim_days_total_value = max(0, int(sim_days_total)) if sim_days_total else 0
        except ValueError as exc:
            raise ValueError("Total Sim Days must be an integer >= 0") from exc

        sim_start_date_value = sim_start_date.strip()
        if sim_start_date_value:
            try:
                date.fromisoformat(sim_start_date_value)
            except ValueError as exc:
                raise ValueError("Simulation Start Date must be YYYY-MM-DD") from exc

        delivery_value = delivery.strip().lower() or "file"
        if delivery_value not in {"file", "api", "both"}:
            raise ValueError("Delivery must be file, api, or both")

        api_url_value = self._resolve_api_url(api_url)
        auth_token_value, supabase_apikey_value = self._resolve_api_keys(auth_token, supabase_apikey)

        # Parse comma-separated rep IDs; empty = auto-fetch from Supabase at runtime
        raw_ids = sales_rep_ids.strip() or os.getenv("TOPREP_SALES_REP_IDS", "")
        rep_ids_list = [r.strip() for r in raw_ids.split(",") if r.strip()]

        lead_sources_value = lead_sources or [key for key, v in self.lead_source_vars.items() if v.get() == "1"]
        deal_statuses_value = deal_statuses or [key for key, v in self.deal_status_vars.items() if v.get() == "1"]
        activity_types_value = activity_types or [key for key, v in self.activity_type_vars.items() if v.get() == "1"]
        default_scenarios_value = default_scenarios or [key for key, v in self.scenario_vars.items() if v.get() == "1"]

        archetype_dist = {
            "rockstar": max(0, int(arch_rockstar or "0")),
            "solid_mid": max(0, int(arch_solid_mid or "0")),
            "underperformer": max(0, int(arch_underperformer or "0")),
            "new_hire": max(0, int(arch_new_hire or "0")),
        }

        parsed_hire_dates = new_hire_dates or []

        if delivery_value in {"api", "both"}:
            validate_api_settings(
                api_url=api_url_value,
                auth_token=auth_token_value,
                supabase_apikey=supabase_apikey_value,
            )

        output_dir = output_dir.strip() or "output/stores"
        output_file = Path(output_dir) / f"{dealership_id}.jsonl"

        return StoreConfig(
            dealership_id=dealership_id,
            salespeople=salespeople_int,
            managers=managers_int,
            bdc_agents=bdc_agents_int,
            daily_leads=daily_leads_int,
            batch_days=batch_days_int,
            every_seconds=every_seconds_int,
            seed=seed_int,
            delivery=delivery_value,
            api_url=api_url_value,
            auth_token=auth_token_value,
            supabase_apikey=supabase_apikey_value,
            sales_rep_ids=rep_ids_list,
            close_rate_pct=close_rate_pct_int,
            status_advance_pct=status_advance_pct_int,
            activities_per_deal_min=activities_min_int,
            activities_per_deal_max=activities_max_int,
            deal_amount_min=deal_amount_min_int,
            deal_amount_max=deal_amount_max_int,
            gross_profit_min=gross_profit_min_int,
            gross_profit_max=gross_profit_max_int,
            lead_sources=lead_sources_value or ["internet", "phone", "showroom"],
            deal_statuses=deal_statuses_value or ["lead", "qualified", "closed_won", "closed_lost"],
            activity_types=activity_types_value or ["call", "email", "meeting"],
            default_scenarios=default_scenarios_value,
            month_shape=month_shape,
            archetype_dist=archetype_dist,
            new_hire_dates=parsed_hire_dates,
            sim_speed_preset=sim_speed_preset,
            sim_speed_multiplier=sim_speed_multiplier_value,
            sim_days_total=sim_days_total_value,
            sim_start_date=sim_start_date_value,
            output_file=output_file,
        )

    def _auto_provision_store_reps(self, config: StoreConfig) -> list[dict]:
        """Provision test users for desktop-created stores when service role is available."""
        if not os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip():
            return []

        store_config = {
            "dealership_id": config.dealership_id,
            "salespeople": config.salespeople,
            "archetype_dist": config.archetype_dist,
        }
        return provision_store_reps(store_config)

    def open_add_store_dialog(self) -> None:
        dialog = Toplevel(self.root)
        dialog.title("Add Store")
        dialog.geometry("560x290")
        dialog.transient(self.root)
        dialog.grab_set()

        dealership_id_var = StringVar(value=self.dealership_id_var.get())
        salespeople_var = StringVar(value=self.salespeople_var.get())
        managers_var = StringVar(value=self.managers_var.get())
        bdc_var = StringVar(value=self.bdc_var.get())
        daily_leads_var = StringVar(value=self.daily_leads_var.get())
        batch_days_var = StringVar(value=self.batch_days_var.get())
        every_seconds_var = StringVar(value=self.every_seconds_var.get())
        seed_var = StringVar(value=self.seed_var.get())
        delivery_var = StringVar(value=self.delivery_var.get())
        api_url_var = StringVar(value=self.api_url_var.get())
        auth_token_var = StringVar(value=self.auth_token_var.get())
        supabase_apikey_var = StringVar(value=self.supabase_apikey_var.get())
        sales_rep_ids_var = StringVar(value="")   # blank = auto-fetch from profiles
        output_dir_var = StringVar(value=self.output_dir_var.get())

        form = ttk.Frame(dialog, padding=12)
        form.pack(fill="both", expand=True)

        self._add_field(form, "Dealership ID", dealership_id_var, 0, 0)
        self._add_field(form, "Sales Reps", salespeople_var, 0, 2)
        self._add_field(form, "Managers", managers_var, 0, 4)

        self._add_field(form, "BDC Agents", bdc_var, 1, 0)
        self._add_field(form, "Daily Leads", daily_leads_var, 1, 2)
        self._add_field(form, "Batch Days", batch_days_var, 1, 4)

        self._add_field(form, "Every Seconds", every_seconds_var, 2, 0)
        self._add_field(form, "Seed", seed_var, 2, 2)
        self._add_field(form, "Output Dir", output_dir_var, 2, 4)
        self._add_field(form, "Delivery", delivery_var, 3, 0)
        self._add_field(form, "API URL", api_url_var, 3, 2)
        self._add_field(form, "Auth Token", auth_token_var, 3, 4)
        self._add_field(form, "Supabase API Key", supabase_apikey_var, 4, 0)
        self._add_field(form, "Rep IDs (blank=auto)", sales_rep_ids_var, 4, 2)

        button_row = ttk.Frame(form)
        button_row.grid(row=5, column=0, columnspan=6, sticky="w", pady=(12, 0))

        def submit() -> None:
            self.add_store(
                dealership_id=dealership_id_var.get(),
                salespeople=salespeople_var.get(),
                managers=managers_var.get(),
                bdc_agents=bdc_var.get(),
                daily_leads=daily_leads_var.get(),
                batch_days=batch_days_var.get(),
                every_seconds=every_seconds_var.get(),
                seed=seed_var.get(),
                delivery=delivery_var.get(),
                api_url=api_url_var.get(),
                auth_token=auth_token_var.get(),
                supabase_apikey=supabase_apikey_var.get(),
                sales_rep_ids=sales_rep_ids_var.get(),
                output_dir=output_dir_var.get(),
            )
            if dealership_id_var.get().strip() in self.runners:
                self.dealership_id_var.set(dealership_id_var.get())
                self.salespeople_var.set(salespeople_var.get())
                self.managers_var.set(managers_var.get())
                self.bdc_var.set(bdc_var.get())
                self.daily_leads_var.set(daily_leads_var.get())
                self.batch_days_var.set(batch_days_var.get())
                self.every_seconds_var.set(every_seconds_var.get())
                self.seed_var.set(seed_var.get())
                self.delivery_var.set(delivery_var.get())
                self.api_url_var.set(api_url_var.get())
                self.auth_token_var.set(auth_token_var.get())
                self.supabase_apikey_var.set(supabase_apikey_var.get())
                self.output_dir_var.set(output_dir_var.get())
                dialog.destroy()

        ttk.Button(button_row, text="Add + Start", command=submit).pack(side="left")
        ttk.Button(button_row, text="Cancel", command=dialog.destroy).pack(side="left", padx=8)

    def add_store(
        self,
        dealership_id: str,
        salespeople: str,
        managers: str,
        bdc_agents: str,
        daily_leads: str,
        batch_days: str,
        every_seconds: str,
        seed: str,
        delivery: str,
        api_url: str,
        auth_token: str,
        supabase_apikey: str,
        sales_rep_ids: str,
        output_dir: str,
    ) -> None:
        try:
            config = self._build_config_from_values(
                dealership_id=dealership_id,
                salespeople=salespeople,
                managers=managers,
                bdc_agents=bdc_agents,
                daily_leads=daily_leads,
                batch_days=batch_days,
                every_seconds=every_seconds,
                seed=seed,
                delivery=delivery,
                api_url=api_url,
                auth_token=auth_token,
                supabase_apikey=supabase_apikey,
                sales_rep_ids=sales_rep_ids,
                output_dir=output_dir,
            )
        except ValueError as err:
            messagebox.showerror("Invalid input", str(err))
            return

        if config.dealership_id in self.runners:
            messagebox.showerror("Duplicate store", f"Store '{config.dealership_id}' already exists.")
            return

        self._start_store_with_config(config)

    def _start_store_with_config(self, config: StoreConfig) -> None:
        if config.dealership_id in self.runners:
            messagebox.showerror("Duplicate store", f"Store '{config.dealership_id}' already exists.")
            return

        credentials: list[dict] = []
        disk_credentials = self._load_store_credentials(config.dealership_id)
        try:
            credentials = self._auto_provision_store_reps(config)
        except Exception as err:
            messagebox.showwarning(
                "Provisioning warning",
                f"Store added, but auto-provisioning failed: {err}",
            )

        runner = StoreRunner(config)
        self.runners[config.dealership_id] = runner
        runner.start()
        self._refresh_table()

        if credentials:
            self.store_credentials[config.dealership_id] = credentials
            self._save_store_credentials(config.dealership_id, credentials)
            success_count = sum(1 for item in credentials if not item.get("error"))
            error_count = len(credentials) - success_count
            preview_lines = []
            for item in credentials[:8]:
                status = "OK" if not item.get("error") else f"ERR: {item.get('error', '')[:40]}"
                preview_lines.append(
                    f"{item.get('email', 'unknown')} / {item.get('password', 'unknown')} [{status}]"
                )
            if len(credentials) > 8:
                preview_lines.append(f"... and {len(credentials) - 8} more")
            messagebox.showinfo(
                "Store Added + Provisioned",
                (
                    f"Store {config.dealership_id} added and started.\n\n"
                    f"Provisioned logins: {success_count}\n"
                    f"Provisioning errors: {error_count}\n\n"
                    "Credentials preview:\n"
                    + "\n".join(preview_lines)
                ),
            )
        elif disk_credentials:
            self.store_credentials[config.dealership_id] = disk_credentials
            messagebox.showinfo(
                "Store Added",
                (
                    f"Store {config.dealership_id} added and started.\n\n"
                    f"Loaded {len(disk_credentials)} cached credential(s) from disk."
                ),
            )

    def _credentials_to_text(self, store_id: str, credentials: list[dict]) -> str:
        lines = [f"Store: {store_id}", "", "email,password,status,user_id"]
        for item in credentials:
            status = "ok" if not item.get("error") else f"error:{str(item.get('error', ''))}"
            email = str(item.get("email", "")).replace(",", " ")
            password = str(item.get("password", "")).replace(",", " ")
            user_id = str(item.get("user_id", "")).replace(",", " ")
            status = status.replace(",", " ")
            lines.append(f"{email},{password},{status},{user_id}")
        return "\n".join(lines)

    def show_selected_credentials(self) -> None:
        store_id = self._selected_store_id()
        if not store_id:
            messagebox.showinfo("No store selected", "Select a store row first.")
            return

        credentials = self.store_credentials.get(store_id, [])
        if not credentials:
            credentials = self._load_store_credentials(store_id)
            if credentials:
                self.store_credentials[store_id] = credentials
        if not credentials:
            messagebox.showinfo(
                "No credentials",
                (
                    f"No cached credentials for {store_id}.\n\n"
                    "Credentials are generated when auto-provisioning runs "
                    "(requires SUPABASE_SERVICE_ROLE_KEY) and are saved under "
                    "output/stores/*.credentials.json."
                ),
            )
            return

        dialog = Toplevel(self.root)
        dialog.title(f"Credentials - {store_id}")
        dialog.geometry("920x420")
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill="both", expand=True)

        columns = ("email", "password", "status", "user_id")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        tree.heading("email", text="Email")
        tree.heading("password", text="Password")
        tree.heading("status", text="Status")
        tree.heading("user_id", text="User ID")
        tree.column("email", width=280, anchor="w")
        tree.column("password", width=120, anchor="w")
        tree.column("status", width=220, anchor="w")
        tree.column("user_id", width=280, anchor="w")

        for item in credentials:
            status = "OK" if not item.get("error") else f"ERR: {str(item.get('error', ''))[:100]}"
            tree.insert(
                "",
                END,
                values=(
                    item.get("email", ""),
                    item.get("password", ""),
                    status,
                    item.get("user_id", ""),
                ),
            )

        tree.pack(fill="both", expand=True)

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(10, 0))

        def copy_all() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._credentials_to_text(store_id, credentials))
            messagebox.showinfo("Copied", "All credentials copied to clipboard as CSV.")

        def copy_selected_row() -> None:
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("No row selected", "Select a credential row first.")
                return
            item = tree.item(selected[0])
            values = item.get("values", [])
            if len(values) < 4:
                return
            row = f"{values[0]},{values[1]},{values[2]},{values[3]}"
            self.root.clipboard_clear()
            self.root.clipboard_append(row)
            messagebox.showinfo("Copied", "Selected credential row copied to clipboard.")

        ttk.Button(button_row, text="Copy Selected", command=copy_selected_row).pack(side="left")
        ttk.Button(button_row, text="Copy All (CSV)", command=copy_all).pack(side="left", padx=8)
        ttk.Button(button_row, text="Close", command=dialog.destroy).pack(side="right")

    def _selected_store_id(self) -> str | None:
        selected = self.tree.selection()
        if not selected:
            return None
        item = self.tree.item(selected[0])
        values = item.get("values", [])
        return values[0] if values else None

    def stop_selected(self) -> None:
        dealership_id = self._selected_store_id()
        if not dealership_id:
            return
        runner = self.runners.get(dealership_id)
        if runner:
            runner.stop()
        self._refresh_table()

    def start_selected(self) -> None:
        dealership_id = self._selected_store_id()
        if not dealership_id:
            return
        runner = self.runners.get(dealership_id)
        if runner:
            runner.start()
        self._refresh_table()

    def remove_selected(self) -> None:
        dealership_id = self._selected_store_id()
        if not dealership_id:
            return
        runner = self.runners.get(dealership_id)
        if runner:
            runner.stop()
            self.runners.pop(dealership_id, None)
        self.store_credentials.pop(dealership_id, None)
        self._refresh_table()

    def stop_all(self) -> None:
        for runner in self.runners.values():
            runner.stop()
        self._refresh_table()

    def _refresh_table(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        for dealership_id, runner in sorted(self.runners.items(), key=lambda item: item[0]):
            config = runner.config
            self.tree.insert(
                "",
                END,
                values=(
                    dealership_id,
                    runner.status,
                    config.delivery,
                    config.salespeople,
                    config.managers,
                    config.bdc_agents,
                    config.daily_leads,
                    config.every_seconds,
                    runner.events_written,
                    runner.last_write_at or "-",
                    runner.last_api_error or "-",
                    str(config.output_file),
                ),
            )

    def _refresh_loop(self) -> None:
        self._refresh_table()
        self.root.after(1000, self._refresh_loop)


def main() -> None:
    root = Tk()
    app = DealMakerGUI(root)
    root.protocol("WM_DELETE_WINDOW", lambda: on_close(root, app))
    root.mainloop()


def on_close(root: Tk, app: DealMakerGUI) -> None:
    app.stop_all()
    time.sleep(0.2)
    root.destroy()


if __name__ == "__main__":
    main()
