"""Database connection helpers."""

from __future__ import annotations

import logging
import time

import psycopg

from fleet_pipeline.config import Settings

logger = logging.getLogger(__name__)


def connect(
    settings: Settings | None = None,
    *,
    retries: int = 30,
    delay_seconds: float = 2.0,
) -> psycopg.Connection:
    """Open a connection, retrying while the database is still starting.

    Containers start in parallel, so the database may not accept connections
    yet when a service boots. Retrying here keeps every service self-healing.
    """
    settings = settings or Settings.from_env()
    last_error: psycopg.OperationalError | None = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg.connect(settings.conninfo)
        except psycopg.OperationalError as error:
            last_error = error
            logger.warning(
                "Database not ready (attempt %d/%d): %s",
                attempt,
                retries,
                str(error).strip(),
            )
            time.sleep(delay_seconds)
    raise ConnectionError(
        f"Could not connect to the database after {retries} attempts"
    ) from last_error
