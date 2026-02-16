"""Import builder: API LOC response → QGIS memory layers + mappings.

Parses the response from ``LOCs/all-locs-for-location`` and creates
in-memory vector layers grouped by category, with auto-generated
LayerMapping objects for a seamless round-trip back to push.

Hidden tracking attributes (``_loc_id``, ``_origin_id``, ``_dest_id``,
``_multi_id``, ``_stop_ids``) store server IDs so the export builder
can distinguish updates from new creates.
"""

import json as _json
import os
from typing import Dict, List, Optional, Set, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from ..models.category import Category, CategoryField
from ..models.mapping import FieldMapping, LayerMapping
from ..models.route import Route, Stop, StopType


WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
_TAG = "LOC Import"


def _log(msg: str, level=Qgis.Info) -> None:
    QgsMessageLog.logMessage(msg, _TAG, level)

# Essential fields that always appear on imported layers
_POINT_ESSENTIAL = ["_loc_id", "_origin_id", "_origin_lon", "_origin_lat",
                    "_sloc_json",
                    "Unique Asset Identifier", "Actual Asset Name"]
_LINE_ESSENTIAL = ["_loc_id", "_origin_id", "_dest_id", "_multi_id",
                   "_stop_ids", "_stop_loc_ids",
                   "_origin_lon", "_origin_lat", "_dest_lon", "_dest_lat",
                   "_dloc_json", "_mloc_json",
                   "Route ID", "Actual Asset Name", "Destination"]

# Known field names excluded from numbered field_N mapping
_SINGLE_KNOWN = {"unique asset identifier", "actual asset name"}
_DUAL_ORIGIN_KNOWN = {"route id", "origin", "unique asset identifier",
                      "actual asset name"}
_DUAL_DEST_KNOWN = {"destination"}


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def extract_multi_stop_counts(locs_data: dict) -> Dict[str, int]:
    """Count multiLoc stops consistent with reconstruction ghost-stop rules.

    Reconstruction drops "ghost stops" — stops whose singleLoc coordinates
    don't match any standalone singleLOC (which become the point features
    in the QGIS point layer).  This function applies the same filtering
    using only the raw API data, so the expected count matches what
    ``reconstruct_routes()`` will actually produce.

    Returns ``{multiLoc_id: matched_stop_count}``.
    """
    if not isinstance(locs_data, dict):
        return {}

    # Build set of all standalone singleLOC coordinates (8dp ≈ 1 mm)
    single_coords: set = set()
    for sloc in locs_data.get("singleLOCs", []):
        lon = _float(sloc.get("origin_longitude", 0))
        lat = _float(sloc.get("origin_latitude", 0))
        if lon != 0 or lat != 0:
            single_coords.add((round(lon, 8), round(lat, 8)))

    result: Dict[str, int] = {}
    for mloc in locs_data.get("multiLoc", []):
        multi_id = mloc.get("id", mloc.get("_id", ""))
        if not multi_id:
            continue

        stops = mloc.get("Stops", mloc.get("stops", []))
        count = 0
        for stop_entry in stops:
            sloc = (stop_entry.get("singleLoc")
                    or stop_entry.get("singleLOC")
                    or stop_entry.get("single_loc")
                    or {})
            lon = _float(sloc.get("origin_longitude", 0))
            lat = _float(sloc.get("origin_latitude", 0))
            if lon == 0 and lat == 0:
                continue
            coord = (round(lon, 8), round(lat, 8))
            if coord in single_coords:
                count += 1
        result[multi_id] = count

    return result


