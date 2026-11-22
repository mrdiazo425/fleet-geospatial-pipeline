"""Turn per-report alerts into events a person would recognise.

The Silver layer raises one speeding alert per GPS report (every few seconds),
which is right for a heatmap but inflates counts: a driver who speeds for one
minute did not commit twelve infractions. Consecutive alerts of the same type
and vehicle are therefore merged into a single event.
"""

from __future__ import annotations

import pandas as pd

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "severe": 3}
_RANK_SEVERITY = {rank: severity for severity, rank in SEVERITY_RANK.items()}
EVENT_COLUMNS = [
    "vehicle_id", "reported_at", "alert_type", "severity", "geofence_code",
    "observed", "threshold", "latitude", "longitude", "reports", "duration_s",
    "ingested_at", "arrival_delay_s",
]  # fmt: skip


def alert_events(
    alerts: pd.DataFrame, max_gap_s: float = 30, report_interval_s: float = 5
) -> pd.DataFrame:
    """One row per event.

    An event starts at its first alert and keeps: the worst severity reached,
    the highest observed value, the first critical zone it touched, how many
    reports it spans, how long it lasted and when its first report arrived.
    """
    if alerts.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    ordered = alerts.sort_values(["vehicle_id", "alert_type", "reported_at"])
    gap = ordered.groupby(["vehicle_id", "alert_type"])["reported_at"].diff().dt.total_seconds()
    ordered = ordered.assign(
        event_id=(gap.isna() | (gap > max_gap_s)).cumsum(),
        rank=ordered["severity"].map(SEVERITY_RANK),
    )
    events = ordered.groupby("event_id", sort=False).agg(
        vehicle_id=("vehicle_id", "first"),
        reported_at=("reported_at", "first"),
        ended_at=("reported_at", "last"),
        alert_type=("alert_type", "first"),
        rank=("rank", "max"),
        geofence_code=("geofence_code", "first"),  # first non-null zone touched
        observed=("observed", "max"),
        threshold=("threshold", "min"),
        latitude=("latitude", "first"),
        longitude=("longitude", "first"),
        reports=("alert_type", "size"),
        ingested_at=("ingested_at", "first"),  # arrival of the report that opened it
        arrival_delay_s=("arrival_delay_s", lambda delays: delays.iloc[0]),
    )
    lasted = (events["ended_at"] - events["reported_at"]).dt.total_seconds()
    events = events.assign(
        severity=events["rank"].map(_RANK_SEVERITY), duration_s=lasted + report_interval_s
    )
    return events.sort_values("reported_at")[EVENT_COLUMNS].reset_index(drop=True)
