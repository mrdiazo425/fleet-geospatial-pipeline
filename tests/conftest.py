"""Shared fixtures.

SQL tests run against a dedicated ``<db>_test`` database that contains the
schema but no reference data, so each test builds the tiny synthetic world it
needs. Every test runs inside a transaction that is rolled back afterwards.
"""

from __future__ import annotations

import dataclasses

import psycopg
import pytest
from psycopg import sql

from fleet_pipeline.config import Settings
from fleet_pipeline.migrations import apply_sql_files


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(base, db_name=f"{base.db_name}_test")


@pytest.fixture(scope="session")
def _test_database(test_settings: Settings) -> Settings:
    base = Settings.from_env()
    try:
        admin = psycopg.connect(base.conninfo, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as error:
        pytest.skip(f"PostGIS database not reachable: {error}")
    with admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (test_settings.db_name,)
        ).fetchone()
        if not exists:
            admin.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_settings.db_name))
            )
    with psycopg.connect(test_settings.conninfo) as conn:
        apply_sql_files(conn, test_settings.sql_dir)
    return test_settings


@pytest.fixture
def conn(_test_database: Settings):
    """Connection whose changes are discarded at the end of the test."""
    with psycopg.connect(_test_database.conninfo) as connection:
        yield connection
        connection.rollback()


# --- builders for the synthetic world (coordinates in metres, EPSG:9377) ------


def square_wkt(x: float, y: float, size: float) -> str:
    """Square with lower-left corner at (x, y)."""
    return (
        f"MULTIPOLYGON((({x} {y}, {x + size} {y}, {x + size} {y + size}, {x} {y + size}, {x} {y})))"
    )


def add_geofence(conn, code, wkt, *, limit=30, geofence_type="high_accident", max_vertices=128):
    """Insert a geofence and its subdivided parts; return its id."""
    geofence_id = conn.execute(
        """
        INSERT INTO reference.geofences (zone_code, name, geofence_type, speed_limit_kmh, geom)
        VALUES (%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 9377)))
        RETURNING geofence_id
        """,
        (code, f"Test {code}", geofence_type, limit, wkt),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO reference.geofence_parts (geofence_id, geom)
        SELECT geofence_id, ST_Subdivide(geom, %s) FROM reference.geofences
        WHERE geofence_id = %s
        """,
        (max_vertices, geofence_id),
    )
    return geofence_id


def add_road(conn, wkt, *, limit=50, road_class="primary"):
    return conn.execute(
        """
        INSERT INTO reference.roads (road_class, speed_limit_kmh, geom)
        VALUES (%s, %s, ST_GeomFromText(%s, 9377)) RETURNING road_id
        """,
        (road_class, limit, wkt),
    ).fetchone()[0]


def geofence_codes_at(conn, x, y) -> set[str]:
    rows = conn.execute(
        """
        SELECT g.zone_code
        FROM silver.geofences_at(ST_SetSRID(ST_MakePoint(%s, %s), 9377)) AS f
        JOIN reference.geofences AS g USING (geofence_id)
        """,
        (x, y),
    ).fetchall()
    return {row[0] for row in rows}
