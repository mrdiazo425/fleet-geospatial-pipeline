"""SQL access for the Bronze layer. Keeps SQL out of the HTTP handlers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

from fleet_pipeline.privacy import Role
from fleet_pipeline.schemas import TelemetryPacket

_INSERT = """
    INSERT INTO bronze.telemetry_raw
        (vehicle_id, reported_at, latitude, longitude, speed_kmh, fuel_level_pct, is_backfill)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (vehicle_id, reported_at) DO NOTHING
"""

# Admins read the raw table; everyone else reads the masked view, so the
# unmasked id never leaves the database for a non-admin request.
_SOURCE_BY_ROLE = {
    Role.ADMIN: "bronze.telemetry_raw",
    Role.ANALYST: "bronze.telemetry_masked",
}

_SELECT = """
    SELECT vehicle_id, reported_at AS timestamp, latitude, longitude,
           speed_kmh, fuel_level_pct
    FROM {source}
    WHERE (%(vehicle_id)s::text IS NULL OR vehicle_id = %(vehicle_id)s)
    ORDER BY reported_at DESC
    LIMIT %(limit)s
"""


def insert_packets(
    conn: psycopg.Connection, packets: Sequence[TelemetryPacket], *, backfill: bool = False
) -> int:
    """Insert packets, ignoring retransmissions. Returns rows actually stored.

    ``backfill`` flags a historical replay, so its age is not read as a
    transmission delay.
    """
    rows = [
        (
            p.vehicle_id,
            p.timestamp,
            p.latitude,
            p.longitude,
            p.speed_kmh,
            p.fuel_level_pct,
            backfill,
        )
        for p in packets
    ]
    with conn.cursor() as cur:
        cur.executemany(_INSERT, rows)
        return cur.rowcount


def fetch_latest(
    conn: psycopg.Connection, role: Role, vehicle_id: str | None, limit: int
) -> list[dict[str, Any]]:
    """Latest reports, masked or not depending on ``role``."""
    query = _SELECT.format(source=_SOURCE_BY_ROLE[role])
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"vehicle_id": vehicle_id, "limit": limit})
        return cur.fetchall()
