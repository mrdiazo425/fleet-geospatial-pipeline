"""End-to-end: raw reports in Bronze become enriched rows, alerts and KPIs.

Synthetic world (EPSG:9377, far from real data): a 10 km east-west road with
a 60 km/h limit, inside a city geofence (50 km/h), crossing a high-accident
zone (30 km/h) between km 4 and km 6. One vehicle drives it at 54 km/h.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import add_geofence, add_road

VEHICLE = "VHC-0001-TST"
X0, Y0 = 4_880_000.0, 2_070_000.0
START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
STEP_S, STEP_M, SPEED = 10, 150.0, 54.0  # 54 km/h = 15 m/s
REPORTS = 60  # 9 km


def box(x_from, x_to):
    return (
        f"MULTIPOLYGON((({X0 + x_from} {Y0 - 500}, {X0 + x_to} {Y0 - 500}, "
        f"{X0 + x_to} {Y0 + 500}, {X0 + x_from} {Y0 + 500}, {X0 + x_from} {Y0 - 500})))"
    )


@pytest.fixture
def world(conn):
    route = f"LINESTRING({X0} {Y0}, {X0 + 10_000} {Y0})"
    add_geofence(conn, "CITY", box(-1000, 11_000), geofence_type="urban_perimeter", limit=50)
    zone_id = add_geofence(conn, "ZONE", box(4000, 6000), limit=30)
    add_road(conn, route, limit=60)
    conn.execute("INSERT INTO reference.vehicles VALUES (%s, 60, 10)", (VEHICLE,))
    conn.execute(
        "INSERT INTO reference.vehicle_routes (vehicle_id, geom) "
        "VALUES (%s, ST_GeomFromText(%s, 9377))",
        (VEHICLE, route),
    )
    return {"zone_id": zone_id}


def insert_trip(conn, *, fuel_start=80.0, fuel_per_step=0.025):
    """Insert the trip as raw GPS reports (lat/lon), like a device would."""
    for i in range(REPORTS):
        conn.execute(
            """
            INSERT INTO bronze.telemetry_raw
                (vehicle_id, reported_at, latitude, longitude, speed_kmh, fuel_level_pct)
            SELECT %s, %s, ST_Y(p), ST_X(p), %s, %s
            FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 9377), 4326) AS p
            """,
            (
                VEHICLE,
                START + timedelta(seconds=i * STEP_S),
                SPEED,
                max(fuel_start - i * fuel_per_step, 0),
                X0 + i * STEP_M,
                Y0,
            ),
        )


def process(conn):
    return conn.execute("SELECT * FROM silver.process_new_telemetry()").fetchone()


def enriched(conn, columns):
    return conn.execute(
        f"SELECT {columns} FROM silver.telemetry_enriched ORDER BY reported_at"
    ).fetchall()


def alerts(conn, alert_type):
    return conn.execute(
        "SELECT severity, geofence_id, observed, threshold FROM silver.alerts "
        "WHERE alert_type = %s ORDER BY reported_at",
        (alert_type,),
    ).fetchall()


def test_every_report_is_enriched_once(conn, world):
    insert_trip(conn)
    assert process(conn)[0] == REPORTS
    assert process(conn) == (0, 0)  # watermark: nothing is processed twice
    assert len(enriched(conn, "telemetry_id")) == REPORTS


def test_geofence_membership_per_report(conn, world):
    insert_trip(conn)
    process(conn)
    rows = enriched(conn, f"ST_X(geom_9377) - {X0}, in_urban_perimeter, high_accident_geofence_id")
    for offset_m, in_city, zone_id in rows:
        assert in_city is True
        expected_zone = world["zone_id"] if 4000 <= round(offset_m) <= 6000 else None
        assert zone_id == expected_zone, f"wrong zone at {offset_m:.0f} m"


def test_speed_limit_adapts_to_the_geofence(conn, world):
    insert_trip(conn)
    process(conn)
    rows = enriched(
        conn, "high_accident_geofence_id IS NOT NULL, speed_limit_kmh, speeding_severity"
    )
    # outside the zone: min(road 60, city 50) = 50 -> 54 km/h is 4 over = minor
    assert {(limit, severity) for in_zone, limit, severity in rows if not in_zone} == {
        (50, "minor")
    }
    # inside the zone: 30 km/h -> 24 over = severe
    assert {(limit, severity) for in_zone, limit, severity in rows if in_zone} == {(30, "severe")}


def test_speeding_alerts_carry_speed_limit_and_zone(conn, world):
    insert_trip(conn)
    process(conn)
    speeding = alerts(conn, "speeding")
    assert len(speeding) == REPORTS
    in_zone = [a for a in speeding if a[1] == world["zone_id"]]
    assert len(in_zone) == 14  # reports between 4000 m and 6000 m inclusive, every 150 m
    assert all(a == ("severe", world["zone_id"], 54.0, 30.0) for a in in_zone)


def test_zone_entry_is_alerted_once_on_the_transition(conn, world):
    insert_trip(conn)
    process(conn)
    assert alerts(conn, "zone_entry") == [("info", world["zone_id"], 54.0, 30.0)]


def test_step_distance_and_remaining_route(conn, world):
    insert_trip(conn)
    process(conn)
    rows = enriched(conn, "step_seconds, step_distance_m, acceleration_ms2, route_remaining_km")
    assert rows[0][:3] == (None, None, None)  # first report has no previous one
    assert rows[0][3] == pytest.approx(10.0, abs=0.01)
    for seconds, distance, acceleration, _ in rows[1:]:
        assert seconds == STEP_S
        assert distance == pytest.approx(STEP_M, abs=0.5)  # survives the 4326 round trip
        assert acceleration == 0
    assert rows[-1][3] == pytest.approx(10.0 - 59 * 0.15, abs=0.01)


def test_enough_fuel_raises_no_alert(conn, world):
    insert_trip(conn, fuel_start=80.0)
    process(conn)
    assert alerts(conn, "fuel_insufficient") == []
    assert {row[0] for row in enriched(conn, "fuel_insufficient")} == {False}


def test_insufficient_fuel_is_predicted_before_the_tank_is_empty(conn, world):
    """1.2 % of fuel cannot cover 10 km at ~0.167 %/km (needs ~1.83 % with reserve)."""
    insert_trip(conn, fuel_start=1.2, fuel_per_step=0.025)
    process(conn)
    fuel_alerts = alerts(conn, "fuel_insufficient")
    assert len(fuel_alerts) == 1  # raised once, on the transition
    _, _, autonomy_km, needed_km = fuel_alerts[0]
    assert autonomy_km == pytest.approx(1.2 / (100 / 600), rel=0.01)  # 7.2 km of range
    assert needed_km == pytest.approx(11.0, abs=0.05)  # 10 km + 10 % reserve
    first_fuel = enriched(conn, "fuel_level_pct")[0][0]
    assert first_fuel > 0  # alerted while there was still fuel


def test_observed_consumption_replaces_nominal_after_one_km(conn, world):
    # 0.05 % per 150 m = 0.333 %/km, twice the nominal 0.167 %/km
    insert_trip(conn, fuel_start=80.0, fuel_per_step=0.05)
    process(conn)
    rates = [row[0] for row in enriched(conn, "fuel_rate_pct_km")]
    assert rates[0] == pytest.approx(100 / 600, rel=1e-3)  # no history yet: nominal
    assert rates[-1] == pytest.approx(0.05 / 0.15, rel=0.02)  # observed


def test_gap_longer_than_five_minutes_starts_a_new_trip(conn, world):
    insert_trip(conn)
    conn.execute(
        """
        INSERT INTO bronze.telemetry_raw
            (vehicle_id, reported_at, latitude, longitude, speed_kmh, fuel_level_pct)
        SELECT %s, %s, ST_Y(p), ST_X(p), 0, 95
        FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 9377), 4326) AS p
        """,
        (VEHICLE, START + timedelta(hours=1), X0, Y0),
    )
    process(conn)
    last = enriched(conn, "step_seconds, step_distance_m, fuel_insufficient")[-1]
    assert last == (None, None, False)  # the jump back to the depot is not distance


def test_gold_fact_and_driver_score(conn, world):
    insert_trip(conn)
    process(conn)
    assert conn.execute("SELECT gold.sync_fact_telemetry()").fetchone()[0] == REPORTS
    assert conn.execute("SELECT gold.sync_fact_telemetry()").fetchone()[0] == 0
    conn.execute("SELECT gold.refresh_aggregates()")

    in_zone, geofence_code = conn.execute(
        "SELECT count(*) FILTER (WHERE in_high_accident_zone), max(geofence_code) "
        "FROM gold.fact_telemetry"
    ).fetchone()
    assert (in_zone, geofence_code) == (14, "ZONE")

    distance_km, speeding_pct, score = conn.execute(
        "SELECT distance_km, speeding_time_pct, driver_score FROM gold.driver_score_daily"
    ).fetchone()
    assert float(distance_km) == pytest.approx(59 * 0.15, abs=0.01)
    assert float(speeding_pct) == 100.0
    assert float(score) == 50.0  # speeding penalty is capped at 50 points

    assert (
        conn.execute("SELECT sum(infractions) FROM gold.infraction_hotspots").fetchone()[0]
        == REPORTS
    )


def test_analyst_views_never_expose_the_vehicle_id(conn, world):
    insert_trip(conn)
    process(conn)
    conn.execute("SELECT gold.sync_fact_telemetry()")
    conn.execute("SELECT gold.refresh_aggregates()")
    for view in (
        "bronze.telemetry_masked",
        "gold.v_fact_telemetry",
        "gold.v_driver_score_daily",
        "gold.v_alerts",
    ):
        ids = {row[0] for row in conn.execute(f"SELECT DISTINCT vehicle_id FROM {view}")}
        assert ids == {"VHC-****-TST"}, view


def test_gold_keeps_the_transmission_delay_for_device_health(conn, world):
    """Report 0 arrives 90 s late, report 1 is a historical replay, the rest are instant."""
    insert_trip(conn)
    conn.execute(
        """
        UPDATE bronze.telemetry_raw
        SET ingested_at = reported_at + CASE WHEN reported_at = %(first)s
                                             THEN interval '90 seconds' ELSE interval '0' END,
            is_backfill = (reported_at = %(second)s)
        """,
        {"first": START, "second": START + timedelta(seconds=STEP_S)},
    )
    process(conn)
    conn.execute("SELECT gold.sync_fact_telemetry()")
    delays = [
        row[0]
        for row in conn.execute(
            "SELECT arrival_delay_s FROM gold.fact_telemetry ORDER BY reported_at"
        )
    ]
    assert delays[0] == 90
    assert delays[1] is None  # a replay is not a late transmission
    assert set(delays[2:]) == {0}


def test_road_speed_profile_feeds_route_optimisation(conn, world):
    insert_trip(conn)
    process(conn)
    conn.execute("SELECT gold.sync_fact_telemetry()")
    conn.execute("SELECT gold.refresh_aggregates()")
    rows = conn.execute(
        "SELECT hour_of_day, speed_limit_kmh, reports, vehicles, observed_speed_kmh::float, "
        "congestion_ratio::float, speeding_reports_pct::float, ST_SRID(geom) "
        "FROM gold.road_speed_profile"
    ).fetchall()
    # one road, driven at a constant 54 km/h at 07:00 local time (12:00 UTC)
    assert rows == [(7, 60, REPORTS, 1, 54.0, 0.9, 100.0, 4326)]