def build_layers(
    locs_data: dict,
    categories: List[Category],
) -> Tuple[List[QgsVectorLayer], List[LayerMapping], Set[str]]:
    """Parse API LOC data and build QGIS memory layers.

    Returns:
        (layers, mappings, pulled_ids)
        - layers: list of QgsVectorLayer (memory) ready to add to project
        - mappings: auto-generated LayerMapping for each layer
        - pulled_ids: set of all loc_id values from the server
    """
    # Debug: dump raw pull data for inspection
    try:
        dump_path = os.path.join(os.path.expanduser("~"), "loc_pull_data.json")
        with open(dump_path, "w", encoding="utf-8") as f:
            _json.dump(locs_data, f, indent=2, default=str)
    except Exception:
        pass

    cat_lookup: Dict[str, Category] = {c.category_id: c for c in categories}
    field_name_maps = _build_field_name_maps(categories)

    # Accumulators: {(cat_id, geom_type): [features]}
    # geom_type is "point" or "line"
    point_buckets: Dict[str, List[QgsFeature]] = {}
    line_buckets: Dict[str, List[QgsFeature]] = {}

    # Per-bucket field names (beyond essentials) — preserves order
    point_fields: Dict[str, List[str]] = {}
    line_fields: Dict[str, List[str]] = {}

    pulled_ids: Set[str] = set()

    if not isinstance(locs_data, dict):
        locs_data = {}

    _log(f"LOC data keys: {list(locs_data.keys())}")
    _log(f"singleLOCs: {len(locs_data.get('singleLOCs', []))}, "
         f"dualLOCs: {len(locs_data.get('dualLOCs', []))}, "
         f"multiLoc: {len(locs_data.get('multiLoc', []))}")

    # Log first multiLoc entry structure for debugging
    multi_entries = locs_data.get("multiLoc", [])
    if multi_entries:
        first = multi_entries[0]
        _log(f"First multiLoc keys: {list(first.keys())}")
        dl = first.get("dualLoc") or first.get("dualLOC") or {}
        _log(f"First multiLoc dualLoc keys: {list(dl.keys()) if isinstance(dl, dict) else type(dl)}")

    # --- Parse singleLOCs ---
    for sloc in locs_data.get("singleLOCs", []):
        cat_id = sloc.get("category", sloc.get("category_id", ""))
        loc_id = sloc.get("loc_id", "")
        if loc_id:
            pulled_ids.add(loc_id)

        cat = cat_lookup.get(cat_id)
        cat_name = cat.name if cat else cat_id
        fmap = field_name_maps.get(cat_id, {})

        # Ensure bucket exists
        if cat_id not in point_buckets:
            point_buckets[cat_id] = []
            point_fields[cat_id] = _category_field_names(cat, "point", fmap)

        feat = _build_point_feature(sloc, fmap, point_fields[cat_id])
        point_buckets[cat_id].append(feat)

    # --- Parse dualLOCs (standalone) ---
    for dloc in locs_data.get("dualLOCs", []):
        cat_id = dloc.get("category", dloc.get("category_id", ""))
        loc_id = dloc.get("loc_id", "")
        if loc_id:
            pulled_ids.add(loc_id)

        cat = cat_lookup.get(cat_id)
        fmap = field_name_maps.get(cat_id, {})

        if cat_id not in line_buckets:
            line_buckets[cat_id] = []
            line_fields[cat_id] = _category_field_names(cat, "line", fmap)

        feat = _build_line_feature(dloc, fmap, line_fields[cat_id])
        line_buckets[cat_id].append(feat)

    # --- Parse multiLoc entries ---
    for mloc in locs_data.get("multiLoc", []):
        multi_id = mloc.get("id", mloc.get("_id", ""))

        # The embedded dual LOC may appear under different keys
        dual_loc = (mloc.get("dualLoc")
                    or mloc.get("dualLOC")
                    or mloc.get("dual_loc")
                    or {})

        stops = mloc.get("Stops", mloc.get("stops", []))

        _log(f"multiLoc {multi_id}: keys={list(mloc.keys())}, "
             f"dualLoc keys={list(dual_loc.keys())}, stops={len(stops)}")

        cat_id = dual_loc.get("category", dual_loc.get("category_id", ""))
        loc_id = dual_loc.get("loc_id", "")
        if loc_id:
            pulled_ids.add(loc_id)

        if not cat_id:
            _log(f"multiLoc {multi_id}: no category_id on dualLoc, skipping",
                 Qgis.Warning)
            continue

        cat = cat_lookup.get(cat_id)
        fmap = field_name_maps.get(cat_id, {})

        if cat_id not in line_buckets:
            line_buckets[cat_id] = []
            line_fields[cat_id] = _category_field_names(cat, "line", fmap)

        # Build polyline through origin → stops → destination
        stop_coords = _sorted_stop_coords(stops)
        stop_ids_str = ",".join(
            s.get("stop_id", "") for s in sorted(
                stops, key=lambda s: s.get("stopNumber", 0)
            )
        )

        _log(f"multiLoc {multi_id}: "
             f"origin=({dual_loc.get('origin_longitude')},{dual_loc.get('origin_latitude')}), "
             f"dest LOCDest={bool(dual_loc.get('LOCDestination'))}, "
             f"stop_coords={len(stop_coords)}")

        feat = _build_line_feature(
            dual_loc, fmap, line_fields[cat_id],
            extra_coords=stop_coords,
            multi_id=multi_id,
            stop_ids=stop_ids_str,
        )
        # Store raw multiLoc JSON (full wrapper incl. Stops + dualLoc)
        feat.setAttribute("_mloc_json", _json.dumps(mloc, default=str))
        line_buckets[cat_id].append(feat)

        # Track stop singleLoc IDs for delete detection on push,
        # but do NOT import them as point features — they are derived
        # data (one per stop per route) that interfere with route
        # generation.  Stops are rediscovered from structures on
        # regeneration and recreated fresh on push.
        #
        # Also build a coord→ID map stored on the line feature so the
        # export builder can reuse original server IDs on round-trip.
        stop_loc_parts: list = []
        for stop_entry in sorted(
            stops, key=lambda s: s.get("stopNumber", 0)
        ):
            sloc = (stop_entry.get("singleLoc")
                    or stop_entry.get("singleLOC")
                    or stop_entry.get("single_loc")
                    or {})
            sloc_id = sloc.get("loc_id", "")
            sloc_origin = sloc.get("origin_id", "")
            if sloc_id:
                pulled_ids.add(sloc_id)
            s_lon = _float(sloc.get("origin_longitude", 0))
            s_lat = _float(sloc.get("origin_latitude", 0))
            if sloc_id and (s_lon != 0 or s_lat != 0):
                # Store ALL stop IDs including IN/OUT pairs at the
                # same coordinate — the export builder pops them in
                # order so each stop gets its own unique ID.
                # Include exact server coordinates to avoid precision
                # drift through QGIS geometry storage.
                exact_lon = sloc.get("origin_longitude", "")
                exact_lat = sloc.get("origin_latitude", "")
                stop_loc_parts.append(
                    f"{round(s_lon, 8)},{round(s_lat, 8)}"
                    f"={sloc_id},{sloc_origin}"
                    f",{exact_lon},{exact_lat}"
                )
        if stop_loc_parts:
            feat.setAttribute("_stop_loc_ids", "|".join(stop_loc_parts))

    # --- Create memory layers ---
    layers: List[QgsVectorLayer] = []
    mappings: List[LayerMapping] = []

    for cat_id, features in point_buckets.items():
        cat = cat_lookup.get(cat_id)
        cat_name = cat.name if cat else cat_id
        extra_fields = point_fields.get(cat_id, [])
        all_field_names = list(_POINT_ESSENTIAL) + extra_fields

        layer = _create_memory_layer(
            f"{cat_name} (LOC)", "Point", all_field_names, features
        )
        layers.append(layer)

        mapping = _auto_mapping_point(layer, cat_id, cat_name, all_field_names)
        mappings.append(mapping)

    for cat_id, features in line_buckets.items():
        cat = cat_lookup.get(cat_id)
        cat_name = cat.name if cat else cat_id
        extra_fields = line_fields.get(cat_id, [])
        all_field_names = list(_LINE_ESSENTIAL) + extra_fields

        _log(f"Creating line layer '{cat_name} (LOC)': "
             f"{len(features)} features, {len(all_field_names)} fields")

        layer = _create_memory_layer(
            f"{cat_name} (LOC)", "LineString", all_field_names, features
        )
        _log(f"Line layer '{layer.name()}': provider has "
             f"{layer.dataProvider().featureCount()} features")
        layers.append(layer)

        # Determine stop category if any multiLocs used this cat
        stop_cat_id = ""
        stop_cat_name = ""
        for mloc in locs_data.get("multiLoc", []):
            dl = (mloc.get("dualLoc") or mloc.get("dualLOC")
                  or mloc.get("dual_loc") or {})
            if dl.get("category", dl.get("category_id", "")) == cat_id:
                for stop_entry in mloc.get("Stops", mloc.get("stops", [])):
                    sl = (stop_entry.get("singleLoc")
                          or stop_entry.get("singleLOC")
                          or stop_entry.get("single_loc") or {})
                    scid = sl.get("category", sl.get("category_id", ""))
                    if scid:
                        stop_cat_id = scid
                        sc = cat_lookup.get(scid)
                        stop_cat_name = sc.name if sc else scid
                        break
                if stop_cat_id:
                    break

        mapping = _auto_mapping_line(
            layer, cat_id, cat_name, all_field_names,
            stop_cat_id, stop_cat_name,
        )
        mappings.append(mapping)

    return layers, mappings, pulled_ids


