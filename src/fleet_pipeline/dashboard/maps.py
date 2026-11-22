"""Folium map: geofences, vehicle routes, infraction heatmap, live positions."""

from __future__ import annotations

import json

import folium
import pandas as pd
from branca.element import MacroElement
from folium.plugins import FastMarkerCluster, HeatMap
from jinja2 import Template

BOGOTA_CENTER = (4.65, -74.10)
ROUTE_COLORS = (
    "#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#e377c2",
    "#17becf", "#bcbd22", "#d62728", "#393b79", "#637939", "#8c6d31",
)  # fmt: skip
ZONE_COLOR, PERIMETER_COLOR = "#b3261e", "#1f2937"
HEALTH_COLOR, EDUCATION_COLOR = "#d6336c", "#3b5bdb"
HEALTH_TYPES = ("hospital", "clinic")
SATELLITE_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
SATELLITE_ATTRIBUTION = "Tiles &copy; Esri, Maxar, Earthstar Geographics"
SEVERITY_WEIGHT = {"minor": 0.35, "major": 0.7, "severe": 1.0}


class _MapStyle(MacroElement):
    """CSS for the map page.

    streamlit-folium only forwards elements that expose an ``html`` macro, so
    the style travels as one. It removes the black focus rectangle browsers
    draw around a clicked shape.
    """

    _template = Template(
        "{% macro html(this, kwargs) %}"
        "<style>.leaflet-interactive:focus { outline: none; }"
        ".leaflet-control-layers label { font-size: 12px; }</style>"
        "{% endmacro %}"
    )


_HEAT_GRADIENT = "linear-gradient(90deg,#3b82f6,#22c55e,#facc15,#ef4444)"


def _swatch(kind: str, color: str) -> str:
    """Small symbol shown next to a layer name, so the layer list is the legend."""
    styles = {
        "line": f"width:18px;height:0;border-top:4px solid {color}",
        "dashed": f"width:18px;height:0;border-top:2px dashed {color}",
        "area": f"width:14px;height:10px;border:1.5px solid {color};background:{color}22",
        "dot": f"width:11px;height:11px;border-radius:50%;background:{color}",
        "heat": "width:18px;height:8px;background:" + _HEAT_GRADIENT,
    }
    base = "display:inline-block;vertical-align:middle;margin-right:5px;box-sizing:border-box"
    return f'<span style="{base};{styles[kind]}"></span>'


def _flag_icon(color: str, alert: bool) -> folium.DivIcon:
    """Small flag on a pole; a red dot marks a vehicle short of fuel."""
    badge = '<circle cx="19" cy="5" r="4" fill="#e5484d" stroke="#fff"/>' if alert else ""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="26" viewBox="0 0 24 26">'
        '<line x1="4" y1="2" x2="4" y2="25" stroke="#1f2937" stroke-width="2" '
        'stroke-linecap="round"/>'
        f'<path d="M5 3 L19 6.5 L5 11 Z" fill="{color}" stroke="#1f2937" stroke-width="1"/>'
        f"{badge}</svg>"
    )
    return folium.DivIcon(html=svg, icon_size=(24, 26), icon_anchor=(4, 25), class_name="")


def _facility_callback(color: str, glyph: str) -> str:
    """JS factory for FastMarkerCluster: a coloured dot with a white glyph."""
    html = (
        f'<div style="width:18px;height:18px;border-radius:50%;background:{color};'
        "border:1.5px solid #fff;box-shadow:0 0 2px rgba(0,0,0,.5);color:#fff;font-size:11px;"
        f'line-height:16px;text-align:center;font-weight:700">{glyph}</div>'
    )
    return (
        "function (row) {"
        f"  var icon = L.divIcon({{html: '{html}', className: '', iconSize: [18, 18]}});"
        "  var marker = L.marker(new L.LatLng(row[0], row[1]), {icon: icon});"
        "  if (row[2]) { marker.bindTooltip(row[2]); }"
        "  return marker;"
        "}"
    )


def route_segments(points: pd.DataFrame) -> list[list[tuple[float, float]]]:
    """Split one vehicle's reports into continuous trips.

    ``step_seconds`` is NULL when the Silver layer found a gap (ignition off
    or GPS jump); drawing a line across it would invent a path never driven.
    """
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for latitude, longitude, step in zip(
        points["latitude"], points["longitude"], points["step_seconds"], strict=True
    ):
        if pd.isna(step) and current:
            segments.append(current)
            current = []
        current.append((latitude, longitude))
    if current:
        segments.append(current)
    return [segment for segment in segments if len(segment) > 1]


