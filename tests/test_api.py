"""HTTP API: ingestion, validation and masking by role."""

import psycopg
import pytest
from fastapi.testclient import TestClient

VEHICLE = "VHC-0002-API"
PACKET = {
    "vehicle_id": VEHICLE,
    "timestamp": "2026-11-01T08:00:00-05:00",
    "latitude": 4.711,
    "longitude": -74.0721,
    "speed_kmh": 42.5,
    "fuel_level_pct": 63.2,
}


@pytest.fixture
def client(_test_database, monkeypatch):
    monkeypatch.setenv("DB_NAME", _test_database.db_name)
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    from fleet_pipeline.api.app import app

    with TestClient(app) as test_client:
        yield test_client
    # The API commits for real, so remove what these tests stored.
    with psycopg.connect(_test_database.conninfo) as conn:
        conn.execute("DELETE FROM bronze.telemetry_raw WHERE vehicle_id = %s", (VEHICLE,))


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_single_packet_is_stored_and_retransmission_is_ignored(client):
    first = client.post("/telemetry", json=PACKET)
    assert first.status_code == 201
    assert first.json() == {"received": 1, "inserted": 1, "duplicates": 0}
    again = client.post("/telemetry", json=PACKET)
    assert again.json() == {"received": 1, "inserted": 0, "duplicates": 1}


def test_batch_ingestion(client):
    batch = [{**PACKET, "timestamp": f"2026-11-01T08:00:{s:02d}-05:00"} for s in range(10)]
    assert client.post("/telemetry", json=batch).json()["inserted"] == 10


@pytest.mark.parametrize(
    "bad",
    [
        {**PACKET, "vehicle_id": "ABC"},
        {**PACKET, "latitude": 95},
        {**PACKET, "fuel_level_pct": 140},
        {k: v for k, v in PACKET.items() if k != "speed_kmh"},
        [],
    ],
)
def test_invalid_payloads_are_rejected_and_nothing_is_stored(client, bad):
    assert client.post("/telemetry", json=bad).status_code == 422
    assert client.get("/telemetry", params={"vehicle_id": "VHC-****-API"}).json()["count"] == 0


def test_vehicle_id_is_masked_unless_the_admin_key_is_sent(client):
    client.post("/telemetry", json=PACKET)

    anonymous = client.get("/telemetry").json()
    assert anonymous["role"] == "analyst"
    assert {item["vehicle_id"] for item in anonymous["items"]} == {"VHC-****-API"}

    wrong_key = client.get("/telemetry", headers={"X-API-Key": "nope"}).json()
    assert wrong_key["role"] == "analyst"

    admin = client.get("/telemetry", headers={"X-API-Key": "test-admin-key"}).json()
    assert admin["role"] == "admin"
    assert {item["vehicle_id"] for item in admin["items"]} == {VEHICLE}


def test_non_admin_cannot_search_by_the_real_id(client):
    client.post("/telemetry", json=PACKET)
    assert client.get("/telemetry", params={"vehicle_id": VEHICLE}).json()["count"] == 0


def test_backfill_flag_is_stored(client, _test_database):
    client.post("/telemetry", json=PACKET, params={"backfill": "true"})
    live = {**PACKET, "timestamp": "2026-11-01T08:00:05-05:00"}
    client.post("/telemetry", json=live)
    with psycopg.connect(_test_database.conninfo) as conn:
        flags = conn.execute(
            "SELECT is_backfill FROM bronze.telemetry_raw WHERE vehicle_id = %s "
            "ORDER BY reported_at",
            (VEHICLE,),
        ).fetchall()
    assert flags == [(True,), (False,)]
