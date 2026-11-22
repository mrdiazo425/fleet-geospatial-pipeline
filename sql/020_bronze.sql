-- BRONZE: raw telemetry exactly as the IoT/AVL device reported it.
-- Append-only. No business rule is applied here beyond basic sanity checks.

CREATE TABLE IF NOT EXISTS bronze.telemetry_raw (
    telemetry_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id     text             NOT NULL
        CHECK (vehicle_id ~ '^VHC-[0-9]{4}-[A-Z]{3}$'),
    reported_at    timestamptz      NOT NULL,
    latitude       double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude      double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    speed_kmh      real             NOT NULL CHECK (speed_kmh >= 0),
    fuel_level_pct real             NOT NULL CHECK (fuel_level_pct BETWEEN 0 AND 100),
    -- The point is derived from lat/lon by the database itself, so the
    -- geometry can never disagree with the raw coordinates.
    geom           geometry(Point, 4326) GENERATED ALWAYS AS
        (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED,
    ingested_at    timestamptz      NOT NULL DEFAULT now(),
    -- Devices retransmit when the network drops: this makes ingestion
    -- idempotent and doubles as the "one vehicle over time" access path.
    CONSTRAINT telemetry_raw_vehicle_time_uq UNIQUE (vehicle_id, reported_at)
);

-- Historical replays (a device uploading its memory, a migration, the demo
-- backfill) are flagged so they are never mistaken for late transmissions.
ALTER TABLE bronze.telemetry_raw
    ADD COLUMN IF NOT EXISTS is_backfill boolean NOT NULL DEFAULT false;

-- Spatial access path ("what was reported inside this area?").
CREATE INDEX IF NOT EXISTS telemetry_raw_geom_gist
    ON bronze.telemetry_raw USING gist (geom);
-- Telemetry arrives in time order, so a BRIN index covers time-range scans
-- at a tiny fraction of the size of a B-tree.
CREATE INDEX IF NOT EXISTS telemetry_raw_reported_at_brin
    ON bronze.telemetry_raw USING brin (reported_at);

-- PRIVACY -------------------------------------------------------------------
-- VHC-8472-ABC -> VHC-****-ABC. Anything that does not look like a vehicle id
-- is fully masked (fail closed).
CREATE OR REPLACE FUNCTION bronze.mask_vehicle_id(vehicle_id text)
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN vehicle_id ~ '^[A-Z]+-[0-9]+-[A-Z]+$'
            THEN regexp_replace(vehicle_id, '^([A-Z]+)-[0-9]+-([A-Z]+)$', '\1-****-\2')
        ELSE '****'
    END
$$;

CREATE OR REPLACE VIEW bronze.telemetry_masked AS
SELECT
    telemetry_id,
    bronze.mask_vehicle_id(vehicle_id) AS vehicle_id,
    reported_at,
    latitude,
    longitude,
    speed_kmh,
    fuel_level_pct,
    geom,
    ingested_at
FROM bronze.telemetry_raw;

-- Non-admin database role: can read the masked view but has no privilege on
-- the raw table, so masking holds even for direct SQL/BI connections.
-- Demo credentials; in production they come from a secrets manager.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_analyst') THEN
        CREATE ROLE fleet_analyst LOGIN PASSWORD 'analyst';
    END IF;
END
$$;

REVOKE ALL ON bronze.telemetry_raw FROM fleet_analyst;
GRANT USAGE ON SCHEMA bronze, reference TO fleet_analyst;
GRANT SELECT ON bronze.telemetry_masked TO fleet_analyst;
GRANT SELECT ON reference.geofences, reference.roads, reference.facilities
    TO fleet_analyst;