def import_summary(locs_data: dict) -> dict:
    """Quick summary of LOC data for the pull dialog."""
    if not isinstance(locs_data, dict):
        return {"single": 0, "dual": 0, "multi": 0, "total": 0}

    singles = locs_data.get("singleLOCs", [])
    duals = locs_data.get("dualLOCs", [])
    multis = locs_data.get("multiLoc", [])

    # Count by category for singles
    single_by_cat: Dict[str, int] = {}
    for s in singles:
        cname = s.get("category_name", "Unknown")
        single_by_cat[cname] = single_by_cat.get(cname, 0) + 1

    dual_by_cat: Dict[str, int] = {}
    for d in duals:
        cname = d.get("category_name", "Unknown")
        dual_by_cat[cname] = dual_by_cat.get(cname, 0) + 1

    total_stop_singles = sum(
        len(m.get("Stops", [])) for m in multis
    )

    total = len(singles) + len(duals) + len(multis) + total_stop_singles

    return {
        "single": len(singles),
        "single_by_cat": single_by_cat,
        "dual": len(duals),
        "dual_by_cat": dual_by_cat,
        "multi": len(multis),
        "stop_singles": total_stop_singles,
        "total": total,
    }


# ------------------------------------------------------------------
# Route reconstruction from pulled layers
# ------------------------------------------------------------------

