-- Spatial extension. Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS postgis;

-- EPSG:9377 (MAGNA-SIRGAS 2018 / Origen-Nacional) is the official projected CRS
-- for Colombia (IGAC Res. 471/2020). Older PostGIS builds do not ship it, so we
-- register it defensively. Units are metres, which is what we need for
-- distance, buffer and nearest-road operations.
INSERT INTO spatial_ref_sys (srid, auth_name, auth_srid, proj4text, srtext)
VALUES (
    9377, 'EPSG', 9377,
    '+proj=tmerc +lat_0=4 +lon_0=-73 +k=0.9992 +x_0=5000000 +y_0=2000000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs',
    'PROJCS["MAGNA-SIRGAS 2018 / Origen-Nacional",GEOGCS["MAGNA-SIRGAS 2018",DATUM["Marco_Geocentrico_Nacional_de_Referencia_2018",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",4],PARAMETER["central_meridian",-73],PARAMETER["scale_factor",0.9992],PARAMETER["false_easting",5000000],PARAMETER["false_northing",2000000],UNIT["metre",1],AUTHORITY["EPSG","9377"]]'
)
ON CONFLICT (srid) DO NOTHING;
