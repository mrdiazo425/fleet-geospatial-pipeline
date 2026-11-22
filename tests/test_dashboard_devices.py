from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fleet_pipeline.dashboard.devices import (
    LATE,
    ON_TIME,
    classify_delay,
    device_summary,
    transmitted,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def test_delay_classification_boundaries():
    delays = pd.Series([0.0, 0.5, 60, 60.1, 7200])
    assert classify_delay(delays).tolist() == [ON_TIME, ON_TIME, ON_TIME, LATE, LATE]


def points(rows):
    return pd.DataFrame(rows, columns=["vehicle_id", "arrival_delay_s"])


def seen(**minutes_ago):
    return pd.DataFrame(
        {
            "vehicle_id": list(minutes_ago),
            "last_report": [pd.Timestamp(NOW - timedelta(minutes=m)) for m in minutes_ago.values()],
        }
    )


def test_historical_replays_are_not_transmission_delays():
    sent = transmitted(points([("A", 0.5), ("A", None), ("A", 80)]))
    assert sent["arrival_delay_s"].tolist() == [0.5, 80]
    assert sent["arrival"].tolist() == [ON_TIME, LATE]


def test_summary_reports_delay_and_late_share_per_vehicle():
    data = points([("A", 0.4), ("A", 0.6), ("A", 90), ("A", 0.5), ("B", 1.0), ("B", None)])
    summary = device_summary(data, seen(A=0.1, B=0.1), NOW).set_index("vehicle_id")
    assert summary.loc["A", "median_delay_s"] == pytest.approx(0.55)
    assert summary.loc["A", "max_delay_s"] == 90
    assert summary.loc["A", "late_pct"] == 25
    assert summary.loc["A", "reports"] == 4
    assert summary.loc["B", "reports"] == 1  # the replayed report is ignored


def test_vehicle_is_silent_only_after_the_threshold():
    summary = device_summary(points([]), seen(A=1, B=15, C=15.5, D=240), NOW)
    status = summary.set_index("vehicle_id")["status"].to_dict()
    assert status == {"A": "Reportando", "B": "Reportando", "C": "Sin señal", "D": "Sin señal"}
    assert summary["vehicle_id"].iloc[0] == "D"  # most silent first


def test_vehicle_without_reports_in_the_period_still_appears():
    summary = device_summary(points([("A", 0.5)]), seen(A=0.1, B=45), NOW).set_index("vehicle_id")
    assert summary.loc["B", "reports"] == 0
    assert summary.loc["B", "status"] == "Sin señal"
    assert pd.isna(summary.loc["B", "median_delay_s"])


def test_no_vehicles_gives_an_empty_summary():
    assert device_summary(points([]), seen(), NOW).empty
