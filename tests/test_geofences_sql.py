"""Geofencing logic: is a point inside or outside each geofence?"""

import pytest

from tests.conftest import add_geofence, geofence_codes_at, square_wkt

# One 1 km square geofence with its lower-left corner at (1000, 1000).
SQUARE = square_wkt(1000, 1000, 1000)


@pytest.mark.parametrize(
    ("x", "y", "inside"),
    [
        (1500, 1500, True),  # centre
        (1001, 1001, True),  # just inside a corner
        (1999.999, 1500, True),  # just inside an edge
        (999.999, 1500, False),  # just outside an edge
        (2000.001, 2000.001, False),  # just outside a corner
        (5000, 5000, False),  # far away
    ],
)
def test_point_inside_or_outside(conn, x, y, inside):
    add_geofence(conn, "SQ", SQUARE)
    assert (geofence_codes_at(conn, x, y) == {"SQ"}) is inside


@pytest.mark.parametrize(("x", "y"), [(1000, 1500), (1500, 2000), (1000, 1000), (2000, 2000)])
def test_point_on_the_boundary_counts_as_inside(conn, x, y):
    """ST_Covers semantics: edges and vertices belong to the geofence."""
    add_geofence(conn, "SQ", SQUARE)
    assert geofence_codes_at(conn, x, y) == {"SQ"}


def test_point_in_overlapping_geofences_returns_all_of_them(conn):
    add_geofence(conn, "CITY", square_wkt(0, 0, 10_000), geofence_type="urban_perimeter", limit=50)
    add_geofence(conn, "ZONE", SQUARE, limit=30)
    assert geofence_codes_at(conn, 1500, 1500) == {"CITY", "ZONE"}
    assert geofence_codes_at(conn, 5000, 5000) == {"CITY"}
    assert geofence_codes_at(conn, 20_000, 20_000) == set()


def test_point_inside_a_hole_is_outside(conn):
    donut = (
        "MULTIPOLYGON(((0 0, 3000 0, 3000 3000, 0 3000, 0 0),"
        "(1000 1000, 2000 1000, 2000 2000, 1000 2000, 1000 1000)))"
    )
    add_geofence(conn, "DONUT", donut)
    assert geofence_codes_at(conn, 500, 500) == {"DONUT"}
    assert geofence_codes_at(conn, 1500, 1500) == set()


def test_multipart_geofence_covers_every_part(conn):
    two_islands = (
        "MULTIPOLYGON(((0 0,100 0,100 100,0 100,0 0)),((500 0,600 0,600 100,500 100,500 0)))"
    )
    add_geofence(conn, "ISLANDS", two_islands)
    assert geofence_codes_at(conn, 50, 50) == {"ISLANDS"}
    assert geofence_codes_at(conn, 550, 50) == {"ISLANDS"}
    assert geofence_codes_at(conn, 300, 50) == set()


def test_subdivided_geofence_matches_the_original_polygon(conn):
    """ST_Subdivide is an optimisation: it must never change the answer.

    A 720-vertex circle is split into many parts; a grid of 1,600 points
    (including points lying exactly on the internal cut lines) must give the
    same inside/outside result as the original, undivided polygon.

    Points closer than 1 mm to the outer boundary are left out: clipping
    recomputes boundary vertices in floating point, so a point sitting exactly
    on the edge can legitimately flip depending on the GEOS version. That is
    numerical noise far below GPS accuracy, not a geofencing error.
    """
    geofence_id = conn.execute(
        """
        INSERT INTO reference.geofences (zone_code, name, geofence_type, speed_limit_kmh, geom)
        VALUES ('CIRCLE', 'Test circle', 'high_accident', 30,
                ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(5000, 5000), 9377), 2000, 180)))
        RETURNING geofence_id
        """
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO reference.geofence_parts (geofence_id, geom)
        SELECT geofence_id, ST_Subdivide(geom, 16) FROM reference.geofences
        WHERE geofence_id = %s
        """,
        (geofence_id,),
    )
    parts = conn.execute("SELECT count(*) FROM reference.geofence_parts").fetchone()[0]
    assert parts > 20

    compared, inside, mismatches = conn.execute(
        """
        WITH grid AS (
            SELECT ST_SetSRID(ST_MakePoint(x, y), 9377) AS pt
            FROM generate_series(2500, 7500, 125) AS x, generate_series(2500, 7500, 125) AS y
        ),
        checked AS (
            SELECT grid.pt,
                   ST_Covers(g.geom, grid.pt) AS in_original,
                   EXISTS (SELECT 1 FROM silver.geofences_at(grid.pt)) AS in_parts
            FROM grid, reference.geofences AS g
            WHERE g.geofence_id = %s
              AND ST_Distance(ST_Boundary(g.geom), grid.pt) > 0.001
        )
        SELECT count(*),
               count(*) FILTER (WHERE in_original),
               array_agg(ST_AsText(pt)) FILTER (WHERE in_original IS DISTINCT FROM in_parts)
        FROM checked
        """,
        (geofence_id,),
    ).fetchone()
    assert compared > 1600  # nearly the whole 41 x 41 grid is compared
    assert 700 < inside < 900  # and it really straddles the circle
    assert mismatches is None, f"subdivision changed the result at {mismatches}"


def test_gps_coordinates_are_projected_before_the_test(conn):
    """A WGS84 point is matched after projecting it to EPSG:9377 (metres)."""
    x, y = conn.execute(
        """
        SELECT ST_X(p), ST_Y(p)
        FROM ST_Transform(ST_SetSRID(ST_MakePoint(-74.0721, 4.7110), 4326), 9377) AS p
        """
    ).fetchone()
    # Bogotá sits near easting 4.88 M / northing 2.08 M in Origen Nacional.
    assert 4_800_000 < x < 4_950_000
    assert 2_000_000 < y < 2_150_000
    add_geofence(conn, "NEAR", square_wkt(x - 50, y - 50, 100))
    assert geofence_codes_at(conn, x, y) == {"NEAR"}
    assert geofence_codes_at(conn, x + 200, y) == set()


def test_st_covers_agrees_with_st_within_and_st_contains_away_from_the_boundary(conn):
    """The spec names ST_Contains / ST_Within; this pipeline uses ST_Covers.

    For any point not exactly on a boundary the three predicates give the same
    answer. They only differ on the boundary itself, where ST_Covers counts the
    point as inside and the other two do not.
    """
    add_geofence(conn, "SQ", SQUARE)
    query = """
        SELECT ST_Covers(g.geom, p.pt), ST_Contains(g.geom, p.pt), ST_Within(p.pt, g.geom)
        FROM reference.geofences AS g,
             (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 9377) AS pt) AS p
        WHERE g.zone_code = 'SQ'
    """
    assert conn.execute(query, (1500, 1500)).fetchone() == (True, True, True)  # inside
    assert conn.execute(query, (2500, 1500)).fetchone() == (False, False, False)  # outside
    assert conn.execute(query, (1000, 1500)).fetchone() == (True, False, False)  # on the edge
