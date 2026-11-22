"""Measure what the GiST index buys on a large table of GPS points.

Creates a throw-away table with N synthetic reports spread over Bogotá and
times typical analytical queries before and after indexing.

Usage: ``python -m fleet_pipeline.benchmark [--rows 1000000]``
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg
from psycopg import sql

from fleet_pipeline.config import Settings
from fleet_pipeline.db import connect

logger = logging.getLogger(__name__)

# Rough bounding box of urban Bogotá (lon/lat).
_CREATE = """
    DROP TABLE IF EXISTS gold.benchmark_points;
    CREATE UNLOGGED TABLE gold.benchmark_points AS
    SELECT n AS id,
           now() - random() * interval '30 days' AS reported_at,
           ST_SetSRID(ST_MakePoint(-74.22 + random() * 0.22, 4.47 + random() * 0.36), 4326)
               ::geometry(Point, 4326) AS geom
    FROM generate_series(1, {rows}) AS n;
    ANALYZE gold.benchmark_points;
"""

_QUERIES = {
    "Reports inside one geofence (ST_Covers)": """
        SELECT count(*)
        FROM gold.benchmark_points AS p
        JOIN (SELECT ST_Transform(geom, 4326) AS geom
              FROM reference.geofences
              WHERE geofence_type = 'high_accident'
              ORDER BY geofence_id LIMIT 1) AS g
          ON ST_Covers(g.geom, p.geom)
    """,
    "Reports within 300 m of a point (ST_DWithin)": """
        SELECT count(*)
        FROM gold.benchmark_points AS p
        WHERE ST_DWithin(
            p.geom::geography,
            ST_SetSRID(ST_MakePoint(-74.0721, 4.7110), 4326)::geography, 300)
    """,
    "10 nearest reports to a point (KNN <->)": """
        SELECT id
        FROM gold.benchmark_points
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(-74.0721, 4.7110), 4326)
        LIMIT 10
    """,
}

_INDEXES = """
    CREATE INDEX benchmark_points_geom_gist ON gold.benchmark_points USING gist (geom);
    CREATE INDEX benchmark_points_geog_gist
        ON gold.benchmark_points USING gist ((geom::geography));
    ANALYZE gold.benchmark_points;
"""


def _execution_ms(conn: psycopg.Connection, query: str) -> float:
    """Best of three runs, as reported by EXPLAIN ANALYZE."""
    timings = []
    for _ in range(3):
        plan = conn.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {query}").fetchone()[0]
        timings.append(plan[0]["Execution Time"])
    return min(timings)


def run(conn: psycopg.Connection, rows: int) -> list[tuple[str, float, float]]:
    logger.info("Generating %s synthetic GPS reports...", f"{rows:,}")
    conn.execute(sql.SQL(_CREATE).format(rows=sql.Literal(rows)))
    before = {name: _execution_ms(conn, query) for name, query in _QUERIES.items()}
    logger.info("Building GiST indexes...")
    conn.execute(_INDEXES)
    after = {name: _execution_ms(conn, query) for name, query in _QUERIES.items()}
    conn.execute("DROP TABLE gold.benchmark_points")
    conn.commit()
    return [(name, before[name], after[name]) for name in _QUERIES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with connect(Settings.from_env()) as conn:
        results = run(conn, args.rows)

    print(f"\nBenchmark on {args.rows:,} points\n")
    print(f"{'Query':<46}{'No index':>12}{'GiST':>12}{'Speed-up':>10}")
    for name, before, after in results:
        print(f"{name:<46}{before:>9.1f} ms{after:>9.2f} ms{before / after:>9.0f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
