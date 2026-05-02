"""Fortellis mock contract tests for TopRep ingestion."""
from __future__ import annotations

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app.routes.fortellis_mock as fortellis_mod
    import app.routes.stores as stores_mod

    monkeypatch.setattr(stores_mod, "_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(stores_mod, "_STORES_FILE", tmp_path / "stores_config.json")
    stores_mod._stores.clear()
    fortellis_mod._cache.clear()

    store_id = "DLR-FORTELLIS-TEST"
    stores_mod._stores[store_id] = {
        "dealership_id": store_id,
        "display_name": "Fortellis Test Store",
        "salespeople": 3,
        "managers": 1,
        "bdc_agents": 0,
        "daily_leads": 4,
        "seed": 42,
        "delivery": "file",
        "close_rate_pct": 45,
        "deal_amount_min": 12000,
        "deal_amount_max": 68000,
        "gross_profit_min": 700,
        "gross_profit_max": 6000,
        "activities_per_deal_min": 2,
        "activities_per_deal_max": 4,
        "month_shape": "flat",
        "archetype_dist": {"rockstar": 1, "solid_mid": 1, "underperformer": 1, "new_hire": 0},
        "default_scenarios": [],
        "new_hire_dates": [],
        "batch_days": 1,
        "sim_speed_preset": "realtime",
        "sim_days_total": 0,
        "sim_start_date": "",
        "status": "stopped",
        "events_sent": 0,
        "credentials": [],
    }

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client, store_id


def _headers(store_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer mock-fortellis-token",
        "Subscription-Id": store_id,
    }


def test_token_endpoint_returns_fortellis_bearer_shape(client):
    test_client, _store_id = client

    resp = test_client.post("/oauth2/aus1p1ixy7YL8cMq02p7/v1/token")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["access_token"] == "mock-fortellis-token"
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0


def test_activity_types_are_returned_as_catalog(client):
    test_client, _store_id = client

    resp = test_client.get("/sales/v1/elead/activities/activityTypes")

    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, list)
    assert {"activityTypeId": 1, "activityTypeName": "call"} in body


def test_opportunities_search_returns_paginated_leads(client):
    test_client, store_id = client

    resp = test_client.get(
        "/sales/v2/elead/opportunities/search?page=1&pageSize=5",
        headers=_headers(store_id),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"opportunities", "totalItems", "totalPages", "currentPage"}
    assert body["currentPage"] == 1
    assert len(body["opportunities"]) <= 5
    assert body["totalItems"] > 5

    opportunity = body["opportunities"][0]
    assert {
        "opportunityId",
        "salesPersonId",
        "leadSource",
        "customerName",
        "status",
        "createdDate",
        "updatedDate",
    }.issubset(opportunity)
    assert opportunity["salesPersonId"].startswith("S-")


def test_sold_status_filter_returns_deal_shape(client):
    test_client, store_id = client

    resp = test_client.get(
        "/sales/v2/elead/opportunities/search?status=sold&pageSize=100",
        headers=_headers(store_id),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["opportunities"], "expected sold deals from 90-day synthetic dataset"

    deal = body["opportunities"][0]
    assert {
        "opportunityId",
        "salesPersonId",
        "status",
        "saleAmount",
        "customerName",
        "closeDate",
    }.issubset(deal)
    assert deal["status"] == "sold"


def test_activity_history_uses_opportunity_id(client):
    test_client, store_id = client

    lead_resp = test_client.get(
        "/sales/v2/elead/opportunities/search?pageSize=1",
        headers=_headers(store_id),
    )
    opportunity_id = lead_resp.get_json()["opportunities"][0]["opportunityId"]

    resp = test_client.get(
        f"/sales/v1/elead/activities/history/byOpportunityId/{opportunity_id}",
        headers=_headers(store_id),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert "activities" in body
    assert isinstance(body["activities"], list)
    if body["activities"]:
        activity = body["activities"][0]
        assert {"activityId", "activityName", "assignedTo"}.issubset(activity)


def test_employees_return_member_ids_for_toprep_rep_mapping(client):
    test_client, store_id = client

    resp = test_client.get(
        "/sales/v1/elead/reference/employees",
        headers=_headers(store_id),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert "employees" in body
    assert len(body["employees"]) == 3
    assert body["employees"][0]["employeeId"] == "S-001"
    assert {"employeeId", "firstName", "lastName"}.issubset(body["employees"][0])


def test_unknown_subscription_id_returns_404(client):
    test_client, _store_id = client

    resp = test_client.get(
        "/sales/v2/elead/opportunities/search",
        headers=_headers("missing-store"),
    )

    assert resp.status_code == 404
    assert "No DealMaker store found" in resp.get_json()["error"]


def test_sync_info_includes_fortellis_mock_configuration(client):
    test_client, store_id = client

    resp = test_client.get(f"/stores/{store_id}/sync-info", base_url="http://dealmaker.test")

    assert resp.status_code == 200
    body = resp.get_json()
    fortellis = body["fortellis_mock"]
    assert fortellis["base_url"] == "http://dealmaker.test"
    assert fortellis["subscription_id"] == store_id
    assert fortellis["headers"]["Subscription-Id"] == store_id
    assert fortellis["endpoints"]["sold_deals"].endswith("status=sold")
    assert fortellis["rep_mapping"]["toprep_rep_field"] == "employee_external_id"


def test_builtin_toprep_store_has_static_team():
    from app.routes.stores import TOPREP_TEST_STORE_ID, _toprep_test_store_defaults, build_store_team
    from dealmaker_generator import sales_rep_uuid

    store = _toprep_test_store_defaults()
    team = build_store_team(store)

    assert store["dealership_id"] == TOPREP_TEST_STORE_ID
    assert store["delivery"] == "api"
    assert [member.member_id for member in team] == [
        "S-001",
        "S-002",
        "S-003",
        "S-004",
        "S-005",
        "S-006",
        "M-001",
    ]
    assert team[0].name == "Avery Johnson"
    assert sales_rep_uuid(TOPREP_TEST_STORE_ID, team[0])


def test_builtin_toprep_store_is_added_to_registry():
    from app.routes.stores import TOPREP_TEST_STORE_ID, _ensure_builtin_stores

    stores = {}

    changed = _ensure_builtin_stores(stores)

    assert changed is True
    assert TOPREP_TEST_STORE_ID in stores
    assert stores[TOPREP_TEST_STORE_ID]["is_static_test_store"] is True
    assert stores[TOPREP_TEST_STORE_ID]["static_team"][0]["member_id"] == "S-001"