def reconstruct_routes(
    layer_mappings: List[LayerMapping],
) -> List[Route]:
    """Reconstruct Route objects from pulled QGIS layers.

    Pulled line features encode the full route geometry:
      vertex 0 = origin, vertex -1 = destination,
      intermediate vertices = stops (for multiLoc lines with ``_multi_id``).

    Point features are matched by coordinate proximity (tight epsilon
    since both sides originate from the same API data).

    This avoids the snapping-based ``generate_routes()`` which incorrectly
    matches ALL point features to every line in dense areas.
    """
    project = QgsProject.instance()

    line_mappings: List[LayerMapping] = []
    point_mappings: List[LayerMapping] = []

    for lm in layer_mappings:
        if not lm.include_in_routes:
            continue
        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue
        if layer.geometryType() == QgsWkbTypes.LineGeometry:
            line_mappings.append(lm)
        elif layer.geometryType() == QgsWkbTypes.PointGeometry:
            point_mappings.append(lm)

    if not line_mappings:
        return []

    # Build spatial index of all point features.
    # Key: (rounded_lon, rounded_lat) → list of (layer_id, feat_id, name, cat_id)
    # 8 decimal places ≈ 1 mm — avoids collisions between distinct
    # structures that are only millimetres apart in server data.
    pt_index: Dict[
        Tuple[float, float], List[Tuple[str, int, str, str]]
    ] = {}

    for pm in point_mappings:
        layer = project.mapLayer(pm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue
        name_field = (
            pm.qgis_field_for("Actual Asset Name")
            or pm.qgis_field_for("Unique Asset Identifier")
            or pm.first_mapped_qgis_field()
        )
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom.isEmpty() or geom.isNull():
                continue
            pt = geom.asPoint()
            key = (round(pt.x(), 8), round(pt.y(), 8))
            try:
                name = str(feat[name_field]) if name_field else ""
                if name == "NULL":
                    name = ""
            except Exception:
                name = ""
            pt_index.setdefault(key, []).append(
                (layer.id(), feat.id(), name, pm.category_id)
            )

    _log(f"reconstruct_routes: {len(line_mappings)} line layer(s), "
         f"{len(point_mappings)} point layer(s), "
         f"{len(pt_index)} unique point locations")

    routes: List[Route] = []

    for lm in line_mappings:
        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue

        stop_cat_id = lm.stop_category_id
        name_field = (
            lm.qgis_field_for("Route ID")
            or lm.first_mapped_qgis_field()
        )

        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom.isEmpty() or geom.isNull():
                continue

            if geom.isMultipart():
                parts = geom.asMultiPolyline()
                if not parts:
                    continue
                vertices: list = []
                for part in parts:
                    vertices.extend(part)
            else:
                vertices = geom.asPolyline()

            if len(vertices) < 2:
                continue

            line_name = _safe_feat_attr(feat, name_field)
            multi_id = _safe_feat_attr(feat, "_multi_id")

            # Read origin/dest names from line feature attributes
            origin_name = _safe_feat_attr(feat, "Actual Asset Name")
            dest_name = _safe_feat_attr(feat, "Destination")

            stops: List[Stop] = []

            # --- Origin = first vertex ---
            o_key = (round(vertices[0].x(), 8), round(vertices[0].y(), 8))
            o_match = _find_point(pt_index, o_key, exclude_cat=stop_cat_id)
            if o_match:
                stops.append(Stop(
                    original_name=o_match[2] or origin_name,
                    structure_name=o_match[2] or origin_name,
                    stop_type=StopType.ORIGIN,
                    stop_number=0,
                    point_layer_id=o_match[0],
                    point_feature_id=o_match[1],
                ))
            else:
                stops.append(Stop(
                    original_name=origin_name,
                    structure_name=origin_name,
                    stop_type=StopType.ORIGIN,
                    stop_number=0,
                ))

            # --- Intermediate vertices → stops (multiLoc lines only) ---
            # Consecutive duplicate vertices at the same coordinate
            # indicate an ingress/egress pair on the server.  We
            # detect this pattern and recreate the IN/OUT stops so the
            # user's data isn't silently changed to passthrough.
            if multi_id and len(vertices) > 2:
                stop_num = 0
                v_idx = 1
                end_idx = len(vertices) - 1
                while v_idx < end_idx:
                    v = vertices[v_idx]
                    key = (round(v.x(), 8), round(v.y(), 8))

                    # Count consecutive vertices at the same coord
                    run = 1
                    while (v_idx + run < end_idx
                           and (round(vertices[v_idx + run].x(), 8),
                                round(vertices[v_idx + run].y(), 8)) == key):
                        run += 1

                    s_match = _find_point(
                        pt_index, key, prefer_cat=stop_cat_id,
                    )
                    if not s_match:
                        v_idx += run
                        continue  # no matching structure — skip

                    s_name = s_match[2]
                    common = dict(
                        structure_name=s_name,
                        point_layer_id=s_match[0],
                        point_feature_id=s_match[1],
                    )

                    if run >= 2:
                        # Ingress / Egress pair
                        stop_num += 1
                        label_in = (f"{line_name}_{s_name}_IN"
                                    if line_name else f"{s_name}_IN")
                        label_out = (f"{line_name}_{s_name}_OUT"
                                     if line_name else f"{s_name}_OUT")
                        stops.append(Stop(
                            original_name=label_in,
                            stop_type=StopType.INGRESS,
                            stop_number=stop_num,
                            **common,
                        ))
                        stops.append(Stop(
                            original_name=label_out,
                            stop_type=StopType.EGRESS,
                            stop_number=stop_num,
                            **common,
                        ))
                    else:
                        # Single passthrough
                        stop_num += 1
                        label = (f"{line_name}_{s_name}"
                                 if line_name else s_name)
                        stops.append(Stop(
                            original_name=label,
                            stop_type=StopType.PASSTHROUGH,
                            stop_number=stop_num,
                            **common,
                        ))

                    v_idx += run

            # --- Destination = last vertex ---
            d_key = (round(vertices[-1].x(), 8),
                     round(vertices[-1].y(), 8))
            d_match = _find_point(pt_index, d_key, exclude_cat=stop_cat_id)
            if d_match:
                stops.append(Stop(
                    original_name=d_match[2] or dest_name,
                    structure_name=d_match[2] or dest_name,
                    stop_type=StopType.DESTINATION,
                    stop_number=0,
                    point_layer_id=d_match[0],
                    point_feature_id=d_match[1],
                ))
            else:
                stops.append(Stop(
                    original_name=dest_name,
                    structure_name=dest_name,
                    stop_type=StopType.DESTINATION,
                    stop_number=0,
                ))

            route = Route(
                line_name=line_name,
                origin=stops[0].structure_name,
                destination=stops[-1].structure_name,
                stops=stops,
                line_layer_id=layer.id(),
                line_feature_id=feat.id(),
                category_name=lm.category_name,
            )
            routes.append(route)

            _log(f"Reconstructed route '{line_name}': "
                 f"{len(stops)} total stops "
                 f"({route.active_stop_count} intermediate)")

    _log(f"reconstruct_routes: {len(routes)} route(s) reconstructed")
    return routes


def _find_point(
    pt_index: Dict[Tuple[float, float], List[Tuple[str, int, str, str]]],
    key: Tuple[float, float],
    prefer_cat: str = "",
    exclude_cat: str = "",
) -> Optional[Tuple[str, int, str]]:
    """Find a point feature at the given coordinate key.

    Returns ``(layer_id, feature_id, name)`` or *None*.

    *prefer_cat*: if set, prefer matches from this category.
    *exclude_cat*: if set, exclude matches from this category (with
    fallback to any match if all were excluded).
    """
    candidates = pt_index.get(key)
    if not candidates:
        return None

    if exclude_cat:
        filtered = [c for c in candidates if c[3] != exclude_cat]
    else:
        filtered = list(candidates)

    if not filtered:
        filtered = list(candidates)  # fallback

    if prefer_cat:
        preferred = [c for c in filtered if c[3] == prefer_cat]
        if preferred:
            lid, fid, name, _ = preferred[0]
            return (lid, fid, name)

    lid, fid, name, _ = filtered[0]
    return (lid, fid, name)


def _safe_feat_attr(feat: QgsFeature, field_name: str) -> str:
    """Read a feature attribute safely, returning '' for missing/NULL."""
    if not field_name:
        return ""
    try:
        val = feat[field_name]
    except Exception:
        return ""
    if val is None:
        return ""
    s = str(val)
    if s in ("NULL", "None"):
        return ""
    return s


# ------------------------------------------------------------------
# Field name maps: field_N → real name
# ------------------------------------------------------------------

def _build_field_name_maps(
    categories: List[Category],
) -> Dict[str, Dict[str, str]]:
    """Build {cat_id: {field_1: "RealName", field_2: ...}} for each category.

    The server stores values under field_1, field_2, etc. — we reverse-map
    these to human-readable names using the category's field list.
    """
    result: Dict[str, Dict[str, str]] = {}
    for cat in categories:
        fmap: Dict[str, str] = {}

        # Origin/main fields
        n = 0
        for cf in cat.fields:
            if cf.name.lower() in _SINGLE_KNOWN | _DUAL_ORIGIN_KNOWN:
                continue
            n += 1
            fmap[f"field_{n}"] = cf.name

        # Destination fields (dual categories)
        dn = 0
        for cf in cat.destination_fields:
            if cf.name.lower() in _DUAL_DEST_KNOWN:
                continue
            dn += 1
            fmap[f"destination_field_{dn}"] = cf.name

        result[cat.category_id] = fmap
    return result


def _category_field_names(
    cat: Optional[Category],
    geom_type: str,
    fmap: Dict[str, str],
) -> List[str]:
    """Return the extra field names (beyond essentials) for a category."""
    names: List[str] = []
    seen: set = set()

    # Add fields from the reverse map
    for key in sorted(fmap.keys()):
        name = fmap[key]
        if name not in seen:
            names.append(name)
            seen.add(name)

    return names


# ------------------------------------------------------------------
# Feature builders
# ------------------------------------------------------------------

def _build_point_feature(
    sloc: dict,
    fmap: Dict[str, str],
    extra_field_names: List[str],
) -> QgsFeature:
    """Build a QgsFeature (Point) from a singleLOC API object."""
    all_fields = list(_POINT_ESSENTIAL) + extra_field_names
    fields = _make_qgs_fields(all_fields)
    feat = QgsFeature(fields)

    # Geometry
    lon = _float(sloc.get("origin_longitude", 0))
    lat = _float(sloc.get("origin_latitude", 0))
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))

    # Essential tracking attributes
    feat.setAttribute("_loc_id", sloc.get("loc_id", ""))
    feat.setAttribute("_origin_id", sloc.get("origin_id", ""))
    # Store original coordinate strings for bit-exact round-trip
    feat.setAttribute("_origin_lon", str(sloc.get("origin_longitude", "")))
    feat.setAttribute("_origin_lat", str(sloc.get("origin_latitude", "")))
    feat.setAttribute("Unique Asset Identifier",
                       sloc.get("unique_asset_id", ""))
    feat.setAttribute("Actual Asset Name",
                       sloc.get("actual_asset_name", ""))
    # Store raw server JSON for round-trip fidelity on push
    feat.setAttribute("_sloc_json", _json.dumps(sloc, default=str))

    # Category-specific fields via reverse map
    _populate_numbered_fields(feat, sloc, fmap, extra_field_names)

    return feat


