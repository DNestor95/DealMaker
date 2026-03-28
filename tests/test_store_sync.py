from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    import app.routes.stores as stores_mod

    monkeypatch.setattr(stores_mod, "_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(stores_mod, "_STORES_FILE", tmp_path / "stores_config.json")
    monkeypatch.setattr(stores_mod, "_save_stores", lambda stores: None)
    monkeypatch.setenv("TOPREP_API_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    stores_mod._stores.clear()

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app.test_client(), stores_mod
    stores_mod._stores.clear()


def test_create_store_syncs_canonical_store_row(app_client, monkeypatch):
    client, stores_mod = app_client
    captured: list[tuple[str, bool]] = []

    monkeypatch.setattr(stores_mod, "upsert_store", lambda store, active=True: captured.append((store["dealership_id"], active)) or {})
    monkeypatch.setattr(stores_mod, "priors_from_archetypes", lambda **kwargs: [])
    monkeypatch.setattr(stores_mod, "seed_source_stage_priors", lambda *args, **kwargs: {})

    response = client.post(
        "/stores/new",
        data={"dealership_id": "DLR-SYNC-1", "salespeople": "2", "managers": "1", "bdc_agents": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert captured == [("DLR-SYNC-1", True)]


def test_create_store_rolls_back_when_canonical_store_sync_fails(app_client, monkeypatch):
    client, stores_mod = app_client

    monkeypatch.setattr(stores_mod, "upsert_store", lambda store, active=True: {"error": "sync failed"})

    response = client.post(
        "/stores/new",
        data={"dealership_id": "DLR-SYNC-FAIL", "salespeople": "2", "managers": "1", "bdc_agents": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert b"Failed to create canonical store row" in response.data
    assert "DLR-SYNC-FAIL" not in stores_mod._stores


def test_update_store_syncs_canonical_store_row(app_client, monkeypatch):
    client, stores_mod = app_client
    stores_mod._stores["DLR-SYNC-2"] = {
        "dealership_id": "DLR-SYNC-2",
        "salespeople": 2,
        "managers": 1,
        "bdc_agents": 1,
        "daily_leads": 3,
        "lead_sources": ["internet"],
        "deal_statuses": ["lead", "qualified", "closed_won", "closed_lost"],
        "activity_types": ["call", "email", "meeting"],
        "activity_outcomes": ["connected", "appt_set", "showed", "sold"],
        "deal_amount_min": 12000,
        "deal_amount_max": 68000,
        "gross_profit_min": 700,
        "gross_profit_max": 6000,
        "close_rate_pct": 36,
        "status_advance_pct": 88,
        "contact_rate_pct": 72,
        "appointment_rate_pct": 55,
        "showroom_rate_pct": 65,
        "negotiation_rate_pct": 80,
        "activities_per_deal_min": 2,
        "activities_per_deal_max": 6,
        "archetype_dist": {},
        "new_hire_dates": [],
        "month_shape": "flat",
        "default_scenarios": [],
        "delivery": "file",
        "batch_days": 1,
        "every_seconds": 10,
        "seed": 42,
        "sim_speed_preset": "realtime",
        "sim_speed_multiplier": 1.0,
        "sim_days_total": 0,
        "sim_start_date": "",
        "status": "stopped",
        "events_sent": 0,
        "credentials": [],
    }

    captured: list[tuple[str, bool, int]] = []
    monkeypatch.setattr(
        stores_mod,
        "upsert_store",
        lambda store, active=True: captured.append((store["dealership_id"], active, store["salespeople"])) or {},
    )

    response = client.post(
        "/stores/DLR-SYNC-2/edit",
        data={"salespeople": "4", "managers": "1", "bdc_agents": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert captured == [("DLR-SYNC-2", True, 4)]


def test_delete_store_purges_all_associated_data(app_client, monkeypatch):
    client, stores_mod = app_client
    stores_mod._stores["DLR-SYNC-3"] = {"dealership_id": "DLR-SYNC-3", "status": "stopped", "events_sent": 0}
    captured: list[str] = []

    monkeypatch.setattr(
        stores_mod, "purge_store_data",
        lambda store_id: captured.append(store_id) or {"ok": True, "details": {}, "errors": {}},
    )

    response = client.post("/stores/DLR-SYNC-3/delete", follow_redirects=False)

    assert response.status_code == 302
    assert captured == ["DLR-SYNC-3"]
    assert "DLR-SYNC-3" not in stores_mod._stores


def test_store_detail_matches_profiles_using_canonical_store_uuid(app_client, monkeypatch):
    client, stores_mod = app_client
    store_id = "DLR-SYNC-4"
    stores_mod._stores[store_id] = {
        "dealership_id": store_id,
        "status": "stopped",
        "events_sent": 0,
        "delivery": "file",
        "daily_leads": 3,
        "batch_days": 1,
        "every_seconds": 10,
        "close_rate_pct": 36,
        "status_advance_pct": 88,
        "activities_per_deal_min": 2,
        "activities_per_deal_max": 6,
        "month_shape": "flat",
        "salespeople": 2,
        "managers": 1,
        "bdc_agents": 1,
        "archetype_dist": {},
        "new_hire_dates": [],
        "default_scenarios": [],
        "lead_sources": ["internet"],
        "deal_statuses": ["lead"],
        "activity_types": ["call"],
        "credentials": [],
        "sim_speed_preset": "realtime",
        "sim_speed_multiplier": 1.0,
        "sim_days_total": 0,
        "sim_start_date": "",
    }
    monkeypatch.setattr(
        stores_mod,
        "get_profiles",
        lambda: [{"id": "rep-1", "first_name": "Sales", "last_name": "Rep", "role": "sales_rep", "store_id": stores_mod.canonical_store_uuid(store_id)}],
    )

    response = client.get(f"/stores/{store_id}")

    assert response.status_code == 200
    assert b"Sales Rep" in response.data


def test_app_startup_reconciles_existing_store_rows(tmp_path, monkeypatch):
    import app.routes.stores as stores_mod

    monkeypatch.setattr(stores_mod, "_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(stores_mod, "_STORES_FILE", tmp_path / "stores_config.json")
    monkeypatch.setattr(stores_mod, "_save_stores", lambda stores: None)
    monkeypatch.setenv("TOPREP_API_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    stores_mod._stores.clear()
    stores_mod._stores["DLR-BOOT-1"] = {
        "dealership_id": "DLR-BOOT-1",
        "salespeople": 2,
        "managers": 1,
        "bdc_agents": 1,
        "daily_leads": 3,
        "lead_sources": ["internet"],
        "deal_statuses": ["lead", "qualified", "closed_won", "closed_lost"],
        "activity_types": ["call", "email", "meeting"],
        "activity_outcomes": ["connected", "appt_set", "showed", "sold"],
        "deal_amount_min": 12000,
        "deal_amount_max": 68000,
        "gross_profit_min": 700,
        "gross_profit_max": 6000,
        "close_rate_pct": 36,
        "status_advance_pct": 88,
        "contact_rate_pct": 72,
        "appointment_rate_pct": 55,
        "showroom_rate_pct": 65,
        "negotiation_rate_pct": 80,
        "activities_per_deal_min": 2,
        "activities_per_deal_max": 6,
        "archetype_dist": {},
        "new_hire_dates": [],
        "month_shape": "flat",
        "default_scenarios": [],
        "delivery": "file",
        "batch_days": 1,
        "every_seconds": 10,
        "seed": 42,
        "sim_speed_preset": "realtime",
        "sim_speed_multiplier": 1.0,
        "sim_days_total": 0,
        "sim_start_date": "",
        "status": "stopped",
        "events_sent": 0,
        "credentials": [],
    }

    captured: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        stores_mod,
        "upsert_store",
        lambda store, active=True: captured.append((store["dealership_id"], active)) or {},
    )

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True

    assert captured == [("DLR-BOOT-1", True)]
    stores_mod._stores.clear()