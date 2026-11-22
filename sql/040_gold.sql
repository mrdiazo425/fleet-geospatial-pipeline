-- GOLD: consolidated, denormalised and indexed data ready for BI tools.
-- One wide fact table (no joins needed) plus small pre-aggregated views.

CREATE TABLE IF NOT EXISTS gold.fact_telemetry (
    telemetry_id          bigint PRIMARY KEY,
    vehicle_id            text        NOT NULL,
    reported_at           timestamptz NOT NULL,
    report_date           date        NOT NULL,  -- local day (America/Bogota)
    latitude              double precision NOT NULL,
    longitude             double precision NOT NULL,
    easting_m             double precision NOT NULL,  -- EPSG:9377
    northing_m            double precision NOT NULL,  -- EPSG:9377
    speed_kmh             real        NOT NULL,
    speed_limit_kmh       smallint    NOT NULL,
    speed_excess_kmh      real        NOT NULL,
    is_speeding           boolean     NOT NULL,
    speeding_severity     text,
    in_urban_perimeter    boolean     NOT NULL,
    in_high_accident_zone boolean     NOT NULL,
    geofence_code         text,
    geofence_name         text,
    road_class            text,
    road_name             text,
    step_seconds          real,
    step_distance_m       real,
    harsh_event           text,
    fuel_level_pct        real        NOT NULL,
    fuel_autonomy_km      real,
    route_remaining_km    real,
    fuel_insufficient     boolean,
    geom                  geometry(Point, 4326) NOT NULL,
    -- device health: when the platform received the report, and how late
    ingested_at           timestamptz,
    arrival_delay_s       real
);
-- Databases created before these columns existed get them on the next start.
ALTER TABLE gold.fact_telemetry
    ADD COLUMN IF NOT EXISTS ingested_at timestamptz,
    ADD COLUMN IF NOT EXISTS arrival_delay_s real;

-- Index strategy (see DESIGN.md):
--   GiST          -> spatial filters (bbox, radius, polygon) on every point
--   partial GiST  -> heatmaps only read infractions: a much smaller index
--   B-tree        -> one vehicle over a time range (route drawing)
--   BRIN          -> fleet-wide time ranges at negligible storage cost
CREATE INDEX IF NOT EXISTS fact_telemetry_geom_gist
    ON gold.fact_telemetry USING gist (geom);
CREATE INDEX IF NOT EXISTS fact_telemetry_speeding_geom_gist
    ON gold.fact_telemetry USING gist (geom) WHERE is_speeding;
CREATE INDEX IF NOT EXISTS fact_telemetry_vehicle_time_idx
    ON gold.fact_telemetry (vehicle_id, reported_at);
CREATE INDEX IF NOT EXISTS fact_telemetry_reported_at_brin
    ON gold.fact_telemetry USING brin (reported_at);

-- Transmission delay of a report: seconds between the device taking the
-- reading and the platform receiving it. NULL for historical replays, which
-- say nothing about how the device is transmitting.
CREATE OR REPLACE FUNCTION gold.arrival_delay_s(
    reported_at timestamptz, ingested_at timestamptz, is_backfill boolean)
RETURNS real
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE WHEN NOT is_backfill
        THEN GREATEST(EXTRACT(EPOCH FROM ingested_at - reported_at), 0)::real
    END
$$;

-- Incremental load from Silver. The fact table is its own watermark.
CREATE OR REPLACE FUNCTION gold.sync_fact_telemetry()
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_from bigint;
    v_rows integer;
