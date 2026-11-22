"""Device health: is each AVL unit reporting, and how late do reports arrive?

Two different questions:

* arrival delay  -> time between the device taking a reading and the platform
  receiving it (coverage problems, buffering);
* silence        -> time since the last report (unit off, unplugged, tampered).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

ON_TIME_S = 60  # at or under: normal transmission
SILENT_AFTER_MIN = 15  # longer than any normal stop between trips

ON_TIME, LATE = "A tiempo", "Tardío"
DEVICE_COLUMNS = [
    "vehicle_id", "status", "last_report", "minutes_silent",
    "median_delay_s", "max_delay_s", "late_pct", "reports",
]  # fmt: skip


def classify_delay(delay_s: pd.Series) -> pd.Series:
    """Label each transmitted report by how long it took to arrive."""
    return delay_s.map(lambda delay: LATE if delay > ON_TIME_S else ON_TIME)


def transmitted(points: pd.DataFrame) -> pd.DataFrame:
    """Reports sent live by the device.

    Historical replays carry no delay (NULL in Gold) and are left out: their
    age says nothing about how the device is transmitting.
    """
    sent = points[points["arrival_delay_s"].notna()]
    return sent.assign(arrival=classify_delay(sent["arrival_delay_s"]))


def device_summary(points: pd.DataFrame, last_seen: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """One row per vehicle: connection status and transmission quality.

    ``last_seen`` (vehicle_id, last_report) covers every known vehicle, so a
    unit that sent nothing in the selected period still shows up as silent.
    """
    if last_seen.empty:
        return pd.DataFrame(columns=DEVICE_COLUMNS)

    sent = transmitted(points)
    stats = sent.groupby("vehicle_id").agg(
        median_delay_s=("arrival_delay_s", "median"),
        max_delay_s=("arrival_delay_s", "max"),
        late_pct=("arrival", lambda labels: 100.0 * (labels == LATE).mean()),
        reports=("arrival", "size"),
    )
    summary = last_seen.merge(stats, on="vehicle_id", how="left")
    silent = (pd.Timestamp(now) - summary["last_report"]).dt.total_seconds() / 60
    summary = summary.assign(
        minutes_silent=silent.clip(lower=0).round(1),
        status=silent.map(lambda m: "Sin señal" if m > SILENT_AFTER_MIN else "Reportando"),
        reports=summary["reports"].fillna(0).astype(int),
    )
    return summary.sort_values(["minutes_silent"], ascending=False)[DEVICE_COLUMNS].reset_index(
        drop=True
    )