def _build_line_feature(
    dloc: dict,
    fmap: Dict[str, str],
    extra_field_names: List[str],
    extra_coords: Optional[List[Tuple[float, float]]] = None,
    multi_id: str = "",
    stop_ids: str = "",
) -> QgsFeature:
    """Build a QgsFeature (LineString) from a dualLOC API object."""
    all_fields = list(_LINE_ESSENTIAL) + extra_field_names
    fields = _make_qgs_fields(all_fields)
    feat = QgsFeature(fields)

    # Origin coords
    o_lon = _float(dloc.get("origin_longitude", 0))
    o_lat = _float(dloc.get("origin_latitude", 0))

    # Destination coords — try multiple possible key structures
    dest = (dloc.get("LOCDestination")
            or dloc.get("locDestination")
            or dloc.get("destination")
            or {})
    # If dest is a string (the destination name), not a dict
    if not isinstance(dest, dict):
        dest = {}
    d_lon = _float(dest.get("longitude", 0))
    d_lat = _float(dest.get("latitude", 0))

    # Build polyline: origin → stops → destination
    points = [QgsPointXY(o_lon, o_lat)]
    if extra_coords:
        for lon, lat in extra_coords:
            points.append(QgsPointXY(lon, lat))
    points.append(QgsPointXY(d_lon, d_lat))

    _log(f"Line feature: {len(points)} points, "
         f"origin=({o_lon:.6f},{o_lat:.6f}), "
         f"dest=({d_lon:.6f},{d_lat:.6f}), "
         f"extra_coords={len(extra_coords or [])}")
    feat.setGeometry(QgsGeometry.fromPolylineXY(points))

    # Essential tracking attributes
    feat.setAttribute("_loc_id", dloc.get("loc_id", ""))
    feat.setAttribute("_origin_id", dloc.get("origin_id", ""))
    feat.setAttribute("_dest_id", dest.get("destination_id", ""))
    feat.setAttribute("_multi_id", multi_id)
    feat.setAttribute("_stop_ids", stop_ids)
    # Store original coordinate strings for bit-exact round-trip
    feat.setAttribute("_origin_lon", str(dloc.get("origin_longitude", "")))
    feat.setAttribute("_origin_lat", str(dloc.get("origin_latitude", "")))
    feat.setAttribute("_dest_lon", str(dest.get("longitude", "")))
    feat.setAttribute("_dest_lat", str(dest.get("latitude", "")))
    feat.setAttribute("Route ID", dloc.get("unique_asset_id", ""))
    feat.setAttribute("Actual Asset Name",
                       dloc.get("actual_asset_name", ""))
    feat.setAttribute("Destination", dest.get("destination", ""))
    # Store raw server JSON for round-trip fidelity on push
    feat.setAttribute("_dloc_json", _json.dumps(dloc, default=str))

    # Category-specific fields via reverse map (origin side)
    _populate_numbered_fields(feat, dloc, fmap, extra_field_names)

    # Destination-side fields
    # The destination_fields sub-dict may use "field_N" keys (legacy) or
    # human-readable name keys (current).  Our fmap stores destination
    # field names under "destination_field_N".
    dest_fields = dest.get("destination_fields", {})
    if isinstance(dest_fields, dict):
        for key, val in dest_fields.items():
            if key.startswith("field_"):
                # field_N → destination_field_N for fmap lookup
                dest_key = f"destination_{key}"
                real_name = fmap.get(dest_key, "")
            else:
                # Key is already a human-readable name (e.g. "Length")
                real_name = key if key in extra_field_names else ""
            if real_name and real_name in extra_field_names:
                if val:
                    feat.setAttribute(real_name, _clean_val(val))

    return feat


