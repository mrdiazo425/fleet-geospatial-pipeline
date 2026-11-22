-- SILVER: telemetry enriched with geofences, speed limits, motion metrics,
-- predictive fuel range and alerts. Everything spatial runs in EPSG:9377.
--
-- Business rules live in small IMMUTABLE functions so that each one can be
-- unit tested in isolation and reused by the batch job and the simulator.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.pipeline_state (
    id                boolean PRIMARY KEY DEFAULT true CHECK (id),
    last_telemetry_id bigint      NOT NULL DEFAULT 0,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
INSERT INTO silver.pipeline_state DEFAULT VALUES ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS silver.telemetry_enriched (
    telemetry_id              bigint PRIMARY KEY
        REFERENCES bronze.telemetry_raw (telemetry_id) ON DELETE CASCADE,
    vehicle_id                text        NOT NULL,
    reported_at               timestamptz NOT NULL,
    speed_kmh                 real        NOT NULL,
    fuel_level_pct            real        NOT NULL,
    geom_9377                 geometry(Point, 9377) NOT NULL,
    -- geofencing
    in_urban_perimeter        boolean     NOT NULL,
    high_accident_geofence_id integer REFERENCES reference.geofences (geofence_id),
    geofence_speed_limit_kmh  smallint,
    -- road matching
    road_id                   integer,
    road_speed_limit_kmh      smallint,
    -- speed rule
    speed_limit_kmh           smallint    NOT NULL,
    speed_excess_kmh          real        NOT NULL,
    speeding_severity         text CHECK (speeding_severity IN ('minor', 'major', 'severe')),
    -- motion since the previous report of the same vehicle
    step_seconds              real,
    step_distance_m           real,
    acceleration_ms2          real,
    harsh_event               text CHECK (harsh_event IN ('harsh_acceleration', 'harsh_braking')),
    -- predictive fuel
    route_remaining_km        real,
    fuel_rate_pct_km          real,
    fuel_autonomy_km          real,
    fuel_insufficient         boolean,
    processed_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS telemetry_enriched_geom_gist
    ON silver.telemetry_enriched USING gist (geom_9377);
CREATE INDEX IF NOT EXISTS telemetry_enriched_vehicle_time_idx
    ON silver.telemetry_enriched (vehicle_id, reported_at);

CREATE TABLE IF NOT EXISTS silver.alerts (
    alert_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telemetry_id bigint      NOT NULL
        REFERENCES silver.telemetry_enriched (telemetry_id) ON DELETE CASCADE,
    vehicle_id   text        NOT NULL,
    reported_at  timestamptz NOT NULL,
    alert_type   text        NOT NULL CHECK (alert_type IN (
        'speeding', 'fuel_insufficient', 'harsh_acceleration', 'harsh_braking', 'zone_entry')),
    severity     text        NOT NULL CHECK (severity IN ('info', 'minor', 'major', 'severe')),
    geofence_id  integer REFERENCES reference.geofences (geofence_id),
    observed     real,       -- measured value (km/h, km of autonomy, m/s2)
    threshold    real,       -- value it was compared against
    geom_9377    geometry(Point, 9377) NOT NULL,
    CONSTRAINT alerts_once_per_report UNIQUE (telemetry_id, alert_type)
);
CREATE INDEX IF NOT EXISTS alerts_geom_gist ON silver.alerts USING gist (geom_9377);
CREATE INDEX IF NOT EXISTS alerts_type_time_idx ON silver.alerts (alert_type, reported_at);
CREATE INDEX IF NOT EXISTS alerts_vehicle_time_idx ON silver.alerts (vehicle_id, reported_at);

-- ---------------------------------------------------------------------------
-- Spatial lookups (index-driven)
-- ---------------------------------------------------------------------------

-- Geofences containing a point. ST_Covers (not ST_Contains) so that a point
-- exactly on a boundary counts as inside; this also keeps results correct on
-- the internal cut lines introduced by ST_Subdivide. The GiST index on
-- geofence_parts resolves the bounding-box filter.
CREATE OR REPLACE FUNCTION silver.geofences_at(pt geometry)
RETURNS TABLE (geofence_id integer, geofence_type text, speed_limit_kmh smallint)
LANGUAGE sql STABLE PARALLEL SAFE
AS $$
    SELECT DISTINCT g.geofence_id, g.geofence_type, g.speed_limit_kmh
    FROM reference.geofence_parts AS p
    JOIN reference.geofences AS g USING (geofence_id)
    WHERE ST_Covers(p.geom, pt)
$$;

-- Nearest road within a tolerance (simple map matching). ST_DWithin bounds
-- the search with the GiST index; "<->" orders candidates by distance (KNN).
CREATE OR REPLACE FUNCTION silver.nearest_road(pt geometry, max_distance_m double precision DEFAULT 30)
RETURNS TABLE (road_id integer, speed_limit_kmh smallint, distance_m double precision)
LANGUAGE sql STABLE PARALLEL SAFE
AS $$
    SELECT r.road_id, r.speed_limit_kmh, ST_Distance(r.geom, pt)
    FROM reference.roads AS r
    WHERE ST_DWithin(r.geom, pt, max_distance_m)
    ORDER BY r.geom <-> pt
    LIMIT 1
$$;

-- ---------------------------------------------------------------------------
-- Business rules (pure functions)
-- ---------------------------------------------------------------------------

-- The most restrictive limit wins. With neither a road nor a geofence the
-- general urban limit applies.
CREATE OR REPLACE FUNCTION silver.applicable_speed_limit(
    road_limit_kmh integer, geofence_limit_kmh integer, default_limit_kmh integer DEFAULT 50)
RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT COALESCE(LEAST(road_limit_kmh, geofence_limit_kmh), default_limit_kmh)
$$;

-- NULL = not speeding. The tolerance absorbs GPS/speedometer error.
CREATE OR REPLACE FUNCTION silver.speeding_severity(
    speed_kmh real, limit_kmh integer, tolerance_kmh real DEFAULT 3)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN speed_kmh IS NULL OR limit_kmh IS NULL THEN NULL
        WHEN speed_kmh <= limit_kmh + tolerance_kmh THEN NULL
        WHEN speed_kmh - limit_kmh <= 10 THEN 'minor'
        WHEN speed_kmh - limit_kmh <= 20 THEN 'major'
        ELSE 'severe'
    END
$$;

-- A step between two consecutive reports is usable only if they belong to
-- the same trip (short gap) and the implied speed is physically possible
-- (otherwise it is a GPS jump).
CREATE OR REPLACE FUNCTION silver.is_valid_step(
    seconds double precision, distance_m double precision,
    max_gap_s double precision DEFAULT 300, max_speed_kmh double precision DEFAULT 200)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT COALESCE(
        seconds > 0 AND seconds <= max_gap_s
        AND distance_m / seconds * 3.6 <= max_speed_kmh,
        false)
$$;

-- Distance travelled in a step. A stationary vehicle still "moves" a few
-- metres between reports because of GPS noise; that drift is not distance.
CREATE OR REPLACE FUNCTION silver.step_distance_m(
    distance_m double precision, speed_kmh real, previous_speed_kmh real)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE WHEN speed_kmh < 1 AND previous_speed_kmh < 1 THEN 0 ELSE distance_m END
$$;

CREATE OR REPLACE FUNCTION silver.harsh_event(
    acceleration_ms2 double precision,
    accel_threshold_ms2 double precision DEFAULT 2.5,
    brake_threshold_ms2 double precision DEFAULT -3.0)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN acceleration_ms2 >= accel_threshold_ms2 THEN 'harsh_acceleration'
        WHEN acceleration_ms2 <= brake_threshold_ms2 THEN 'harsh_braking'
    END
$$;

-- PREDICTIVE FUEL ------------------------------------------------------------
-- Manufacturer consumption, as % of the tank burnt per km.
CREATE OR REPLACE FUNCTION silver.nominal_fuel_rate_pct_km(
    tank_capacity_l numeric, efficiency_km_l numeric)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT 100.0 / (tank_capacity_l * efficiency_km_l)
$$;

-- Consumption used for the prediction. The rate actually observed over the
-- recent window is preferred (it reflects traffic, load and driving style);
-- with too little evidence, or after a refuel, the nominal rate is used.
-- The observed rate is clamped to a plausible band around the nominal one so
-- a noisy fuel sensor cannot produce absurd predictions.
CREATE OR REPLACE FUNCTION silver.fuel_rate_pct_km(
    nominal_rate_pct_km double precision,
    observed_drop_pct double precision,
    observed_km double precision,
    min_observed_km double precision DEFAULT 1.0)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN observed_km >= min_observed_km AND observed_drop_pct > 0 THEN
            LEAST(GREATEST(observed_drop_pct / observed_km, nominal_rate_pct_km * 0.5),
                  nominal_rate_pct_km * 3.0)
        ELSE nominal_rate_pct_km
    END
$$;

CREATE OR REPLACE FUNCTION silver.fuel_autonomy_km(
    fuel_level_pct double precision, rate_pct_km double precision)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE WHEN rate_pct_km > 0 THEN fuel_level_pct / rate_pct_km END
$$;

-- Alert when the predicted range does not cover what is left of the route
-- plus a safety reserve.
CREATE OR REPLACE FUNCTION silver.is_fuel_insufficient(
    autonomy_km double precision, remaining_km double precision,
    reserve_ratio double precision DEFAULT 0.10)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT autonomy_km < remaining_km * (1 + reserve_ratio)
$$;

-- Speed limit that applies at a point: road limit vs geofence limit.
CREATE OR REPLACE FUNCTION silver.speed_limit_at(pt geometry)
RETURNS TABLE (
    road_id integer, road_limit_kmh smallint,
    geofence_limit_kmh smallint, speed_limit_kmh integer)
LANGUAGE sql STABLE PARALLEL SAFE
AS $$
    SELECT rd.road_id, rd.speed_limit_kmh, gf.limit_kmh,
           silver.applicable_speed_limit(rd.speed_limit_kmh, gf.limit_kmh)
    FROM (SELECT min(g.speed_limit_kmh)::smallint AS limit_kmh
          FROM silver.geofences_at(pt) AS g) AS gf
    LEFT JOIN LATERAL silver.nearest_road(pt) AS rd ON true
$$;
