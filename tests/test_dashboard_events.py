from datetime import UTC, datetime, timedelta

import pandas as pd

from fleet_pipeline.dashboard.events import alert_events

T0 = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)


def alert(seconds, severity="minor", vehicle="VHC-0001-AAA", kind="speeding", zone=None, speed=55):
    return {
        "vehicle_id": vehicle,
        "reported_at": pd.Timestamp(T0 + timedelta(seconds=seconds)),
        "alert_type": kind,
        "severity": severity,
        "geofence_code": zone,
        "observed": speed,
        "threshold": 50,
        "latitude": 4.6 + seconds / 1e5,
        "longitude": -74.1,
        "ingested_at": pd.Timestamp(T0 + timedelta(seconds=seconds + 2)),
        "arrival_delay_s": 2.0,
    }


def events_of(rows):
    return alert_events(pd.DataFrame(rows))


def test_consecutive_reports_are_one_event():
    events = events_of([alert(0), alert(5), alert(10), alert(15)])
    assert len(events) == 1
    assert events.loc[0, "reports"] == 4
    assert events.loc[0, "duration_s"] == 20  # 15 s between first and last + one interval


def test_a_pause_starts_a_new_event():
    events = events_of([alert(0), alert(5), alert(60), alert(65)])  # 55 s apart
    assert events["reports"].tolist() == [2, 2]


def test_event_keeps_the_worst_severity_and_top_speed():
    events = events_of(
        [alert(0, "minor", speed=54), alert(5, "severe", speed=78), alert(10, "major", speed=66)]
    )
    assert events.loc[0, "severity"] == "severe"
    assert events.loc[0, "observed"] == 78


def test_event_is_attributed_to_the_zone_it_touched():
    events = events_of([alert(0), alert(5, zone="Z6"), alert(10, zone="Z6")])
    assert events.loc[0, "geofence_code"] == "Z6"


def test_vehicles_and_alert_types_are_never_merged():
    events = events_of(
        [
            alert(0),
            alert(5, vehicle="VHC-0002-BBB"),
            alert(5, kind="harsh_braking"),
            alert(10),
        ]
    )
    assert len(events) == 3
    assert sorted(events["reports"].tolist()) == [1, 1, 2]


def test_event_starts_where_and_when_the_first_alert_happened():
    events = events_of([alert(5), alert(0), alert(10)])  # unordered input
    assert events.loc[0, "reported_at"] == pd.Timestamp(T0)
    assert events.loc[0, "latitude"] == 4.6


def test_no_alerts_gives_an_empty_frame_with_the_same_columns():
    events = alert_events(pd.DataFrame(columns=list(alert(0))))
    assert events.empty
    assert "duration_s" in events.columns


def test_event_keeps_the_arrival_time_of_its_first_report():
    events = events_of([alert(0), alert(5), alert(10)])
    assert events.loc[0, "ingested_at"] == pd.Timestamp(T0 + timedelta(seconds=2))