def _populate_numbered_fields(
    feat: QgsFeature,
    loc_obj: dict,
    fmap: Dict[str, str],
    extra_field_names: List[str],
) -> None:
    """Set feature attributes from field_1, field_2, etc. in the API object."""
    # Top-level field_N values
    for key, real_name in fmap.items():
        if real_name not in extra_field_names:
            continue
        val = loc_obj.get(key, "")
        if val:
            feat.setAttribute(real_name, _clean_val(val))

    # Also check nested "fields" dict — server may store values under
    # field_N keys (legacy) or human-readable name keys (current).
    fields_dict = loc_obj.get("fields", {})
    if isinstance(fields_dict, dict):
        for key, val in fields_dict.items():
            # Try field_N -> real name mapping first
            real_name = fmap.get(key, "")
            if real_name and real_name in extra_field_names:
                if val:
                    feat.setAttribute(real_name, _clean_val(val))
                continue
            # Key might already be the human-readable field name
            if key in extra_field_names:
                if val:
                    feat.setAttribute(key, _clean_val(val))


def _sorted_stop_coords(
    stops: List[dict],
) -> List[Tuple[float, float]]:
    """Extract stop coordinates, sorted by stopNumber.

    Keeps all vertices including duplicates from ingress/egress pairs
    so the polyline faithfully represents the server's stop structure.
    Reconstruction detects consecutive duplicates as IN/OUT pairs.
    """
    ordered = sorted(stops, key=lambda s: s.get("stopNumber", 0))
    coords: List[Tuple[float, float]] = []
    for stop in ordered:
        sloc = (stop.get("singleLoc")
                or stop.get("singleLOC")
                or stop.get("single_loc")
                or {})
        lon = _float(sloc.get("origin_longitude", 0))
        lat = _float(sloc.get("origin_latitude", 0))
        if lon != 0 or lat != 0:
            coords.append((lon, lat))
    return coords


