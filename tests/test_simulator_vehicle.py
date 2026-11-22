import random
from datetime import UTC, datetime, timedelta

import pytest
from shapely.geometry import LineString

from fleet_pipeline.simulator.vehicle import PROFILES, SpeedLimitProfile, VehicleSimulator

START = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)
DT_S = 5.0


def make_vehicle(length_m=5_000.0, fuel_pct=80.0, profile=PROFILES[0], limits=None):
    return VehicleSimulator(
        vehicle_id="VHC-8472-ABC",
        route=LineString([(0, 0), (length_m, 0)]),
        tank_capacity_l=60,
        efficiency_km_l=10,
        profile=profile,
        rng=random.Random(1),
        limits=limits or SpeedLimitProfile(),
        fuel_pct=fuel_pct,
    )


def run(vehicle, steps):
    readings = []
    for i in range(steps):
        reading = vehicle.step(START + timedelta(seconds=i * DT_S), DT_S)
        if reading is not None:
            readings.append(reading)
    return readings


def run_until_parked(vehicle, max_steps=5_000):
    """Step until the ignition goes off; return the readings of that trip."""
    readings = []
    for i in range(max_steps):
        readings.append(vehicle.step(START + timedelta(seconds=i * DT_S), DT_S))
        if vehicle.parked_until is not None:
            return readings
    raise AssertionError("vehicle never parked")


def test_fuel_used_matches_tank_and_efficiency():
    vehicle = make_vehicle()
    # 10 km at 10 km/l = 1 l = 1/60 of the tank
    assert vehicle.fuel_used_pct(10_000, speed_kmh=40, dt_s=0) == pytest.approx(100 / 60)


def test_high_speed_burns_more_fuel():
    vehicle = make_vehicle()
    assert vehicle.fuel_used_pct(1_000, 90, 0) > vehicle.fuel_used_pct(1_000, 50, 0)


def test_vehicle_stays_on_route_and_values_stay_in_range():
    readings = run(make_vehicle(), 200)
    assert readings
    for reading in readings:
        assert -20 <= reading.x_m <= 5_020
        assert abs(reading.y_m) <= 20  # only GPS noise away from the line
        assert reading.speed_kmh >= 0
        assert 0 <= reading.fuel_level_pct <= 100


def test_fuel_never_increases_during_a_trip():
    vehicle = make_vehicle(length_m=1_000_000)
    levels = [r.fuel_level_pct for r in run(vehicle, 300)]
    assert all(later <= earlier for earlier, later in zip(levels, levels[1:], strict=False))


def test_acceleration_respects_the_driver_profile():
    profile = PROFILES[0]
    speeds = [r.speed_kmh for r in run(make_vehicle(length_m=1_000_000, profile=profile), 300)]
    max_delta = profile.max_accel_ms2 * DT_S * 3.6 + 0.1
    assert all(abs(b - a) <= max_delta for a, b in zip(speeds, speeds[1:], strict=False))


def test_vehicle_parks_at_destination_and_goes_silent():
    vehicle = make_vehicle(length_m=300)
    last = run_until_parked(vehicle)[-1]
    assert last.x_m == pytest.approx(300, abs=20)
    assert last.speed_kmh == 0
    assert vehicle.step(vehicle.parked_until - timedelta(seconds=1), DT_S) is None


def test_new_trip_starts_refuelled_after_parking():
    vehicle = make_vehicle(length_m=300, fuel_pct=5)
    run_until_parked(vehicle)
    reading = vehicle.step(vehicle.parked_until + timedelta(seconds=1), DT_S)
    assert reading is not None
    assert reading.fuel_level_pct >= 69
    assert vehicle.position_m < 300


def test_vehicle_stops_when_it_runs_out_of_fuel():
    vehicle = make_vehicle(length_m=1_000_000, fuel_pct=0.05)
    last = run_until_parked(vehicle)[-1]
    assert last.fuel_level_pct == 0
    assert last.x_m < 1_000_000


def test_same_seed_gives_the_same_trip():
    first = run(make_vehicle(), 50)
    second = run(make_vehicle(), 50)
    assert first == second


def test_speed_limit_profile_returns_the_limit_of_the_current_stretch():
    limits = SpeedLimitProfile((0.0, 100.0, 250.0), (50.0, 30.0, 60.0))
    assert [limits.limit_at(d) for d in (0, 99.9, 100, 249, 250, 9_999)] == [50, 50, 30, 30, 60, 60]


def mean_cruising_speed(vehicle, steps=600):
    speeds = [r.speed_kmh for r in run(vehicle, steps)]
    return sum(speeds) / len(speeds)


def test_drivers_slow_down_where_the_limit_is_lower():
    fast = make_vehicle(length_m=1_000_000, limits=SpeedLimitProfile((0.0,), (50.0,)))
    slow = make_vehicle(length_m=1_000_000, limits=SpeedLimitProfile((0.0,), (30.0,)))
    assert mean_cruising_speed(slow) < mean_cruising_speed(fast) * 0.75


def test_aggressive_profile_drives_faster_than_cautious():
    by_name = {profile.name: profile for profile in PROFILES}
    aggressive = make_vehicle(length_m=1_000_000, profile=by_name["aggressive"])
    cautious = make_vehicle(length_m=1_000_000, profile=by_name["cautious"])
    assert mean_cruising_speed(aggressive) > mean_cruising_speed(cautious) * 1.15
