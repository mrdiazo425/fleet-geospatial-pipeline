"""FastAPI application: receives telemetry and serves it with masking."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from psycopg_pool import ConnectionPool

from fleet_pipeline.api import repository
from fleet_pipeline.config import Settings
from fleet_pipeline.privacy import Role, resolve_role
from fleet_pipeline.schemas import IngestResult, TelemetryPacket

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 5000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    pool = ConnectionPool(settings.conninfo, min_size=1, max_size=5, open=False)
    pool.open(wait=True, timeout=120)
    app.state.settings = settings
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="Fleet Telemetry API",
    description="Bronze layer: ingestion of IoT/AVL telemetry with id masking.",
    version="1.0.0",
    lifespan=lifespan,
)


def _database_unavailable(error: Exception) -> HTTPException:
    logger.exception("Database error: %s", error)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable")


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    """Liveness probe that also checks the database connection."""
    try:
        with request.app.state.pool.connection() as conn:
            conn.execute("SELECT 1")
    except (psycopg.Error, TimeoutError) as error:
        raise _database_unavailable(error) from error
    return {"status": "ok"}


@app.post("/telemetry", status_code=status.HTTP_201_CREATED)
def ingest(
    request: Request,
    payload: TelemetryPacket | list[TelemetryPacket],
    backfill: Annotated[
        bool, Query(description="Historical replay: not counted as a late transmission")
    ] = False,
) -> IngestResult:
    """Store one packet or a batch. Retransmitted packets are ignored."""
    packets = payload if isinstance(payload, list) else [payload]
    if not packets:
        raise HTTPException(422, "Empty batch")
    if len(packets) > MAX_BATCH_SIZE:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Batch larger than {MAX_BATCH_SIZE} packets",
        )
    try:
        with request.app.state.pool.connection() as conn:
            inserted = repository.insert_packets(conn, packets, backfill=backfill)
    except (psycopg.Error, TimeoutError) as error:
        raise _database_unavailable(error) from error
    return IngestResult(
        received=len(packets), inserted=inserted, duplicates=len(packets) - inserted
    )


@app.get("/telemetry")
def latest_telemetry(
    request: Request,
    vehicle_id: Annotated[
        str | None, Query(description="Exact id (admin) or masked id (others)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    x_api_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Latest reports. Vehicle ids are masked unless the admin key is sent."""
    role: Role = resolve_role(x_api_key, request.app.state.settings.admin_api_key)
    try:
        with request.app.state.pool.connection() as conn:
            items = repository.fetch_latest(conn, role, vehicle_id, limit)
    except (psycopg.Error, TimeoutError) as error:
        raise _database_unavailable(error) from error
    return {"role": role, "count": len(items), "items": items}
