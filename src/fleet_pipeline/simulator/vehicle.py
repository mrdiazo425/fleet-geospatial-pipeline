"""Kinematics and fuel model of one simulated vehicle.

Pure logic: no I/O, deterministic for a given random generator, so it can be
unit tested. Distances are in metres along the assigned route (EPSG:9377).
"""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from shapely.geometry import LineString

KMH_PER_MS = 3.6
GPS_NOISE_M = 3.0
STOP_PROBABILITY = 0.05  # chance per step of meeting a red light / traffic jam
HIGH_SPEED_KMH = 60.0  # above this, drag makes consumption grow
IDLE_BURN_L_PER_H = 0.8
MAX_SIGMAS_ABOVE_TYPICAL = 2.5  # keeps top speeds plausible
DEFAULT_LIMIT_KMH = 50.0
PACE_CHANGE_PROBABILITY = 0.08  # per step: a pace lasts about one minute
PACE_JITTER = 0.02  # small speed wobble around the chosen pace


@dataclass(frozen=True)
class DriverProfile:
    """How a driver behaves; drives the variety needed for the Driver Score.

    ``compliance`` is the typical speed as a fraction of the posted limit:
    below 1 the driver stays under the limit, above 1 the driver speeds.
    """

    name: str
    compliance: float
    compliance_sigma: float
    max_accel_ms2: float


PROFILES = (
    DriverProfile("normal", compliance=0.93, compliance_sigma=0.08, max_accel_ms2=1.8),
    DriverProfile("cautious", compliance=0.80, compliance_sigma=0.06, max_accel_ms2=1.2),
    DriverProfile("aggressive", compliance=1.05, compliance_sigma=0.12, max_accel_ms2=3.6),
)


@dataclass(frozen=True)
class SpeedLimitProfile:
    """Posted speed limit along a route, sampled at increasing distances."""

    distances_m: tuple[float, ...] = (0.0,)
    limits_kmh: tuple[float, ...] = (DEFAULT_LIMIT_KMH,)

    def limit_at(self, position_m: float) -> float:
        index = bisect.bisect_right(self.distances_m, position_m) - 1
        return self.limits_kmh[max(index, 0)]


@dataclass(frozen=True)
class Reading:
    """Raw device reading before it is turned into an API packet."""

    vehicle_id: str
    timestamp: datetime
    x_m: float
    y_m: float
    speed_kmh: float
    fuel_level_pct: float


@dataclass
class VehicleSimulator:
    vehicle_id: str
    route: LineString
    tank_capacity_l: float
    efficiency_km_l: float
    profile: DriverProfile
    rng: random.Random
    limits: SpeedLimitProfile = field(default_factory=SpeedLimitProfile)
    fuel_pct: float = 80.0
    refuel_range_pct: tuple[float, float] = (70.0, 100.0)
    position_m: float = 0.0
    speed_kmh: float = 0.0
    parked_until: datetime | None = None
    _stop_steps: int = field(default=0, repr=False)
    _pace: float | None = field(default=None, repr=False)

    def fuel_used_pct(self, distance_m: float, speed_kmh: float, dt_s: float) -> float:
        """Fuel burnt in one step, as a percentage of the tank."""
        drag = 1.0 + max(0.0, speed_kmh - HIGH_SPEED_KMH) * 0.006
        moving_l = (distance_m / 1000.0) / self.efficiency_km_l * drag
        idle_l = IDLE_BURN_L_PER_H * dt_s / 3600.0 if speed_kmh < 1.0 else 0.0
        return (moving_l + idle_l) / self.tank_capacity_l * 100.0

    def _target_speed(self) -> float:
        if self._stop_steps > 0:
            self._stop_steps -= 1
            return 0.0
        if self.rng.random() < STOP_PROBABILITY:
            self._stop_steps = self.rng.randint(1, 4)
            return 0.0
        profile = self.profile
        # Drivers hold a pace for a while instead of changing it at every report.
        if self._pace is None or self.rng.random() < PACE_CHANGE_PROBABILITY:
            ceiling = profile.compliance + MAX_SIGMAS_ABOVE_TYPICAL * profile.compliance_sigma
            pace = self.rng.gauss(profile.compliance, profile.compliance_sigma)
            self._pace = min(max(0.0, pace), ceiling)
        jitter = self.rng.gauss(1.0, PACE_JITTER)
        return self.limits.limit_at(self.position_m) * self._pace * jitter

    def _park(self, now: datetime) -> None:
        """Ignition off at the destination (or out of fuel): stop reporting."""
        self.speed_kmh = 0.0
        self.parked_until = now + timedelta(minutes=self.rng.uniform(6, 10))

    def _start_new_trip(self) -> None:
        self.position_m = 0.0
        self.fuel_pct = self.rng.uniform(*self.refuel_range_pct)
        self.parked_until = None

    def step(self, now: datetime, dt_s: float) -> Reading | None:
        """Advance ``dt_s`` seconds. Returns None while the ignition is off."""
        if self.parked_until is not None:
            if now < self.parked_until:
                return None
            self._start_new_trip()

        max_delta = self.profile.max_accel_ms2 * dt_s * KMH_PER_MS
        target = self._target_speed()
        previous = self.speed_kmh
        self.speed_kmh = previous + max(-max_delta, min(max_delta, target - previous))

        distance_m = (previous + self.speed_kmh) / 2.0 / KMH_PER_MS * dt_s
        distance_m = min(distance_m, self.route.length - self.position_m)
        self.position_m += distance_m
        self.fuel_pct = max(
            0.0, self.fuel_pct - self.fuel_used_pct(distance_m, self.speed_kmh, dt_s)
        )

        if self.position_m >= self.route.length or self.fuel_pct <= 0.0:
            self._park(now)

        point = self.route.interpolate(self.position_m)
        return Reading(
            vehicle_id=self.vehicle_id,
            timestamp=now,
            x_m=point.x + self.rng.gauss(0, GPS_NOISE_M),
            y_m=point.y + self.rng.gauss(0, GPS_NOISE_M),
            speed_kmh=round(self.speed_kmh, 1),
            fuel_level_pct=round(self.fuel_pct, 3),
        )
