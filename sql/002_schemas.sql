-- Medallion architecture: one schema per layer keeps ownership and grants simple.
CREATE SCHEMA IF NOT EXISTS reference;  -- static GIS layers (geofences, roads...)
CREATE SCHEMA IF NOT EXISTS bronze;     -- raw telemetry exactly as received
CREATE SCHEMA IF NOT EXISTS silver;     -- enriched points and alerts
CREATE SCHEMA IF NOT EXISTS gold;       -- BI-ready consolidated tables and views

COMMENT ON SCHEMA reference IS 'Static reference layers in EPSG:9377 (metres).';
COMMENT ON SCHEMA bronze    IS 'Raw IoT/AVL telemetry, append-only, EPSG:4326.';
COMMENT ON SCHEMA silver    IS 'Telemetry enriched with geofences, limits and alerts.';
COMMENT ON SCHEMA gold      IS 'Consolidated, indexed data for BI consumption.';
