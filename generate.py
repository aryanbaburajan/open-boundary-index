"""Generate release-ready ADM2 boundary assets from geoBoundaries.

This deliberately writes its output outside the app bundle. Point it at the
working tree of the public data repository (for example
``../open-boundary-index``), then attach its ``dist/<version>`` directory to a
GitHub Release. Raw upstream geometry is held only in memory.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib import request


API = "https://www.geoboundaries.org/api/current/gbOpen/ALL/{level}/"
HEADERS = {"User-Agent": "open-boundary-index-generator/1.0", "Accept": "application/json"}
LEVELS = ("ADM2",)
MAX_ASSET_BYTES = 100 * 1024 * 1024


def fetch_json(url: str) -> Any:
    with request.urlopen(request.Request(url, headers=HEADERS), timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def assert_asset_size(path: Path) -> None:
    if path.stat().st_size > MAX_ASSET_BYTES:
        raise RuntimeError(f"Release asset exceeds GitHub's 100 MiB limit: {path} ({path.stat().st_size:,} bytes)")


def canonical_name(properties: dict[str, Any]) -> str | None:
    for key in ("shapeName", "name", "NAME_3", "NAME_4", "NAME_5", "admin"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def aliases(properties: dict[str, Any], name: str) -> list[str]:
    values = [name]
    for key in ("shapeName", "name", "NAME_3", "NAME_4", "NAME_5", "VARNAME_3", "VARNAME_4", "VARNAME_5"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
    return values


def normalize_ring(ring: Any) -> list[list[float]]:
    if not isinstance(ring, list):
        return []
    points = [[float(point[0]), float(point[1])] for point in ring if isinstance(point, list) and len(point) >= 2 and isinstance(point[0], (int, float)) and isinstance(point[1], (int, float))]
    if len(points) < 3:
        return []
    if points[0] != points[-1]:
        points.append(points[0][:])
    return points if len(points) >= 4 else []


def simplify_ring(points: list[list[float]], tolerance: float) -> list[list[float]]:
    """A small iterative Douglas–Peucker implementation for closed rings."""
    if len(points) <= 5 or tolerance <= 0:
        return points
    closed = points[:-1]
    if len(closed) < 3:
        return points
    keep = [False] * len(closed)
    keep[0] = keep[-1] = True
    tolerance_squared = tolerance * tolerance
    stack = [(0, len(closed) - 1)]
    while stack:
        start, end = stack.pop()
        ax, ay = closed[start]
        bx, by = closed[end]
        dx, dy = bx - ax, by - ay
        denominator = dx * dx + dy * dy
        furthest, furthest_distance = -1, 0.0
        for index in range(start + 1, end):
            px, py = closed[index]
            ratio = 0.0 if denominator == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denominator))
            qx, qy = ax + ratio * dx, ay + ratio * dy
            distance = (px - qx) ** 2 + (py - qy) ** 2
            if distance > furthest_distance:
                furthest, furthest_distance = index, distance
        if furthest >= 0 and furthest_distance > tolerance_squared:
            keep[furthest] = True
            stack.extend(((start, furthest), (furthest, end)))
    simplified = [point for index, point in enumerate(closed) if keep[index]]
    return simplified + [simplified[0][:]] if len(simplified) >= 3 else points


def polygons(geometry: Any, tolerance: float) -> list[list[list[list[float]]]]:
    if not isinstance(geometry, dict):
        return []
    geometry_type, coordinates = geometry.get("type"), geometry.get("coordinates")
    raw_polygons = [coordinates] if geometry_type == "Polygon" else coordinates if geometry_type == "MultiPolygon" else []
    if not isinstance(raw_polygons, list):
        return []
    output: list[list[list[list[float]]]] = []
    for raw_polygon in raw_polygons:
        if not isinstance(raw_polygon, list):
            continue
        rings = [simplify_ring(ring, tolerance) for ring in (normalize_ring(raw_ring) for raw_ring in raw_polygon)]
        rings = [ring for ring in rings if ring]
        if rings:
            output.append(rings)
    return output


def all_points(shape: Iterable[Iterable[Iterable[list[float]]]]) -> Iterable[list[float]]:
    for polygon in shape:
        for ring in polygon:
            yield from ring


def bbox_and_centroid(shape: list[list[list[list[float]]]]) -> tuple[list[float], list[float]]:
    points = list(all_points(shape))
    west = min(point[0] for point in points)
    south = min(point[1] for point in points)
    east = max(point[0] for point in points)
    north = max(point[1] for point in points)
    return [west, south, east, north], [(west + east) / 2, (south + north) / 2]


def output_geometry(shape: list[list[list[list[float]]]]) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": shape[0]} if len(shape) == 1 else {"type": "MultiPolygon", "coordinates": shape}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_level(output: Path, version: str, level: str, countries: set[str], tolerance: float) -> list[dict[str, Any]]:
    index = fetch_json(API.format(level=level))
    if not isinstance(index, list):
        raise RuntimeError(f"Invalid geoBoundaries index for {level}")
    sources: list[dict[str, Any]] = []
    for item in index:
        if not isinstance(item, dict):
            continue
        iso3 = str(item.get("boundaryISO") or "").upper()
        if countries and iso3 not in countries:
            continue
        geometry_url = str(item.get("simplifiedGeometryGeoJSON") or item.get("gjDownloadURL") or "")
        if not iso3 or not geometry_url:
            continue
        print(f"{level} {iso3}", flush=True)
        payload = fetch_json(geometry_url)
        raw_features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(raw_features, list):
            continue
        chunk_name = f"{iso3}-{level}.geojson.gz"
        manifest_name = f"{iso3}-{level}.json.gz"
        chunk_path = output / "chunks" / level / chunk_name
        manifest_path = output / "manifests" / level / manifest_name
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        feature_count = 0
        with gzip.open(chunk_path, "wt", encoding="utf-8", compresslevel=9) as chunk_output, gzip.open(manifest_path, "wt", encoding="utf-8", compresslevel=9) as manifest_output:
            chunk_output.write('{"type":"FeatureCollection","features":[')
            manifest_output.write('{"version":' + json.dumps(version) + ',"places":[')
            for ordinal, raw_feature in enumerate(raw_features):
                if not isinstance(raw_feature, dict):
                    continue
                properties = raw_feature.get("properties")
                name = canonical_name(properties) if isinstance(properties, dict) else None
                shape = polygons(raw_feature.get("geometry"), tolerance)
                if not name or not shape:
                    continue
                source_id = str(properties.get("shapeID") or properties.get("id") or ordinal)
                place_id = f"gb:{version}:{level}:{iso3}:{source_id}"
                bbox, centroid = bbox_and_centroid(shape)
                if feature_count:
                    chunk_output.write(",")
                    manifest_output.write(",")
                json.dump({"type": "Feature", "properties": {"id": place_id, "name": name}, "geometry": output_geometry(shape)}, chunk_output, ensure_ascii=False, separators=(",", ":"))
                json.dump({"id": place_id, "name": name, "aliases": aliases(properties, name), "country": iso3, "level": level, "centroid": centroid, "bbox": bbox, "chunk": f"chunks/{level}/{chunk_name}"}, manifest_output, ensure_ascii=False, separators=(",", ":"))
                feature_count += 1
            chunk_output.write("]}")
            manifest_output.write("]}")
        if not feature_count:
            chunk_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            continue
        assert_asset_size(chunk_path)
        assert_asset_size(manifest_path)
        sources.append({"country": iso3, "level": level, "apiRecord": item, "geometryUrl": geometry_url, "features": feature_count})
    return sources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="Data repository directory")
    parser.add_argument("--version", required=True, help="Release version, e.g. 2026-08-24")
    parser.add_argument("--countries", default="", help="Comma-separated ISO3 subset; omit for global")
    parser.add_argument("--levels", default="ADM2", help="Administrative levels (ADM2 only)")
    parser.add_argument("--tolerance", type=float, default=0.001, help="Geometry simplification in degrees")
    arguments = parser.parse_args()
    countries = {country.strip().upper() for country in arguments.countries.split(",") if country.strip()}
    levels = tuple(level.strip().upper() for level in arguments.levels.split(",") if level.strip())
    if not levels or any(level not in LEVELS for level in levels):
        raise SystemExit(f"--levels must contain only: {', '.join(LEVELS)}")
    output = arguments.output / "dist" / arguments.version
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    sources = [source for level in levels for source in generate_level(output, arguments.version, level, countries, arguments.tolerance)]
    write_json(output / "index.json", {"version": arguments.version, "levels": list(levels), "countries": sorted({source["country"] for source in sources})})
    write_json(output / "sources.json", {"source": "geoBoundaries gbOpen", "generatedAt": arguments.version, "records": sources})
    (output / "ATTRIBUTION.txt").write_text("Boundary data: geoBoundaries (www.geoboundaries.org), gbOpen. See sources.json for source records.\n", encoding="utf-8")
    files = sorted(path for path in output.rglob("*") if path.is_file())
    (output / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.relative_to(output)}\n" for path in files), encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
