"""Predictive fuel algorithm: will the fuel left cover the rest of the route?"""

import pytest


def scalar(conn, query, params=()):
    return conn.execute(query, params).fetchone()[0]


def test_nominal_rate_from_tank_and_efficiency(conn):
    # 60 l tank at 10 km/l = 600 km per tank -> 1/6 % of the tank per km
    rate = scalar(conn, "SELECT silver.nominal_fuel_rate_pct_km(60, 10)")
    assert rate == pytest.approx(100 / 600)


@pytest.mark.parametrize(
    ("drop_pct", "km", "expected"),
    [
        (1.0, 5.0, 0.20),  # enough evidence: observed rate
        (0.3, 0.5, 0.10),  # less than 1 km observed: nominal
        (-40.0, 5.0, 0.10),  # refuelled inside the window: nominal
        (0.0, 5.0, 0.10),  # no measurable drop: nominal
        (5.0, 5.0, 0.30),  # sensor glitch: clamped to 3x nominal
        (0.1, 5.0, 0.05),  # implausibly low: clamped to 0.5x nominal
        (None, None, 0.10),  # first report: nominal
    ],
)
def test_consumption_rate_prefers_observed_but_stays_plausible(conn, drop_pct, km, expected):
    rate = scalar(conn, "SELECT silver.fuel_rate_pct_km(0.10, %s, %s)", (drop_pct, km))
    assert rate == pytest.approx(expected)


def test_autonomy_is_fuel_divided_by_rate(conn):
    assert scalar(conn, "SELECT silver.fuel_autonomy_km(50, 0.2)") == pytest.approx(250)
    assert scalar(conn, "SELECT silver.fuel_autonomy_km(0, 0.2)") == 0
    assert scalar(conn, "SELECT silver.fuel_autonomy_km(50, 0)") is None


@pytest.mark.parametrize(
    ("autonomy_km", "remaining_km", "alert"),
    [
        (100, 50, False),  # plenty
        (55.1, 50, False),  # just above route + 10 % reserve
        (54.9, 50, True),  # eats into the reserve
        (10, 50, True),  # will not arrive
        (0, 0.5, True),  # empty tank
        (5, 0, False),  # already at destination
    ],
)
def test_alert_when_range_does_not_cover_route_plus_reserve(conn, autonomy_km, remaining_km, alert):
    query = "SELECT silver.is_fuel_insufficient(%s, %s)"
    assert scalar(conn, query, (autonomy_km, remaining_km)) is alert


def test_worked_example_end_to_end(conn):
    """60 l tank, 10 km/l, 3 % fuel left and 20 km to go.

    Observed: 0.9 % burnt over the last 4.5 km -> 0.2 %/km (worse than the
    nominal 0.167 %/km). Range = 3 / 0.2 = 15 km < 20 km * 1.1 -> alert.
    With the nominal rate the range would be 18 km: still an alert, but the
    observed rate is what makes the prediction honest about current traffic.
    """
    rate, autonomy, alert = conn.execute(
        """
        SELECT r.rate, a.autonomy, silver.is_fuel_insufficient(a.autonomy, 20)
        FROM (SELECT silver.fuel_rate_pct_km(silver.nominal_fuel_rate_pct_km(60, 10), 0.9, 4.5)
                  AS rate) AS r,
             LATERAL (SELECT silver.fuel_autonomy_km(3, r.rate) AS autonomy) AS a
        """
    ).fetchone()
    assert rate == pytest.approx(0.2)
    assert autonomy == pytest.approx(15)
    assert alert is True