def _add_geofences(fmap: folium.Map, geofences: pd.DataFrame) -> None:
    perimeter = folium.FeatureGroup(
        name=_swatch("dashed", PERIMETER_COLOR) + "Perímetro urbano (50 km/h)"
    )
    zones = folium.FeatureGroup(
        name=_swatch("area", ZONE_COLOR) + "Zonas de alta siniestralidad (30 km/h)"
    )
    for row in geofences.itertuples():
        is_zone = row.geofence_type == "high_accident"
        tooltip = f"{row.name} · límite {row.speed_limit_kmh} km/h"
        if is_zone and pd.notna(row.fatalities):
            tooltip += f" · {int(row.fatalities)} fallecidos, {int(row.injuries)} lesionados"
        folium.GeoJson(
            json.loads(row.geojson),
            tooltip=tooltip,
            style_function=lambda _, is_zone=is_zone: {
                "color": ZONE_COLOR if is_zone else PERIMETER_COLOR,
                "weight": 2.2 if is_zone else 2.4,
                "opacity": 0.95,
                "dashArray": "6 3" if is_zone else "8 6",
                "fillColor": ZONE_COLOR,
                "fillOpacity": 0.16 if is_zone else 0.0,
            },
        ).add_to(zones if is_zone else perimeter)
    perimeter.add_to(fmap)
    zones.add_to(fmap)


def _add_routes(fmap: folium.Map, points: pd.DataFrame) -> None:
    routes = folium.FeatureGroup(name=_swatch("line", ROUTE_COLORS[0]) + "Rutas recorridas")
    latest = folium.FeatureGroup(name="🚩 Última posición del vehículo")
    for index, (vehicle_id, group) in enumerate(points.groupby("vehicle_id", sort=True)):
        color = ROUTE_COLORS[index % len(ROUTE_COLORS)]
        for segment in route_segments(group):
            # white casing underneath makes the route readable over any background
            folium.PolyLine(segment, color="#ffffff", weight=7, opacity=0.9).add_to(routes)
            folium.PolyLine(segment, color=color, weight=4, opacity=1, tooltip=vehicle_id).add_to(
                routes
            )
        last = group.iloc[-1]
        short_of_fuel = bool(last.fuel_insufficient) if pd.notna(last.fuel_insufficient) else False
        autonomy = "s/d" if pd.isna(last.fuel_autonomy_km) else f"{last.fuel_autonomy_km:.0f} km"
        fuel_note = "<br><b>Combustible insuficiente para su ruta</b>" if short_of_fuel else ""
        folium.Marker(
            (last.latitude, last.longitude),
            icon=_flag_icon(color, short_of_fuel),
            tooltip=(
                f"<b>{vehicle_id}</b><br>{last.speed_kmh:.0f} km/h "
                f"(límite {last.speed_limit_kmh})<br>Combustible {last.fuel_level_pct:.1f} % "
                f"· autonomía {autonomy}{fuel_note}"
            ),
        ).add_to(latest)
    routes.add_to(fmap)
    latest.add_to(fmap)


def _add_heatmap(fmap: folium.Map, alerts: pd.DataFrame) -> None:
    """Severe infractions weigh more, so real hotspots stand out."""
    weights = alerts["severity"].map(SEVERITY_WEIGHT).fillna(0.35)
    rows = [
        [lat, lon, w]
        for lat, lon, w in zip(alerts["latitude"], alerts["longitude"], weights, strict=True)
    ]
    layer = folium.FeatureGroup(name=_swatch("heat", "") + "Mapa de calor de infracciones")
    HeatMap(rows, radius=11, blur=14, min_opacity=0.25, max_zoom=13).add_to(layer)
    layer.add_to(fmap)


def _add_facilities(fmap: folium.Map, facilities: pd.DataFrame) -> None:
    is_health = facilities["facility_type"].isin(HEALTH_TYPES)
    groups = (
        ("Centros de salud", facilities[is_health], HEALTH_COLOR, "✚"),
        ("Centros educativos", facilities[~is_health], EDUCATION_COLOR, "🎓"),
    )
    for label, frame, color, glyph in groups:
        layer = folium.FeatureGroup(name=_swatch("dot", color) + label)
        rows = frame[["latitude", "longitude", "name"]].fillna("").values.tolist()
        FastMarkerCluster(
            rows, callback=_facility_callback(color, glyph), disableClusteringAtZoom=15
        ).add_to(layer)
        layer.add_to(fmap)


def build_map(
    points: pd.DataFrame,
    alerts: pd.DataFrame,
    geofences: pd.DataFrame,
    facilities: pd.DataFrame | None = None,
) -> folium.Map:
    fmap = folium.Map(location=BOGOTA_CENTER, zoom_start=12, tiles=None)
    folium.TileLayer("OpenStreetMap", name="Mapa de calles").add_to(fmap)
    folium.TileLayer(
        tiles=SATELLITE_TILES, attr=SATELLITE_ATTRIBUTION, name="Imagen satelital", show=False
    ).add_to(fmap)
    fmap.get_root().add_child(_MapStyle())
    _add_geofences(fmap, geofences)
    if not alerts.empty:
        _add_heatmap(fmap, alerts)
    if not points.empty:
        _add_routes(fmap, points)
        fmap.fit_bounds(
            [
                [points["latitude"].min(), points["longitude"].min()],
                [points["latitude"].max(), points["longitude"].max()],
            ],
            padding=(25, 25),
        )
    if facilities is not None and not facilities.empty:
        _add_facilities(fmap, facilities)
    folium.LayerControl(collapsed=True, position="topright").add_to(fmap)
    return fmap
