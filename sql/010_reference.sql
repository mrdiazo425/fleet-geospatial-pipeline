-- Reference layers. All geometries are stored in EPSG:9377 so that every
-- spatial operation against telemetry happens in metres on a planar CRS.

CREATE TABLE IF NOT EXISTS reference.geofences (
    geofence_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    zone_code       text        NOT NULL UNIQUE,
    name            text        NOT NULL,
    geofence_type   text        NOT NULL
        CHECK (geofence_type IN ('urban_perimeter', 'high_accident')),
    locality        text,
    speed_limit_kmh smallint    NOT NULL CHECK (speed_limit_kmh > 0),
    fatalities      integer,
    injuries        integer,
    geom            geometry(MultiPolygon, 9377) NOT NULL
);
CREATE INDEX IF NOT EXISTS geofences_geom_gist
    ON reference.geofences USING gist (geom);

-- Big polygons (the urban perimeter has thousands of vertices) make
-- point-in-polygon tests expensive: the GiST index only filters by bounding
-- box and the exact test then walks every vertex. ST_Subdivide splits each
-- geofence into small pieces so the index discards most of them and the exact
-- test runs against <= 128 vertices. Queries join against this table.
CREATE TABLE IF NOT EXISTS reference.geofence_parts (
    geofence_part_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    geofence_id      integer NOT NULL
        REFERENCES reference.geofences (geofence_id) ON DELETE CASCADE,
    geom             geometry(Polygon, 9377) NOT NULL
);
CREATE INDEX IF NOT EXISTS geofence_parts_geom_gist
    ON reference.geofence_parts USING gist (geom);
CREATE INDEX IF NOT EXISTS geofence_parts_geofence_idx
    ON reference.geofence_parts (geofence_id);

CREATE OR REPLACE FUNCTION reference.rebuild_geofence_parts()
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    part_count integer;
BEGIN
    TRUNCATE reference.geofence_parts RESTART IDENTITY;
    INSERT INTO reference.geofence_parts (geofence_id, geom)
    SELECT g.geofence_id, ST_Subdivide(ST_MakeValid(g.geom), 128)
    FROM reference.geofences AS g;
    GET DIAGNOSTICS part_count = ROW_COUNT;
    ANALYZE reference.geofence_parts;
    RETURN part_count;
END;
$$;

CREATE TABLE IF NOT EXISTS reference.roads (
    road_id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    osm_id          text,
    road_class      text     NOT NULL,
    name            text,
    oneway          char(1),
    speed_limit_kmh smallint NOT NULL CHECK (speed_limit_kmh > 0),
    geom            geometry(LineString, 9377) NOT NULL
);
CREATE INDEX IF NOT EXISTS roads_geom_gist
    ON reference.roads USING gist (geom);

CREATE TABLE IF NOT EXISTS reference.facilities (
    facility_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    osm_id        text,
    facility_type text NOT NULL,
    name          text,
    geom          geometry(Point, 9377) NOT NULL
);
CREATE INDEX IF NOT EXISTS facilities_geom_gist
    ON reference.facilities USING gist (geom);
