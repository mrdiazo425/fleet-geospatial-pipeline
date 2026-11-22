"""Micro-batch orchestrator: Bronze -> Silver -> Gold.

All heavy lifting is set-based SQL inside PostGIS (see ``sql/03x`` and
``sql/040``); this module only schedules it, so data never leaves the
database to be processed row by row.

Usage: ``python -m fleet_pipeline.pipeline.runner [--once]``
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

import psycopg

from fleet_pipeline.config import Settings
from fleet_pipeline.db import connect

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    processed: int = 0
    alerts: int = 0
    facts: int = 0


def run_cycle(conn: psycopg.Connection, batch_size: int) -> CycleResult:
    """Drain pending Bronze rows into Silver, then publish them to Gold.

    Each Silver batch commits on its own, so a failure never loses the
    batches already processed and the next cycle resumes from the watermark.
    """
    processed = alerts = 0
    while True:
        batch_processed, batch_alerts = conn.execute(
            "SELECT processed, alerts FROM silver.process_new_telemetry(%s)", (batch_size,)
        ).fetchone()
        conn.commit()
        if batch_processed == 0:
            break
        processed += batch_processed
        alerts += batch_alerts

    facts = conn.execute("SELECT gold.sync_fact_telemetry()").fetchone()[0]
    if facts:
        conn.execute("SELECT gold.refresh_aggregates()")
    conn.commit()
    return CycleResult(processed, alerts, facts)


def run_forever(settings: Settings) -> None:
    """Run cycles on a fixed interval, reconnecting if the database drops."""
    conn = connect(settings)
    while True:
        started = time.monotonic()
        try:
            result = run_cycle(conn, settings.pipeline_batch_size)
            if result.processed or result.facts:
                logger.info(
                    "Cycle done: %d reports enriched, %d alerts, %d rows to gold (%.2f s)",
                    result.processed,
                    result.alerts,
                    result.facts,
                    time.monotonic() - started,
                )
        except psycopg.OperationalError:
            logger.exception("Lost the database connection; reconnecting")
            conn.close()
            conn = connect(settings)
        except psycopg.Error:
            logger.exception("Cycle failed; it will be retried from the watermark")
            conn.rollback()
        time.sleep(settings.pipeline_interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    try:
        if args.once:
            with connect(settings) as conn:
                logger.info("Cycle done: %s", run_cycle(conn, settings.pipeline_batch_size))
            return 0
        run_forever(settings)
    except KeyboardInterrupt:
        logger.info("Pipeline stopped")
    except Exception:
        logger.exception("Pipeline failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
