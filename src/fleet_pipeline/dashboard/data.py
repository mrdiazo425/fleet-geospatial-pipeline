"""Read-only queries for the dashboard.

The role decides *which database user* runs the query: admins read the Gold
tables, everyone else connects as ``fleet_analyst`` and can only see the
masked views. Masking is therefore enforced by PostgreSQL, not by the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import psycopg

from fleet_pipeline.config import Settings
from fleet_pipeline.privacy import Role


@dataclass(frozen=True)
class Sources:
    conninfo: str
    fact: str
    alerts: str


def sources_for(role: Role, settings: Settings) -> Sources:
    if role is Role.ADMIN:
        return Sources(settings.conninfo, "gold.fact_telemetry", "gold.alerts")
    return Sources(settings.analyst_conninfo, "gold.v_fact_telemetry", "gold.v_alerts")


@dataclass(frozen=True)
class Filters:
    """User selection. ``None`` means "no restriction"."""

    since: datetime
    vehicles: tuple[str, ...] | None = None
    zones: tuple[str, ...] | None = None

    def params(self) -> dict[str, object]:
        return {
            "since": self.since,
            "vehicles": list(self.vehicles) if self.vehicles else None,
            "zones": list(self.zones) if self.zones else None,
        }


_FILTER = """
    reported_at >= %(since)s
    AND (%(vehicles)s::text[] IS NULL OR vehicle_id = ANY(%(vehicles)s::text[]))
    AND (%(zones)s::text[] IS NULL OR geofence_code = ANY(%(zones)s::text[]))
"""

_POINTS = """
    SELECT vehicle_id, reported_at, latitude, longitude, speed_kmh, speed_limit_kmh,
           is_speeding, in_high_accident_zone, geofence_code, step_seconds,
           fuel_level_pct, fuel_autonomy_km, route_remaining_km, fuel_insufficient,
           ingested_at, arrival_delay_s
    FROM {fact}
    WHERE {filter}
    ORDER BY vehicle_id, reported_at
"""

_ALERTS = """
    SELECT vehicle_id, reported_at, alert_type, severity, geofence_code,
           observed, threshold, latitude, longitude, ingested_at, arrival_delay_s
    FROM {alerts}
    WHERE {filter}
    ORDER BY reported_at
"""

# Same formula as gold.driver_score_daily, evaluated on the filtered window.
_DRIVER_KPIS = """
    WITH driving AS (
        SELECT vehicle_id,
               COALESCE(sum(step_distance_m), 0) / 1000.0 AS km,
               COALESCE(sum(step_seconds) FILTER (WHERE speed_kmh >= 1), 0) AS driving_s,
               COALESCE(sum(step_seconds) FILTER (WHERE is_speeding), 0) AS speeding_s,
               COALESCE(sum(step_seconds * gold.speeding_weight(
                   speeding_severity, in_high_accident_zone)), 0) AS weighted_s,
               COALESCE(sum(step_seconds) FILTER (WHERE in_high_accident_zone), 0) AS zone_s,
               count(*) FILTER (WHERE harsh_event IS NOT NULL) AS harsh,
               max(speed_kmh) AS max_speed
        FROM {fact}
        WHERE {filter}
        GROUP BY vehicle_id
    ),
    fuel AS (
        SELECT vehicle_id, count(*)::integer AS fuel_alerts
        FROM {alerts}
        WHERE alert_type = 'fuel_insufficient' AND {filter}
        GROUP BY vehicle_id
    )
    SELECT d.vehicle_id,
           round(d.km::numeric, 1)::float AS distance_km,
           round((d.driving_s / 60.0)::numeric, 0)::float AS driving_minutes,
           round(d.max_speed::numeric, 0)::float AS max_speed_kmh,
           COALESCE(round((100.0 * d.speeding_s / NULLIF(d.driving_s, 0))::numeric, 1), 0)::float
               AS speeding_time_pct,
           round((d.zone_s / 60.0)::numeric, 1)::float AS zone_minutes,
           d.harsh AS harsh_events,
           COALESCE(f.fuel_alerts, 0) AS fuel_alerts,
           gold.driver_score(
               d.weighted_s / NULLIF(d.driving_s, 0),
               100.0 * d.harsh / NULLIF(d.km, 0),
               COALESCE(f.fuel_alerts, 0))::float AS driver_score
    FROM driving AS d
    LEFT JOIN fuel AS f USING (vehicle_id)
    ORDER BY driver_score, d.vehicle_id
"""

# Last report of every vehicle, whatever the selected period: a silent device
# must be visible precisely when it has sent nothing lately.
_LAST_SEEN = """
    SELECT vehicle_id, max(reported_at) AS last_report
    FROM {fact}
    WHERE (%(vehicles)s::text[] IS NULL OR vehicle_id = ANY(%(vehicles)s::text[]))
    GROUP BY vehicle_id
"""

_GEOFENCES = """
    SELECT zone_code, name, geofence_type, locality, speed_limit_kmh, fatalities, injuries,
           ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(geom, 5), 4326), 6) AS geojson
    FROM reference.geofences
    ORDER BY geofence_type DESC, zone_code
"""

_FACILITIES = """
    SELECT facility_type, name,
           ST_Y(ST_Transform(geom, 4326)) AS latitude,
           ST_X(ST_Transform(geom, 4326)) AS longitude
    FROM reference.facilities
"""


def _frame(conninfo: str, query: str, params: dict[str, object] | None = None) -> pd.DataFrame:
    with psycopg.connect(conninfo, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [column.name for column in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


def load_points(sources: Sources, filters: Filters) -> pd.DataFrame:
    query = _POINTS.format(fact=sources.fact, filter=_FILTER)
    return _frame(sources.conninfo, query, filters.params())


def load_alerts(sources: Sources, filters: Filters) -> pd.DataFrame:
    query = _ALERTS.format(alerts=sources.alerts, filter=_FILTER)
    return _frame(sources.conninfo, query, filters.params())


def load_driver_kpis(sources: Sources, filters: Filters) -> pd.DataFrame:
    query = _DRIVER_KPIS.format(fact=sources.fact, alerts=sources.alerts, filter=_FILTER)
    return _frame(sources.conninfo, query, filters.params())


def load_last_seen(sources: Sources, filters: Filters) -> pd.DataFrame:
    query = _LAST_SEEN.format(fact=sources.fact)
    return _frame(sources.conninfo, query, filters.params())


def load_vehicle_ids(sources: Sources) -> list[str]:
    query = f"SELECT DISTINCT vehicle_id FROM {sources.fact} ORDER BY 1"
    return _frame(sources.conninfo, query)["vehicle_id"].tolist()


def load_geofences(sources: Sources) -> pd.DataFrame:
    return _frame(sources.conninfo, _GEOFENCES)


def load_facilities(sources: Sources) -> pd.DataFrame:
    return _frame(sources.conninfo, _FACILITIES)
