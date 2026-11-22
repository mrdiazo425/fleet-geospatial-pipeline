"""Speed limit resolution and speeding alerts."""

import pytest

from tests.conftest import add_geofence, add_road, square_wkt


def scalar(conn, query, params=()):
    return conn.execute(query, params).fetchone()[0]


@pytest.mark.parametrize(
    ("road", "geofence", "expected"),
    [
        (50, 30, 30),  # geofence is stricter
        (30, 50, 30),  # road is stricter
        (60, None, 60),  # no geofence: road limit
        (None, 30, 30),  # no road nearby: geofence limit
        (None, None, 50),  # nothing known: general urban limit
        (40, 40, 40),
    ],
)
def test_most_restrictive_limit_wins(conn, road, geofence, expected):
    assert (
        scalar(conn, "SELECT silver.applicable_speed_limit(%s, %s)", (road, geofence)) == expected
    )


@pytest.mark.parametrize(
    ("speed", "limit", "severity"),
    [
        (30, 30, None),
        (33, 30, None),  # inside the 3 km/h tolerance
        (33.1, 30, "minor"),
        (40, 30, "minor"),  # exactly 10 over
        (40.1, 30, "major"),
        (50, 30, "major"),  # exactly 20 over
        (50.1, 30, "severe"),
        (120, 50, "severe"),
        (0, 50, None),
    ],
)
def test_speeding_severity_boundaries(conn, speed, limit, severity):
    query = "SELECT silver.speeding_severity(%s::real, %s)"
    assert scalar(conn, query, (speed, limit)) == severity


def test_same_speed_is_an_infraction_only_inside_the_geofence(conn):
    """The limit adapts dynamically to the geofence the vehicle is in."""
    add_geofence(conn, "CITY", square_wkt(0, 0, 10_000), geofence_type="urban_perimeter", limit=50)
    add_geofence(conn, "ZONE", square_wkt(1000, 1000, 1000), limit=30)
    query = """
        SELECT l.speed_limit_kmh, silver.speeding_severity(45::real, l.speed_limit_kmh)
        FROM silver.speed_limit_at(ST_SetSRID(ST_MakePoint(%s, %s), 9377)) AS l
    """
    assert conn.execute(query, (1500, 1500)).fetchone() == (30, "major")
    assert conn.execute(query, (5000, 5000)).fetchone() == (50, None)


def test_road_limit_applies_when_stricter_than_the_geofence(conn):
    add_geofence(conn, "CITY", square_wkt(0, 0, 10_000), geofence_type="urban_perimeter", limit=50)
    add_road(conn, "LINESTRING(0 5000, 10000 5000)", limit=30)
    limit = scalar(
        conn,
        "SELECT speed_limit_kmh "
        "FROM silver.speed_limit_at(ST_SetSRID(ST_MakePoint(5000, 5010), 9377))",
    )
    assert limit == 30


def test_nearest_road_is_chosen_and_far_roads_are_ignored(conn):
    near = add_road(conn, "LINESTRING(0 0, 1000 0)", limit=30)
    add_road(conn, "LINESTRING(0 20, 1000 20)", limit=60)
    query = """
        SELECT road_id, round(distance_m)
        FROM silver.nearest_road(ST_SetSRID(ST_MakePoint(500, %s), 9377))
    """
    assert conn.execute(query, (5,)).fetchone() == (near, 5)
    assert conn.execute(query, (500,)).fetchone() is None  # > 30 m from any road


@pytest.mark.parametrize(
    ("seconds", "distance", "valid"),
    [
        (5, 70, True),  # 50 km/h
        (300, 3000, True),  # longest accepted gap
        (301, 3000, False),  # ignition was off: new trip
        (0, 10, False),  # duplicated timestamp
        (5, 400, False),  # 288 km/h: GPS jump
        (None, None, False),  # first report of a vehicle
    ],
)
def test_step_validation(conn, seconds, distance, valid):
    assert scalar(conn, "SELECT silver.is_valid_step(%s, %s)", (seconds, distance)) is valid


def test_gps_drift_while_stopped_is_not_distance(conn):
    assert scalar(conn, "SELECT silver.step_distance_m(6, 0::real, 0::real)") == 0
    assert scalar(conn, "SELECT silver.step_distance_m(6, 20::real, 0::real)") == 6


@pytest.mark.parametrize(
    ("acceleration", "event"),
    [(2.5, "harsh_acceleration"), (2.4, None), (-3.0, "harsh_braking"), (-2.9, None), (0, None)],
)
def test_harsh_event_thresholds(conn, acceleration, event):
    assert scalar(conn, "SELECT silver.harsh_event(%s)", (acceleration,)) == event
