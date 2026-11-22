from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fleet_pipeline.schemas import TelemetryPacket

VALID = {
    "vehicle_id": "VHC-8472-ABC",
    "timestamp": "2026-11-01T08:00:00-05:00",
    "latitude": 4.711,
    "longitude": -74.0721,
    "speed_kmh": 42.5,
    "fuel_level_pct": 63.2,
}


def test_valid_packet_is_accepted():
    packet = TelemetryPacket(**VALID)
    assert packet.vehicle_id == "VHC-8472-ABC"
    assert packet.timestamp.utcoffset() == timedelta(hours=-5)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vehicle_id", "ABC-8472"),
        ("vehicle_id", "VHC-847-ABC"),
        ("latitude", 91),
        ("longitude", -181),
        ("speed_kmh", -1),
        ("speed_kmh", 400),
        ("fuel_level_pct", 101),
        ("fuel_level_pct", -0.1),
        ("timestamp", "2026-11-01T08:00:00"),  # no UTC offset: ambiguous
        ("timestamp", "not-a-date"),
    ],
)
def test_invalid_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        TelemetryPacket(**{**VALID, field: value})


def test_future_timestamps_are_rejected():
    future = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(ValidationError):
        TelemetryPacket(**{**VALID, "timestamp": future})


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        TelemetryPacket(**VALID, driver_name="someone")


@pytest.mark.parametrize("missing", list(VALID))
def test_every_field_is_required(missing):
    payload = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationError):
        TelemetryPacket(**payload)
