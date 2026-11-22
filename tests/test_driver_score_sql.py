"""Driver Score formula."""

import pytest


def score(conn, speeding_ratio, harsh_per_100km, fuel_alerts):
    return float(
        conn.execute(
            "SELECT gold.driver_score(%s, %s, %s)", (speeding_ratio, harsh_per_100km, fuel_alerts)
        ).fetchone()[0]
    )


def test_perfect_driver_scores_100(conn):
    assert score(conn, 0, 0, 0) == 100
    assert score(conn, None, None, 0) == 100  # vehicle that did not move


@pytest.mark.parametrize(
    ("speeding_ratio", "harsh", "fuel", "expected"),
    [
        (0.10, 0, 0, 90),  # 10 % of driving time in minor speeding
        (0.50, 0, 0, 50),
        (2.00, 0, 0, 50),  # speeding penalty capped at 50
        (0, 10, 0, 95),  # 10 harsh events per 100 km
        (0, 500, 0, 75),  # harshness penalty capped at 25
        (0, 0, 1, 95),
        (0, 0, 9, 90),  # fuel penalty capped at 10
        (9, 999, 9, 15),  # worst case never goes below 15
    ],
)
def test_penalties_and_caps(conn, speeding_ratio, harsh, fuel, expected):
    assert score(conn, speeding_ratio, harsh, fuel) == expected


def test_speeding_in_a_high_accident_zone_weighs_more(conn):
    def weight(severity, in_zone):
        query = "SELECT gold.speeding_weight(%s, %s)"
        return conn.execute(query, (severity, in_zone)).fetchone()[0]

    assert [weight(s, False) for s in ("minor", "major", "severe", None)] == [1, 2, 3, 0]
    assert weight("severe", True) == pytest.approx(4.5)