BEGIN
    SELECT COALESCE(max(telemetry_id), 0) INTO v_from FROM gold.fact_telemetry;

    INSERT INTO gold.fact_telemetry
    SELECT
        e.telemetry_id, e.vehicle_id, e.reported_at,
        (e.reported_at AT TIME ZONE 'America/Bogota')::date,
        b.latitude, b.longitude,
        ST_X(e.geom_9377), ST_Y(e.geom_9377),
        e.speed_kmh, e.speed_limit_kmh, e.speed_excess_kmh,
        e.speeding_severity IS NOT NULL, e.speeding_severity,
        e.in_urban_perimeter, e.high_accident_geofence_id IS NOT NULL,
        g.zone_code, g.name, r.road_class, r.name,
        e.step_seconds, e.step_distance_m, e.harsh_event,
        e.fuel_level_pct, e.fuel_autonomy_km, e.route_remaining_km, e.fuel_insufficient,
        b.geom,
        b.ingested_at,
        gold.arrival_delay_s(b.reported_at, b.ingested_at, b.is_backfill)
    FROM silver.telemetry_enriched AS e
    JOIN bronze.telemetry_raw AS b USING (telemetry_id)
    LEFT JOIN reference.geofences AS g ON g.geofence_id = e.high_accident_geofence_id
    LEFT JOIN reference.roads AS r ON r.road_id = e.road_id
    WHERE e.telemetry_id > v_from
    ON CONFLICT (telemetry_id) DO NOTHING;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;

-- DRIVER SCORE ----------------------------------------------------------------
-- 100 = perfect. Three capped penalties (50 / 25 / 10 points) so no single
-- factor dominates:
--   speeding  : share of driving time over the limit, weighted by severity
--               (minor 1, major 2, severe 3) and x1.5 inside high-accident zones
--   harshness : harsh accelerations/brakings per 100 km
--   fuel      : times the vehicle was left without enough fuel for its route
CREATE OR REPLACE FUNCTION gold.driver_score(
    weighted_speeding_ratio double precision,
    harsh_events_per_100km double precision,
    fuel_alerts integer)
RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT round(GREATEST(0,
        100
        - LEAST(50, 100 * COALESCE(weighted_speeding_ratio, 0))
        - LEAST(25, 0.5 * COALESCE(harsh_events_per_100km, 0))
        - LEAST(10, 5 * COALESCE(fuel_alerts, 0))
    )::numeric, 1)
$$;

CREATE OR REPLACE FUNCTION gold.speeding_weight(severity text, in_high_accident_zone boolean)
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE severity WHEN 'minor' THEN 1 WHEN 'major' THEN 2 WHEN 'severe' THEN 3 ELSE 0 END
         * CASE WHEN in_high_accident_zone THEN 1.5 ELSE 1 END
$$;

