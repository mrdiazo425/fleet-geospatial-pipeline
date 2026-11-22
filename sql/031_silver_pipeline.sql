-- SILVER batch job: Bronze -> Silver for every report not yet processed.
-- Incremental (watermark on telemetry_id), idempotent (ON CONFLICT) and safe
-- to call concurrently (advisory lock). One call = one transaction.

CREATE OR REPLACE FUNCTION silver.process_new_telemetry(batch_size integer DEFAULT 20000)
RETURNS TABLE (processed integer, alerts integer)
LANGUAGE plpgsql
AS $$
DECLARE
    v_from      bigint;
    v_to        bigint;
    v_processed integer := 0;
    v_alerts    integer := 0;
BEGIN
    IF NOT pg_try_advisory_xact_lock(hashtext('silver.process_new_telemetry')) THEN
        RETURN QUERY SELECT 0, 0;  -- another worker is already running
        RETURN;
    END IF;

    SELECT last_telemetry_id INTO v_from FROM silver.pipeline_state;
    SELECT max(telemetry_id) INTO v_to
    FROM (SELECT telemetry_id FROM bronze.telemetry_raw
          WHERE telemetry_id > v_from ORDER BY telemetry_id LIMIT batch_size) AS next_batch;
    IF v_to IS NULL THEN
        RETURN QUERY SELECT 0, 0;
        RETURN;
    END IF;

    -- 1) Spatial enrichment + motion ----------------------------------------
    WITH batch AS MATERIALIZED (
        SELECT b.telemetry_id, b.vehicle_id, b.reported_at, b.speed_kmh, b.fuel_level_pct,
               ST_Transform(b.geom, 9377) AS geom_9377
        FROM bronze.telemetry_raw AS b
        WHERE b.telemetry_id > v_from AND b.telemetry_id <= v_to
    )
    INSERT INTO silver.telemetry_enriched (
        telemetry_id, vehicle_id, reported_at, speed_kmh, fuel_level_pct, geom_9377,
        in_urban_perimeter, high_accident_geofence_id, geofence_speed_limit_kmh,
        road_id, road_speed_limit_kmh, speed_limit_kmh, speed_excess_kmh, speeding_severity,
        step_seconds, step_distance_m, acceleration_ms2, harsh_event, route_remaining_km)
    SELECT
        b.telemetry_id, b.vehicle_id, b.reported_at, b.speed_kmh, b.fuel_level_pct, b.geom_9377,
        COALESCE(gf.in_urban, false),
        gf.high_accident_id,
        gf.limit_kmh,
        rd.road_id,
        rd.speed_limit_kmh,
        lim.speed_limit_kmh,
        GREATEST(b.speed_kmh - lim.speed_limit_kmh, 0),
        silver.speeding_severity(b.speed_kmh, lim.speed_limit_kmh),
        step.seconds,
        step.distance_m,
        step.acceleration_ms2,
        silver.harsh_event(step.acceleration_ms2),
        (1 - ST_LineLocatePoint(rt.geom, b.geom_9377)) * rt.route_length_m / 1000.0
    FROM batch AS b
    -- Geofencing: which geofences cover the point (GiST on geofence_parts)
    LEFT JOIN LATERAL (
        SELECT bool_or(g.geofence_type = 'urban_perimeter') AS in_urban,
               min(g.geofence_id) FILTER (WHERE g.geofence_type = 'high_accident')
                   AS high_accident_id,
               min(g.speed_limit_kmh)::smallint AS limit_kmh
        FROM silver.geofences_at(b.geom_9377) AS g
    ) AS gf ON true
    -- Map matching: nearest road (GiST KNN on roads)
    LEFT JOIN LATERAL silver.nearest_road(b.geom_9377) AS rd ON true
    CROSS JOIN LATERAL (
        SELECT silver.applicable_speed_limit(rd.speed_limit_kmh, gf.limit_kmh)::smallint
                   AS speed_limit_kmh
    ) AS lim
    -- Previous report of the same vehicle (index: vehicle_id, reported_at)
    LEFT JOIN LATERAL (
        SELECT p.reported_at, p.speed_kmh, ST_Transform(p.geom, 9377) AS geom_9377
        FROM bronze.telemetry_raw AS p
        WHERE p.vehicle_id = b.vehicle_id AND p.reported_at < b.reported_at
        ORDER BY p.reported_at DESC
        LIMIT 1
    ) AS prev ON true
    CROSS JOIN LATERAL (
        SELECT EXTRACT(EPOCH FROM b.reported_at - prev.reported_at)::double precision AS seconds,
               ST_Distance(b.geom_9377, prev.geom_9377) AS distance_m
    ) AS raw_step
    CROSS JOIN LATERAL (
        SELECT
            CASE WHEN ok THEN raw_step.seconds END AS seconds,
            CASE WHEN ok THEN silver.step_distance_m(
                raw_step.distance_m, b.speed_kmh, prev.speed_kmh) END AS distance_m,
            CASE WHEN ok AND raw_step.seconds <= 30 THEN
                (b.speed_kmh - prev.speed_kmh) / 3.6 / raw_step.seconds END AS acceleration_ms2
        FROM (SELECT silver.is_valid_step(raw_step.seconds, raw_step.distance_m) AS ok) AS v
    ) AS step
    LEFT JOIN reference.vehicle_routes AS rt ON rt.vehicle_id = b.vehicle_id
    ON CONFLICT (telemetry_id) DO NOTHING;
    GET DIAGNOSTICS v_processed = ROW_COUNT;

    -- 2) Predictive fuel ----------------------------------------------------
    -- Rolling 10-minute window per vehicle: fuel burnt vs distance driven.
    WITH bounds AS (
        SELECT e.vehicle_id, min(e.reported_at) AS lo, max(e.reported_at) AS hi
        FROM silver.telemetry_enriched AS e
        WHERE e.telemetry_id > v_from AND e.telemetry_id <= v_to
        GROUP BY e.vehicle_id
    ),
    windowed AS (
        SELECT e.telemetry_id, e.vehicle_id, e.fuel_level_pct, e.route_remaining_km,
               first_value(e.fuel_level_pct) OVER w - e.fuel_level_pct AS drop_pct,
               (sum(COALESCE(e.step_distance_m, 0)) OVER w
                    - first_value(COALESCE(e.step_distance_m, 0)) OVER w) / 1000.0 AS km
        FROM silver.telemetry_enriched AS e
        JOIN bounds AS b
          ON b.vehicle_id = e.vehicle_id
         AND e.reported_at BETWEEN b.lo - interval '10 minutes' AND b.hi
        WINDOW w AS (PARTITION BY e.vehicle_id ORDER BY e.reported_at
                     RANGE BETWEEN interval '10 minutes' PRECEDING AND CURRENT ROW)
    ),
    predicted AS (
        SELECT w.telemetry_id, w.route_remaining_km, r.rate,
               silver.fuel_autonomy_km(w.fuel_level_pct, r.rate) AS autonomy_km
        FROM windowed AS w
        JOIN reference.vehicles AS v USING (vehicle_id)
        CROSS JOIN LATERAL (
            SELECT silver.fuel_rate_pct_km(
                silver.nominal_fuel_rate_pct_km(v.tank_capacity_l, v.efficiency_km_l),
                w.drop_pct, w.km) AS rate
        ) AS r
        WHERE w.telemetry_id > v_from AND w.telemetry_id <= v_to
    )
    UPDATE silver.telemetry_enriched AS e
    SET fuel_rate_pct_km  = p.rate,
        fuel_autonomy_km  = p.autonomy_km,
        fuel_insufficient = silver.is_fuel_insufficient(p.autonomy_km, p.route_remaining_km)
    FROM predicted AS p
    WHERE e.telemetry_id = p.telemetry_id;

    -- 3) Alerts -------------------------------------------------------------
    -- Speeding and harsh driving: one alert per offending report.
    -- Fuel and zone entry: only on the transition, to avoid alert storms.
    WITH current AS (
        SELECT e.*, prev.fuel_insufficient AS prev_fuel_insufficient,
               prev.high_accident_geofence_id AS prev_zone_id,
               v.autonomy_needed_km
        FROM silver.telemetry_enriched AS e
        LEFT JOIN LATERAL (
            SELECT p.fuel_insufficient, p.high_accident_geofence_id
            FROM silver.telemetry_enriched AS p
            WHERE p.vehicle_id = e.vehicle_id AND p.reported_at < e.reported_at
              AND p.reported_at >= e.reported_at - interval '5 minutes'
            ORDER BY p.reported_at DESC
            LIMIT 1
        ) AS prev ON true
        CROSS JOIN LATERAL (SELECT e.route_remaining_km * 1.10 AS autonomy_needed_km) AS v
        WHERE e.telemetry_id > v_from AND e.telemetry_id <= v_to
    )
    INSERT INTO silver.alerts (
        telemetry_id, vehicle_id, reported_at, alert_type, severity,
        geofence_id, observed, threshold, geom_9377)
    SELECT telemetry_id, vehicle_id, reported_at, 'speeding', speeding_severity,
           high_accident_geofence_id, speed_kmh, speed_limit_kmh, geom_9377
    FROM current WHERE speeding_severity IS NOT NULL
    UNION ALL
    SELECT telemetry_id, vehicle_id, reported_at, harsh_event, 'minor',
           high_accident_geofence_id, acceleration_ms2, NULL, geom_9377
    FROM current WHERE harsh_event IS NOT NULL
    UNION ALL
    SELECT telemetry_id, vehicle_id, reported_at, 'fuel_insufficient', 'major',
           high_accident_geofence_id, fuel_autonomy_km, autonomy_needed_km, geom_9377
    FROM current
    WHERE fuel_insufficient AND prev_fuel_insufficient IS NOT TRUE
    UNION ALL
    SELECT telemetry_id, vehicle_id, reported_at, 'zone_entry', 'info',
           high_accident_geofence_id, speed_kmh, geofence_speed_limit_kmh, geom_9377
    FROM current
    WHERE high_accident_geofence_id IS NOT NULL
      AND high_accident_geofence_id IS DISTINCT FROM prev_zone_id
    ON CONFLICT (telemetry_id, alert_type) DO NOTHING;
    GET DIAGNOSTICS v_alerts = ROW_COUNT;

    UPDATE silver.pipeline_state SET last_telemetry_id = v_to, updated_at = now();
    RETURN QUERY SELECT v_processed, v_alerts;
END;
$$;
