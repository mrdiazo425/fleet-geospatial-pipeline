"""One-shot initialisation: create the schema and load reference layers.

Usage: ``python -m fleet_pipeline.bootstrap [--force]``
"""

from __future__ import annotations

import argparse
import logging
import sys

from fleet_pipeline.config import Settings
from fleet_pipeline.db import connect
from fleet_pipeline.migrations import apply_sql_files
from fleet_pipeline.reference_loader import load_reference_data

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="reload reference layers even if present"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    try:
        with connect(settings) as conn:
            apply_sql_files(conn, settings.sql_dir)
            conn.commit()
            load_reference_data(conn, settings, force=args.force)
    except Exception:
        logger.exception("Bootstrap failed")
        return 1
    logger.info("Bootstrap finished OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