CREATE MATERIALIZED VIEW IF NOT EXISTS gold.driver_score_daily AS
WITH driving AS (
    SELECT
        vehicle_id,
        report_date,
        count(*)                                              AS reports,
        COALESCE(sum(step_distance_m), 0) / 1000.0            AS distance_km,
        COALESCE(sum(step_seconds) FILTER (WHERE speed_kmh >= 1), 0) AS driving_seconds,
        avg(speed_kmh) FILTER (WHERE speed_kmh >= 1)          AS avg_moving_speed_kmh,
        max(speed_kmh)                                        AS max_speed_kmh,
        count(*) FILTER (WHERE is_speeding)                   AS speeding_reports,
        COALESCE(sum(step_seconds) FILTER (WHERE is_speeding), 0) AS speeding_seconds,
        COALESCE(sum(step_seconds * gold.speeding_weight(speeding_severity, in_high_accident_zone)), 0)
                                                              AS weighted_speeding_seconds,
        count(*) FILTER (WHERE is_speeding AND in_high_accident_zone) AS zone_speeding_reports,
        COALESCE(sum(step_seconds) FILTER (WHERE in_high_accident_zone), 0) AS zone_seconds,
        count(*) FILTER (WHERE harsh_event IS NOT NULL)       AS harsh_events,
        count(*) FILTER (WHERE fuel_insufficient)             AS fuel_risk_reports
    FROM gold.fact_telemetry
    GROUP BY vehicle_id, report_date
),
fuel AS (
    SELECT vehicle_id, (reported_at AT TIME ZONE 'America/Bogota')::date AS report_date,
           count(*)::integer AS fuel_alerts
    FROM silver.alerts
    WHERE alert_type = 'fuel_insufficient'
    GROUP BY 1, 2
)
SELECT
    d.vehicle_id,
    d.report_date,
    d.reports,
    round(d.distance_km::numeric, 2)                  AS distance_km,
    round((d.driving_seconds / 60.0)::numeric, 1)     AS driving_minutes,
    round(d.avg_moving_speed_kmh::numeric, 1)         AS avg_moving_speed_kmh,
    round(d.max_speed_kmh::numeric, 1)                AS max_speed_kmh,
    d.speeding_reports,
    round((100.0 * d.speeding_seconds / NULLIF(d.driving_seconds, 0))::numeric, 1)
                                                      AS speeding_time_pct,
    d.zone_speeding_reports,
    round((d.zone_seconds / 60.0)::numeric, 1)        AS high_accident_zone_minutes,
    d.harsh_events,
    round((100.0 * d.harsh_events / NULLIF(d.distance_km, 0))::numeric, 2)
                                                      AS harsh_events_per_100km,
    COALESCE(f.fuel_alerts, 0)                        AS fuel_alerts,
    gold.driver_score(
        d.weighted_speeding_seconds / NULLIF(d.driving_seconds, 0),
        100.0 * d.harsh_events / NULLIF(d.distance_km, 0),
        COALESCE(f.fuel_alerts, 0))                   AS driver_score
FROM driving AS d
LEFT JOIN fuel AS f USING (vehicle_id, report_date);

CREATE UNIQUE INDEX IF NOT EXISTS driver_score_daily_pk
    ON gold.driver_score_daily (vehicle_id, report_date);

-- HOTSPOTS: infractions aggregated on a 250 m grid (metres, EPSG:9377),
-- published as WGS84 points for web maps.
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.infraction_hotspots AS
SELECT
    row_number() OVER ()                          AS hotspot_id,
    a.alert_type,
    count(*)                                      AS infractions,
    count(DISTINCT a.vehicle_id)                  AS vehicles,
    round(avg(a.observed - a.threshold)::numeric, 1) AS avg_excess,
    ST_Transform(ST_SnapToGrid(a.geom_9377, 250), 4326)::geometry(Point, 4326) AS geom
FROM silver.alerts AS a
WHERE a.alert_type IN ('speeding', 'harsh_braking', 'harsh_acceleration')
GROUP BY a.alert_type, ST_SnapToGrid(a.geom_9377, 250);

CREATE UNIQUE INDEX IF NOT EXISTS infraction_hotspots_pk
    ON gold.infraction_hotspots (hotspot_id);
CREATE INDEX IF NOT EXISTS infraction_hotspots_geom_gist
    ON gold.infraction_hotspots USING gist (geom);

-- ROUTE OPTIMISATION INPUT: how fast the fleet really moves on each road, by
-- hour of day. A routing engine can use observed_speed_kmh (instead of the
-- posted limit) as the travel cost of a segment; congestion_ratio close to 0
-- means traffic jams, close to 1 means free flow.
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.road_speed_profile AS
SELECT
    e.road_id,
    EXTRACT(HOUR FROM e.reported_at AT TIME ZONE 'America/Bogota')::smallint AS hour_of_day,
    r.road_class,
    r.name                                              AS road_name,
    r.speed_limit_kmh,
    count(*)                                            AS reports,
    count(DISTINCT e.vehicle_id)                        AS vehicles,
    round(avg(e.speed_kmh)::numeric, 1)                 AS observed_speed_kmh,
    round((avg(e.speed_kmh) / r.speed_limit_kmh)::numeric, 2) AS congestion_ratio,
    round((100.0 * count(*) FILTER (WHERE e.speeding_severity IS NOT NULL) / count(*))::numeric, 1)
                                                        AS speeding_reports_pct,
    ST_Transform(r.geom, 4326)::geometry(LineString, 4326) AS geom