# ------------------------------------------------------------------
# Memory layer creation
# ------------------------------------------------------------------

def _make_qgs_fields(field_names: List[str]) -> QgsFields:
    """Create QgsFields from a list of string field names."""
    qfields = QgsFields()
    for name in field_names:
        qfields.append(QgsField(name, QVariant.String))
    return qfields


def _create_memory_layer(
    name: str,
    geom_type: str,
    field_names: List[str],
    features: List[QgsFeature],
) -> QgsVectorLayer:
    """Create an in-memory QgsVectorLayer and populate it.

    Fields are added via ``addAttributes`` (not the URI) to avoid
    encoding issues with field names containing spaces.  Features
    are rebuilt using the layer's own QgsFields to guarantee schema
    compatibility with the provider.
    """
    uri = f"{geom_type}?crs=EPSG:4326"
    layer = QgsVectorLayer(uri, name, "memory")

    dp = layer.dataProvider()
    dp.addAttributes([QgsField(fn, QVariant.String) for fn in field_names])
    layer.updateFields()

    # Rebuild each feature with the provider's own field schema
    # so addFeatures doesn't reject them for schema mismatch.
    provider_fields = dp.fields()
    remapped: List[QgsFeature] = []
    for feat in features:
        nf = QgsFeature(provider_fields)
        nf.setGeometry(feat.geometry())
        src_fields = feat.fields()
        for i in range(src_fields.count()):
            fname = src_fields.at(i).name()
            dst_idx = provider_fields.indexOf(fname)
            if dst_idx >= 0:
                nf.setAttribute(dst_idx, feat.attribute(i))
        remapped.append(nf)

    ok, added = dp.addFeatures(remapped)
    _log(f"addFeatures('{name}'): ok={ok}, "
         f"requested={len(remapped)}, added={len(added)}, "
         f"provider={dp.featureCount()}")
    layer.updateExtents()

    return layer


