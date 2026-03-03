from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, StringVar, Tk, Toplevel
from tkinter import messagebox
from tkinter import ttk

from dealmaker_generator import Event, build_team, generate_events


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
        )
        batch_counter = 0
        self.config.output_file.parent.mkdir(parents=True, exist_ok=True)

        while not self._stop_event.is_set():
            seed = self.config.seed + batch_counter
            start_date = datetime.now(timezone.utc)
            events = generate_events(
                start_date=start_date,
                days=self.config.batch_days,
                daily_leads=self.config.daily_leads,
                team=team,
                dealership_id=self.config.dealership_id,
                seed=seed,
            )
            self._append_jsonl(events)
            batch_counter += 1

            if self._stop_event.wait(self.config.every_seconds):
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
        self.root = root
        self.root.title("DealMaker Multi-Store Runner")
        self.root.geometry("980x620")

        self.runners: dict[str, StoreRunner] = {}

        self.dealership_id_var = StringVar(value="DLR-001")
        self.salespeople_var = StringVar(value="8")
        self.managers_var = StringVar(value="2")
        self.bdc_var = StringVar(value="3")
        self.daily_leads_var = StringVar(value="20")
        self.batch_days_var = StringVar(value="1")
        self.every_seconds_var = StringVar(value="10")
        self.seed_var = StringVar(value="42")
        self.output_dir_var = StringVar(value="output/stores")

        self._build_ui()
        self._refresh_loop()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        inputs = ttk.LabelFrame(frame, text="Store Runner Controls", padding=10)
        inputs.pack(fill="x")

        button_row = ttk.Frame(inputs)
        button_row.grid(row=0, column=0, columnspan=6, sticky="w", pady=(2, 2))
        ttk.Button(button_row, text="Add + Start Store", command=self.open_add_store_dialog).pack(side="left")
        ttk.Button(button_row, text="Stop Selected", command=self.stop_selected).pack(side="left", padx=8)
        ttk.Button(button_row, text="Start Selected", command=self.start_selected).pack(side="left", padx=8)
        ttk.Button(button_row, text="Remove Selected", command=self.remove_selected).pack(side="left", padx=8)
        ttk.Button(button_row, text="Stop All", command=self.stop_all).pack(side="left", padx=8)

        table_frame = ttk.LabelFrame(frame, text="Running Stores", padding=10)
        table_frame.pack(fill="both", expand=True, pady=(12, 0))

        columns = (
            "dealership_id",
            "status",
            "salespeople",
            "managers",
            "bdc_agents",
            "daily_leads",
            "every_seconds",
            "events_written",
            "last_write_at",
            "output_file",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)

        headings = {
            "dealership_id": "Store",
            "status": "Status",
            "salespeople": "Reps",
            "managers": "Mgrs",
            "bdc_agents": "BDC",
            "daily_leads": "Leads/Day",
            "every_seconds": "Every(s)",
            "events_written": "Events",
            "last_write_at": "Last Write (UTC)",
            "output_file": "Output File",
        }

        widths = {
            "dealership_id": 100,
            "status": 90,
            "salespeople": 60,
            "managers": 60,
            "bdc_agents": 60,
            "daily_leads": 80,
            "every_seconds": 70,
            "events_written": 80,
            "last_write_at": 180,
            "output_file": 300,
        }

        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.pack(fill="both", expand=True)

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
        output_dir: str,
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
            output_file=output_file,
        )

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

        button_row = ttk.Frame(form)
        button_row.grid(row=3, column=0, columnspan=6, sticky="w", pady=(12, 0))

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
                output_dir=output_dir,
            )
        except ValueError as err:
            messagebox.showerror("Invalid input", str(err))
            return

        if config.dealership_id in self.runners:
            messagebox.showerror("Duplicate store", f"Store '{config.dealership_id}' already exists.")
            return

        runner = StoreRunner(config)
        self.runners[config.dealership_id] = runner
        runner.start()
        self._refresh_table()

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
                    config.salespeople,
                    config.managers,
                    config.bdc_agents,
                    config.daily_leads,
                    config.every_seconds,
                    runner.events_written,
                    runner.last_write_at or "-",
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
