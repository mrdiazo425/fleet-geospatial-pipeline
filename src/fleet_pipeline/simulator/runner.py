"""Run the fleet simulation and push packets to the ingestion API.

1. Backfill: replays the last ``SIM_BACKFILL_MINUTES`` so the pipeline and the
   dashboard have data immediately.
2. Live: one report per vehicle every ``SIM_INTERVAL_S`` seconds.

Usage: ``python -m fleet_pipeline.simulator.runner``
"""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import shapely
from pyproj import Transformer

from fleet_pipeline.config import GPS_SRID, METRIC_SRID, Settings
from fleet_pipeline.db import connect
from fleet_pipeline.simulator.connectivity import Connectivity
from fleet_pipeline.simulator.vehicle import (
    PROFILES,
    Reading,
    SpeedLimitProfile,
    VehicleSimulator,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000
#: Vehicles (by position in the fleet) that are always poorly fuelled, so
#: the predictive fuel alert has real cases to detect.
LOW_FUEL_EVERY = 5
LOW_FUEL_ROUTE_COVERAGE = 0.7

_TO_GPS = Transformer.from_crs(METRIC_SRID, GPS_SRID, always_xy=True)

_FLEET_QUERY = """
    SELECT v.vehicle_id, v.tank_capacity_l, v.efficiency_km_l, ST_AsBinary(r.geom)
    FROM reference.vehicles AS v
    JOIN reference.vehicle_routes AS r USING (vehicle_id)
    ORDER BY v.vehicle_id
"""

# Posted limit every 50 m of each route, using the same rule as the Silver
# layer, so simulated drivers react to roads and geofences like real ones.
_LIMITS_QUERY = """
    SELECT r.vehicle_id, s.distance_m, l.speed_limit_kmh
    FROM reference.vehicle_routes AS r
    CROSS JOIN LATERAL generate_series(0, floor(r.route_length_m)::int, 50) AS s(distance_m)
    CROSS JOIN LATERAL silver.speed_limit_at(
        ST_LineInterpolatePoint(r.geom, s.distance_m / r.route_length_m)) AS l
    ORDER BY r.vehicle_id, s.distance_m
"""


def _limit_profiles(rows: list[tuple[str, int, int]]) -> dict[str, SpeedLimitProfile]:
    samples: dict[str, tuple[list[float], list[float]]] = {}
    for vehicle_id, distance_m, limit_kmh in rows:
        distances, limits = samples.setdefault(vehicle_id, ([], []))
        distances.append(float(distance_m))
        limits.append(float(limit_kmh))
    return {
        vehicle_id: SpeedLimitProfile(tuple(distances), tuple(limits))
        for vehicle_id, (distances, limits) in samples.items()
    }


def load_fleet(settings: Settings) -> tuple[list[VehicleSimulator], datetime | None]:
    """Create one simulator per vehicle from the fleet master data."""
    with connect(settings) as conn:
        rows = conn.execute(_FLEET_QUERY).fetchall()
        limits = _limit_profiles(conn.execute(_LIMITS_QUERY).fetchall())
        latest = conn.execute("SELECT max(reported_at) FROM bronze.telemetry_raw").fetchone()[0]
    if not rows:
        raise RuntimeError("No vehicles found; run the bootstrap job first")

    fleet: list[VehicleSimulator] = []
    for index, (vehicle_id, tank_l, km_per_l, wkb) in enumerate(rows):
        rng = random.Random(f"{settings.sim_seed}-{vehicle_id}")
        route = shapely.from_wkb(bytes(wkb))
        vehicle = VehicleSimulator(
            vehicle_id=vehicle_id,
            route=route,
            tank_capacity_l=float(tank_l),
            efficiency_km_l=float(km_per_l),
            profile=PROFILES[index % len(PROFILES)],
            rng=rng,
            limits=limits.get(vehicle_id, SpeedLimitProfile()),
            fuel_pct=rng.uniform(35, 95),
            position_m=rng.uniform(0, 0.3) * route.length,
        )
        if index % LOW_FUEL_EVERY == LOW_FUEL_EVERY - 1:
            # Poorly fuelled vehicle: sometimes enough for the route, sometimes not.
            route_fuel_pct = vehicle.fuel_used_pct(route.length, 40.0, 0.0)
            vehicle.position_m = 0.0
            vehicle.fuel_pct = route_fuel_pct * LOW_FUEL_ROUTE_COVERAGE
            vehicle.refuel_range_pct = (route_fuel_pct * 0.6, route_fuel_pct * 1.6)
        fleet.append(vehicle)
    logger.info("Fleet ready: %d vehicles (latest stored report: %s)", len(fleet), latest)
    return fleet, latest


def to_packet(reading: Reading) -> dict[str, object]:
    """Device payload: project metres back to GPS coordinates (WGS84)."""
    longitude, latitude = _TO_GPS.transform(reading.x_m, reading.y_m)
    return {
        "vehicle_id": reading.vehicle_id,
        "timestamp": reading.timestamp.isoformat(),
        "latitude": round(latitude, 6),
        "longitude": round(longitude, 6),
        "speed_kmh": reading.speed_kmh,
        "fuel_level_pct": reading.fuel_level_pct,
    }


def tick(fleet: list[VehicleSimulator], now: datetime, dt_s: float) -> list[dict[str, object]]:
    readings = (vehicle.step(now, dt_s) for vehicle in fleet)
    return [to_packet(reading) for reading in readings if reading is not None]


def live_tick(
    fleet: list[VehicleSimulator], links: list[Connectivity], now: datetime, dt_s: float
) -> list[dict[str, object]]:
    """One live step: each device reports through its own (imperfect) link."""
    packets: list[dict[str, object]] = []
    for vehicle, link in zip(fleet, links, strict=True):
        reading = vehicle.step(now, dt_s)
        packets.extend(link.push(to_packet(reading) if reading is not None else None))
    return packets


def send(
    client: httpx.Client,
    packets: list[dict[str, object]],
    *,
    backfill: bool = False,
    retries: int = 5,
) -> None:
    """POST a batch, retrying on transient errors. A lost batch is logged."""
    if not packets:
        return
    for attempt in range(1, retries + 1):
        try:
            params = {"backfill": "true"} if backfill else None
            client.post("/telemetry", json=packets, params=params).raise_for_status()
            return
        except httpx.HTTPError as error:
            logger.warning("Send failed (attempt %d/%d): %s", attempt, retries, error)
            time.sleep(min(2**attempt, 15))
    logger.error("Dropped a batch of %d packets after %d attempts", len(packets), retries)


def wait_for_api(client: httpx.Client, timeout_s: float = 180) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise ConnectionError("Ingestion API did not become healthy in time")


def backfill(
    client: httpx.Client,
    fleet: list[VehicleSimulator],
    start: datetime,
    end: datetime,
    dt_s: float,
) -> int:
    """Replay history between ``start`` and ``end`` as fast as possible."""
    sent, buffer, now = 0, [], start
    while now <= end:
        buffer.extend(tick(fleet, now, dt_s))
        if len(buffer) >= BATCH_SIZE:
            send(client, buffer, backfill=True)
            sent, buffer = sent + len(buffer), []
        now += timedelta(seconds=dt_s)
    send(client, buffer, backfill=True)
    return sent + len(buffer)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    dt_s = settings.sim_interval_s

    try:
        fleet, latest = load_fleet(settings)
        with httpx.Client(base_url=settings.api_url, timeout=30) as client:
            wait_for_api(client)

            end = datetime.now(UTC).replace(microsecond=0)
            start = end - timedelta(minutes=settings.sim_backfill_minutes)
            if latest is not None:  # restart: only fill the gap, never duplicate
                start = max(start, latest + timedelta(seconds=dt_s))
            sent = backfill(client, fleet, start, end, dt_s)
            logger.info("Backfill done: %d packets from %s to %s", sent, start, end)

            logger.info("Live mode: one report per vehicle every %.0f s", dt_s)
            links = [
                Connectivity(random.Random(f"{settings.sim_seed}-link-{vehicle.vehicle_id}"))
                for vehicle in fleet
            ]
            while True:
                time.sleep(dt_s)
                now = datetime.now(UTC).replace(microsecond=0)
                send(client, live_tick(fleet, links, now, dt_s))
    except KeyboardInterrupt:
        logger.info("Simulator stopped")
        return 0
    except Exception:
        logger.exception("Simulator failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