# ------------------------------------------------------------------
# Auto-mapping generation
# ------------------------------------------------------------------

def _auto_mapping_point(
    layer: QgsVectorLayer,
    cat_id: str,
    cat_name: str,
    field_names: List[str],
) -> LayerMapping:
    """Build a LayerMapping for an imported point layer."""
    fms = []
    for fn in field_names:
        if fn.startswith("_"):
            continue  # skip hidden tracking fields
        fms.append(FieldMapping(loc_field=fn, qgis_field=fn))

    return LayerMapping(
        layer_id=layer.id(),
        layer_name=layer.name(),
        category_id=cat_id,
        category_name=cat_name,
        field_mappings=fms,
    )


def _auto_mapping_line(
    layer: QgsVectorLayer,
    cat_id: str,
    cat_name: str,
    field_names: List[str],
    stop_cat_id: str = "",
    stop_cat_name: str = "",
) -> LayerMapping:
    """Build a LayerMapping for an imported line layer."""
    fms = []
    for fn in field_names:
        if fn.startswith("_"):
            continue
        fms.append(FieldMapping(loc_field=fn, qgis_field=fn))

    return LayerMapping(
        layer_id=layer.id(),
        layer_name=layer.name(),
        category_id=cat_id,
        category_name=cat_name,
        field_mappings=fms,
        stop_category_id=stop_cat_id,
        stop_category_name=stop_cat_name,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _clean_val(val) -> str:
    """Convert a value to string, treating None/NULL as empty."""
    if val is None:
        return ""
    s = str(val)
    if s in ("NULL", "N/A", "None"):
        return ""
    return s
