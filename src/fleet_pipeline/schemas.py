"""Data contract of a telemetry packet sent by an IoT/AVL device."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from fleet_pipeline.privacy import VEHICLE_ID_PATTERN

#: Tolerance for device clocks running slightly ahead of the server.
MAX_CLOCK_SKEW = timedelta(minutes=5)


class TelemetryPacket(BaseModel):
    """One GPS report. Invalid packets are rejected before reaching Bronze."""

    model_config = ConfigDict(extra="forbid")

    vehicle_id: str = Field(pattern=VEHICLE_ID_PATTERN, examples=["VHC-8472-ABC"])
    timestamp: AwareDatetime = Field(description="Report time with UTC offset")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=250)
    fuel_level_pct: float = Field(ge=0, le=100)

    @field_validator("timestamp")
    @classmethod
    def _not_in_the_future(cls, value: datetime) -> datetime:
        if value > datetime.now(UTC) + MAX_CLOCK_SKEW:
            raise ValueError("timestamp is in the future")
        return value


class IngestResult(BaseModel):
    received: int
    inserted: int
    duplicates: int
