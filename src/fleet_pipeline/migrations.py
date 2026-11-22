"""Apply the SQL files in ``sql/`` in lexical order.

Every file is idempotent (``IF NOT EXISTS`` / ``CREATE OR REPLACE``), so the
whole set is simply re-applied on each start. That keeps the schema versioned
as plain SQL without needing a migration framework.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)


def apply_sql_files(conn: psycopg.Connection, sql_dir: Path) -> list[str]:
    """Execute every ``*.sql`` file in ``sql_dir``; return the applied names."""
    files = sorted(sql_dir.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"No SQL files found in {sql_dir}")

    applied: list[str] = []
    for path in files:
        logger.info("Applying %s", path.name)
        try:
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
        except psycopg.Error as error:
            raise RuntimeError(f"Failed applying {path.name}: {error}") from error
        applied.append(path.name)
    return applied
