-- Fleet master data: vehicle catalogue and the route assigned to each vehicle.
-- The telemetry packet only carries a fuel percentage, so tank capacity and
-- nominal efficiency live here and feed the predictive fuel algorithm.

CREATE TABLE IF NOT EXISTS reference.vehicles (
    vehicle_id      text PRIMARY KEY
        CHECK (vehicle_id ~ '^VHC-[0-9]{4}-[A-Z]{3}$'),
    tank_capacity_l numeric(6, 1) NOT NULL CHECK (tank_capacity_l > 0),
    efficiency_km_l numeric(5, 2) NOT NULL CHECK (efficiency_km_l > 0)
);

CREATE TABLE IF NOT EXISTS reference.vehicle_routes (
    route_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id     text NOT NULL UNIQUE
        REFERENCES reference.vehicles (vehicle_id) ON DELETE CASCADE,
    geom           geometry(LineString, 9377) NOT NULL,
    route_length_m double precision GENERATED ALWAYS AS (ST_Length(geom)) STORED
);
CREATE INDEX IF NOT EXISTS vehicle_routes_geom_gist
    ON reference.vehicle_routes USING gist (geom);