FROM silver.telemetry_enriched AS e
JOIN reference.roads AS r USING (road_id)
GROUP BY e.road_id, hour_of_day, r.road_class, r.name, r.speed_limit_kmh, r.geom;

CREATE UNIQUE INDEX IF NOT EXISTS road_speed_profile_pk
    ON gold.road_speed_profile (road_id, hour_of_day);
CREATE INDEX IF NOT EXISTS road_speed_profile_geom_gist
    ON gold.road_speed_profile USING gist (geom);

CREATE OR REPLACE FUNCTION gold.refresh_aggregates()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW gold.driver_score_daily;
    REFRESH MATERIALIZED VIEW gold.infraction_hotspots;
    REFRESH MATERIALIZED VIEW gold.road_speed_profile;
END;
$$;

-- PRIVACY: what non-admin users (role fleet_analyst) are allowed to read. -----
CREATE OR REPLACE VIEW gold.v_fact_telemetry AS
SELECT bronze.mask_vehicle_id(vehicle_id) AS vehicle_id,
       telemetry_id, reported_at, report_date, latitude, longitude, easting_m, northing_m,
       speed_kmh, speed_limit_kmh, speed_excess_kmh, is_speeding, speeding_severity,
       in_urban_perimeter, in_high_accident_zone, geofence_code, geofence_name,
       road_class, road_name, step_seconds, step_distance_m, harsh_event,
       fuel_level_pct, fuel_autonomy_km, route_remaining_km, fuel_insufficient, geom,
       ingested_at, arrival_delay_s
FROM gold.fact_telemetry;

CREATE OR REPLACE VIEW gold.v_driver_score_daily AS
SELECT bronze.mask_vehicle_id(vehicle_id) AS vehicle_id,
       report_date, reports, distance_km, driving_minutes, avg_moving_speed_kmh,
       max_speed_kmh, speeding_reports, speeding_time_pct, zone_speeding_reports,
       high_accident_zone_minutes, harsh_events, harsh_events_per_100km,
       fuel_alerts, driver_score
FROM gold.driver_score_daily;

-- Alerts with WGS84 coordinates, ready to plot (admin: real vehicle ids).
-- reported_at = when the device took the reading; ingested_at = when the
-- platform received it; arrival_delay_s = the gap (NULL for replays).
CREATE OR REPLACE VIEW gold.alerts AS
SELECT a.alert_id, a.vehicle_id, a.reported_at, a.alert_type, a.severity,
       g.zone_code AS geofence_code, a.observed, a.threshold,
       ST_Transform(a.geom_9377, 4326)::geometry(Point, 4326) AS geom,
       ST_Y(ST_Transform(a.geom_9377, 4326)) AS latitude,
       ST_X(ST_Transform(a.geom_9377, 4326)) AS longitude,
       b.ingested_at,
       gold.arrival_delay_s(b.reported_at, b.ingested_at, b.is_backfill) AS arrival_delay_s
FROM silver.alerts AS a
JOIN bronze.telemetry_raw AS b USING (telemetry_id)
LEFT JOIN reference.geofences AS g USING (geofence_id);

CREATE OR REPLACE VIEW gold.v_alerts AS
SELECT alert_id, bronze.mask_vehicle_id(vehicle_id) AS vehicle_id,
       reported_at, alert_type, severity, geofence_code, observed, threshold, geom,
       latitude, longitude, ingested_at, arrival_delay_s
FROM gold.alerts;

GRANT USAGE ON SCHEMA gold TO fleet_analyst;
GRANT SELECT ON gold.v_fact_telemetry, gold.v_driver_score_daily, gold.v_alerts,
    gold.infraction_hotspots, gold.road_speed_profile TO fleet_analyst;
