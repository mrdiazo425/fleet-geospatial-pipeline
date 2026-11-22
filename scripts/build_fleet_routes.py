"""Build the demo fleet: vehicles and their assigned routes over real roads.

Offline helper (not part of the runtime). It nodes the main road network,
finds shortest paths between a depot and delivery points, and writes
``data/fleet.gpkg`` (layer ``vehicle_routes``, EPSG:9377). Committing the
output keeps the simulation reproducible without a routing engine at runtime.

Usage: ``python scripts/build_fleet_routes.py``  (needs ``networkx``)
"""

from __future__ import annotations

import random
from pathlib import Path

import geopandas as gpd
import networkx as nx
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_GPKG = ROOT / "data" / "reference.gpkg"
OUTPUT_GPKG = ROOT / "data" / "fleet.gpkg"

MAIN_ROAD_CLASSES = {
    "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link",
}  # fmt: skip
SEED = 42
MIN_ROUTE_M, MAX_ROUTE_M = 8_000, 30_000

# (vehicle_id, tank capacity in litres, efficiency in km per litre)
FLEET = [
    ("VHC-8472-ABC", 60, 11.0), ("VHC-1035-BDF", 55, 12.5), ("VHC-2291-CGH", 70, 9.0),
    ("VHC-3378-DJK", 60, 10.5), ("VHC-4416-ELM", 80, 7.5), ("VHC-5520-FNP", 55, 13.0),
    ("VHC-6194-GQR", 65, 9.5), ("VHC-7083-HST", 60, 11.5), ("VHC-7765-JUV", 90, 6.5),
    ("VHC-8851-KWX", 55, 12.0), ("VHC-9307-LYZ", 70, 8.5), ("VHC-9942-MAB", 60, 10.0),
]  # fmt: skip


def build_graph(roads: gpd.GeoDataFrame) -> nx.Graph:
    """Node the network (split lines at every crossing) and build a graph."""
    noded = unary_union(roads.geometry.values)
    graph = nx.Graph()
    for line in shapely.get_parts(noded):
        start, end = line.coords[0], line.coords[-1]
        graph.add_edge(start, end, weight=line.length, geom=line)
    largest = max(nx.connected_components(graph), key=len)
    return graph.subgraph(largest).copy()


def nearest_node(graph: nx.Graph, point: Point) -> tuple[float, float]:
    return min(graph.nodes, key=lambda n: (n[0] - point.x) ** 2 + (n[1] - point.y) ** 2)


def path_to_line(graph: nx.Graph, path: list[tuple[float, float]]) -> LineString:
    """Stitch edge geometries into a single LineString oriented along ``path``."""
    coords: list[tuple[float, float]] = [path[0]]
    for a, b in zip(path, path[1:], strict=False):
        edge = list(graph.edges[a, b]["geom"].coords)
        if edge[0] != a:
            edge.reverse()
        coords.extend(edge[1:])
    return LineString(coords)


def main() -> None:
    rng = random.Random(SEED)
    roads = gpd.read_file(REFERENCE_GPKG, layer="vias")
    roads = roads[roads["fclass"].isin(MAIN_ROAD_CLASSES)]
    zones = gpd.read_file(REFERENCE_GPKG, layer="zonas_siniestralidad")
    facilities = gpd.read_file(REFERENCE_GPKG, layer="equipamientos_especiales")

    graph = build_graph(roads)
    nodes = list(graph.nodes)

    # Destinations: half the fleet delivers inside high-accident zones, the
    # rest to clinics/hospitals, so routes exercise every geofence rule.
    zone_targets = [g.representative_point() for g in zones.geometry]
    clinic_targets = list(facilities[facilities["fclass"].isin(["hospital", "clinic"])].geometry)

    records = []
    for index, (vehicle_id, tank_l, km_per_l) in enumerate(FLEET):
        targets = zone_targets if index % 2 == 0 else clinic_targets
        for _ in range(200):
            origin = rng.choice(nodes)
            destination = nearest_node(graph, rng.choice(targets))
            try:
                path = nx.shortest_path(graph, origin, destination, weight="weight")
            except nx.NetworkXNoPath:
                continue
            line = path_to_line(graph, path)
            if MIN_ROUTE_M <= line.length <= MAX_ROUTE_M:
                break
        else:
            raise RuntimeError(f"No suitable route found for {vehicle_id}")
        records.append(
            {
                "vehicle_id": vehicle_id,
                "tank_capacity_l": tank_l,
                "efficiency_km_l": km_per_l,
                "route_length_m": round(line.length, 1),
                "geometry": shapely.force_2d(line),
            }
        )

    fleet = gpd.GeoDataFrame(records, crs=roads.crs)
    OUTPUT_GPKG.unlink(missing_ok=True)
    fleet.to_file(OUTPUT_GPKG, layer="vehicle_routes", driver="GPKG")
    print(fleet[["vehicle_id", "route_length_m"]].to_string(index=False))


if __name__ == "__main__":
    main()
