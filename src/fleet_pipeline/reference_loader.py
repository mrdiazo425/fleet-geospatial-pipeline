"""Load the reference GIS layers from the GeoPackage into PostGIS.

The GeoPackage is already in EPSG:9377, so layers are loaded as they are.
Loading uses ``COPY`` with hex EWKB geometries, the fastest bulk path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import geopandas as gpd
import pandas as pd
import psycopg
import shapely
from psycopg import sql
from shapely.geometry import MultiPolygon

from fleet_pipeline.config import METRIC_SRID, Settings

logger = logging.getLogger(__name__)

REFERENCE_TABLES = ("geofences", "roads", "facilities")
FLEET_TABLES = ("vehicles", "vehicle_routes")


class ReferenceDataError(ValueError):
    """Raised when a source layer does not have the expected shape."""


def _read_layer(gpkg: Path, layer: str, required: Iterable[str]) -> gpd.GeoDataFrame:
    """Read a layer and validate CRS, columns and geometries."""
    gdf = gpd.read_file(gpkg, layer=layer)
    missing = sorted(set(required) - set(gdf.columns))
    if missing:
        raise ReferenceDataError(f"Layer '{layer}' is missing columns: {missing}")
    if gdf.crs is None or gdf.crs.to_epsg() != METRIC_SRID:
        raise ReferenceDataError(f"Layer '{layer}' must be in EPSG:{METRIC_SRID}, found {gdf.crs}")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.empty:
        raise ReferenceDataError(f"Layer '{layer}' has no valid geometries")
    # Some sources carry a Z coordinate; every table here is strictly 2D.
    return gdf.set_geometry(shapely.force_2d(gdf.geometry.values), crs=gdf.crs)


def _as_multipolygon(geom: shapely.Geometry) -> MultiPolygon:
    """Promote single polygons so the column type is uniform."""
    return geom if isinstance(geom, MultiPolygon) else MultiPolygon([geom])


def _copy_rows(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    gdf: gpd.GeoDataFrame,
) -> int:
    """Bulk insert ``columns`` plus the geometry of ``gdf`` into ``table``."""
    geoms = gdf.geometry.to_wkb(hex=True, include_srid=True).tolist()
    values = [[None if pd.isna(v) else v for v in gdf[column].tolist()] for column in columns]
    statement = sql.SQL("COPY reference.{} ({}) FROM STDIN").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, [*columns, "geom"])),
    )
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in zip(*values, geoms, strict=True):
            copy.write_row(row)
    return len(gdf)


def _load_geofences(conn: psycopg.Connection, settings: Settings) -> int:
    gpkg = settings.reference_gpkg
    columns = [
        "zone_code", "name", "geofence_type", "locality",
        "speed_limit_kmh", "fatalities", "injuries",
    ]  # fmt: skip

    zones = _read_layer(
        gpkg, "zonas_siniestralidad", ["ID_ZONA_ZC_LOC", "LOCALIDAD", "speed_limit_kmh"]
    ).rename(
        columns={
            "ID_ZONA_ZC_LOC": "zone_code",
            "LOCALIDAD": "locality",
            "FALLECIDOS_ZC_LOC": "fatalities",
            "LESIONADOS_VU_ZC_LOC": "injuries",
        }
    )
    zones["name"] = "Zona crítica " + zones["zone_code"] + " - " + zones["locality"]
    zones["geofence_type"] = "high_accident"

    perimeter = _read_layer(gpkg, "perimetro_urbano", ["CODIGO_ID"]).rename(
        columns={"CODIGO_ID": "zone_code"}
    )
    perimeter["name"] = "Perímetro urbano de Bogotá D.C."
    perimeter["geofence_type"] = "urban_perimeter"
    perimeter["locality"] = None
    perimeter["speed_limit_kmh"] = settings.urban_speed_limit_kmh
    perimeter["fatalities"] = None
    perimeter["injuries"] = None

    geofences = pd.concat(
        [perimeter[[*columns, "geometry"]], zones[[*columns, "geometry"]]],
        ignore_index=True,
    )
    for column in ("speed_limit_kmh", "fatalities", "injuries"):
        geofences[column] = geofences[column].astype("Int64")
    geofences = gpd.GeoDataFrame(geofences, geometry="geometry", crs=zones.crs)
    geofences["geometry"] = geofences.geometry.map(_as_multipolygon)
    return _copy_rows(conn, "geofences", columns, geofences)


def _load_roads(conn: psycopg.Connection, settings: Settings) -> int:
    roads = _read_layer(
        settings.reference_gpkg, "vias", ["fclass", "speed_limit", "oneway"]
    ).rename(columns={"fclass": "road_class", "speed_limit": "speed_limit_kmh"})
    # MultiLineString -> one row per LineString: simpler for routing/snapping.
    roads = roads.explode(index_parts=False, ignore_index=True)
    roads["speed_limit_kmh"] = roads["speed_limit_kmh"].astype("Int64")
    columns = ["osm_id", "road_class", "name", "oneway", "speed_limit_kmh"]
    return _copy_rows(conn, "roads", columns, roads)


def _load_facilities(conn: psycopg.Connection, settings: Settings) -> int:
    facilities = _read_layer(
        settings.reference_gpkg, "equipamientos_especiales", ["fclass"]
    ).rename(columns={"fclass": "facility_type"})
    columns = ["osm_id", "facility_type", "name"]
    return _copy_rows(conn, "facilities", columns, facilities)


def _load_fleet(conn: psycopg.Connection, settings: Settings) -> dict[str, int]:
    fleet = _read_layer(
        settings.fleet_gpkg,
        "vehicle_routes",
        ["vehicle_id", "tank_capacity_l", "efficiency_km_l"],
    )
    vehicle_columns = ["vehicle_id", "tank_capacity_l", "efficiency_km_l"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO reference.vehicles (vehicle_id, tank_capacity_l, efficiency_km_l)"
            " VALUES (%s, %s, %s)",
            list(zip(*(fleet[c].tolist() for c in vehicle_columns), strict=True)),
        )
    routes = _copy_rows(conn, "vehicle_routes", ["vehicle_id"], fleet)
    return {"vehicles": len(fleet), "vehicle_routes": routes}


def is_loaded(conn: psycopg.Connection, tables: Sequence[str]) -> bool:
    """Return True when every table in ``tables`` already has rows."""
    for table in tables:
        query = sql.SQL("SELECT EXISTS (SELECT 1 FROM reference.{})").format(sql.Identifier(table))
        if not conn.execute(query).fetchone()[0]:
            return False
    return True


def load_reference_data(
    conn: psycopg.Connection, settings: Settings, *, force: bool = False
) -> dict[str, int]:
    """Load reference layers and the demo fleet, each in one transaction.

    A group is skipped when its tables already have rows unless ``force`` is
    set, so restarting the stack never wipes rows that other layers reference.
    """
    counts: dict[str, int] = {}

    if force or not is_loaded(conn, REFERENCE_TABLES):
        _require_file(settings.reference_gpkg)
        with conn.transaction():
            conn.execute(
                "TRUNCATE reference.geofences, reference.roads, reference.facilities "
                "RESTART IDENTITY CASCADE"
            )
            counts["geofences"] = _load_geofences(conn, settings)
            counts["roads"] = _load_roads(conn, settings)
            counts["facilities"] = _load_facilities(conn, settings)
            counts["geofence_parts"] = conn.execute(
                "SELECT reference.rebuild_geofence_parts()"
            ).fetchone()[0]
    else:
        logger.info("Reference layers already loaded; skipping")

    if force or not is_loaded(conn, FLEET_TABLES):
        _require_file(settings.fleet_gpkg)
        with conn.transaction():
            conn.execute("TRUNCATE reference.vehicles RESTART IDENTITY CASCADE")
            counts.update(_load_fleet(conn, settings))
    else:
        logger.info("Fleet already loaded; skipping")

    for table in (*REFERENCE_TABLES, *FLEET_TABLES):
        conn.execute(sql.SQL("ANALYZE reference.{}").format(sql.Identifier(table)))
    conn.commit()
    if counts:
        logger.info("Reference data loaded: %s", counts)
    return counts


def _require_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"GeoPackage not found: {path}")
