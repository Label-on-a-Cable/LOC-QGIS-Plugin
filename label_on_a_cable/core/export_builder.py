"""Export payload builder for LOCs/plugin-v3.

Builds the full push payload matching the server's expected structure.

Top-level envelope::

    {
        "location_id":      str,
        "organization_id":  str,
        "multiLocs":        [...],   # routes WITH stops
        "dual_locs":        [...],   # routes WITHOUT stops
        "single_locs":      [...],   # standalone point assets
        "Location": { "center": {"x": lon, "y": lat}, "radius": float,
                      "name": str, "is_update": bool }
    }

multiLocs entry:
    { "id": UUID, "multiName": str,
      "Stops": [{"stop_id": UUID, "stopNumber": int, "singleLoc": {...}}],
      "dualLoc": { ...dual LOC object... } }

dual_locs entry (same shape as a dualLoc object):
    { "loc_id": UUID, "unique_asset_id": str, "origin_id": UUID,
      "actual_asset_name": str, ..., "LOCDestination": {...},
      "category": UUID, "category_name": str, "category_type": "dual" }

All coordinates are WGS 84 (EPSG:4326).
All IDs are v4 UUIDs.
"""

import json as _json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsMessageLog,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..models.category import Category, CategoryField
from ..models.location import Location
from ..models.mapping import FieldMapping, LayerMapping
from ..models.route import Route, Stop, StopType

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

# Field names that map to dedicated top-level keys (case-insensitive).
# These are excluded from the numbered field_N / destination_field_N keys.
_SINGLE_KNOWN = {"unique asset identifier", "actual asset name"}
_DUAL_ORIGIN_KNOWN = {"route id", "origin", "unique asset identifier",
                      "actual asset name"}
_DUAL_DEST_KNOWN = {"destination"}


# ------------------------------------------------------------------
# Type-normalizing comparison helpers
# ------------------------------------------------------------------

def _norm_val(v) -> str:
    """Normalize a value for comparison: stringify, treat null/N-A/empty as sentinel."""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("N/A", "NULL", "None", ""):
        return ""
    return s


def _norm_coord(v) -> float:
    """Normalize a coordinate value to float for comparison."""
    try:
        return round(float(v), 8)
    except (TypeError, ValueError):
        return 0.0


def _fields_equal(a: dict, b: dict) -> bool:
    """Compare two field dicts with type-normalized values.

    Handles raw JSON types (int, float, null) vs QGIS string types.
    """
    if a is None and b is None:
        return True
    a = a or {}
    b = b or {}
    all_keys = set(a.keys()) | set(b.keys())
    for k in all_keys:
        if _norm_val(a.get(k)) != _norm_val(b.get(k)):
            return False
    return True


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def build_payload(
    routes: List[Route],
    layer_mappings: List[LayerMapping],
    categories: Dict[str, Category],
    location: Location,
    user_id: str,
    organization_id: str,
    pulled_loc_ids: Optional[set] = None,
) -> dict:
    """Build the complete LOCs/plugin-v3 push payload.

    If *pulled_loc_ids* is provided (non-empty set), delete detection is
    enabled: any loc_id that was pulled but is no longer present in the
    payload will be added to a ``deleted_locs`` array.
    """
    project = QgsProject.instance()
    now = _now_iso()

    mapping_by_layer: Dict[str, LayerMapping] = {
        lm.layer_id: lm for lm in layer_mappings
    }
    transforms = _build_wgs84_transforms(project, layer_mappings)

    multi_locs: List[dict] = []
    dual_locs_list: List[dict] = []

    for route in routes:
        line_mapping = mapping_by_layer.get(route.line_layer_id)
        if not line_mapping:
            continue

        line_feature = _get_feature(
            project, route.line_layer_id, route.line_feature_id,
        )
        line_cat = categories.get(line_mapping.category_id)

        # Use the actual origin/destination POINT feature coordinates,
        # not the line endpoints (which may extend beyond the structures).
        origin_xy, dest_xy = _route_endpoint_coords(
            route, project, transforms,
        )

        dual_loc = _build_dual_loc_obj(
            route, line_feature, line_mapping, line_cat,
            origin_xy, dest_xy,
            location.location_id, user_id, now,
        )

        intermediates = route.intermediate_stops
        if not intermediates:
            # 0 stops → standalone dual_loc
            # Always include — server deletes LOCs not in the push.
            dual_locs_list.append(dual_loc)
        else:
            # ≥1 stop → multiLoc
            multi_id = ""
            if line_feature is not None:
                multi_id = _safe_value(line_feature, "_multi_id")

            # Fast path: use raw multiLoc JSON as base
            raw_mloc = _get_raw_json(line_feature, "_mloc_json")
            if raw_mloc is not None:
                mloc_entry = dict(raw_mloc)
                # Remove case-variant keys before setting canonical ones.
                # API may return "stops"/"dualLOC"/"dual_loc" etc.
                for k in list(mloc_entry.keys()):
                    kl = k.lower()
                    if kl == "stops" or kl == "dualloc" or kl == "dual_loc":
                        del mloc_entry[k]
                mloc_entry["dualLoc"] = dual_loc  # already patched
                mloc_entry["multiName"] = route.line_name
                mloc_entry["id"] = multi_id or mloc_entry.get("id", str(uuid4()))
                rebuilt_stops = _rebuild_stops_from_raw(
                    raw_mloc, intermediates, route, mapping_by_layer,
                    categories, line_mapping, line_feature, transforms,
                    location, user_id, now,
                )
                mloc_entry["Stops"] = rebuilt_stops

                # Always include — server deletes LOCs not in the push.
                multi_locs.append(mloc_entry)
            else:
                # From-scratch path (locally created features)
                stop_cat = categories.get(line_mapping.stop_category_id)
                route_uid = dual_loc.get("unique_asset_id", route.line_name)

                # Parse coord→ID map from pulled line feature so we can
                # reuse original server IDs for unchanged stops.
                stop_id_map: dict = {}
                orig_stop_ids: List[str] = []
                if line_feature is not None:
                    raw_ids = _safe_value(line_feature, "_stop_loc_ids")
                    if raw_ids:
                        stop_id_map = _parse_stop_loc_ids(raw_ids)
                    raw_stop_ids = _safe_value(line_feature, "_stop_ids")
                    if raw_stop_ids:
                        orig_stop_ids = raw_stop_ids.split(",")

                stops_arr: List[dict] = []
                for idx, stop in enumerate(intermediates, start=1):
                    pt_mapping = mapping_by_layer.get(stop.point_layer_id)
                    pt_feature = _get_feature(
                        project, stop.point_layer_id, stop.point_feature_id,
                    )
                    pt_xy = _point_wgs84(
                        pt_feature, stop.point_layer_id, transforms,
                    )

                    single_loc = _build_single_loc_obj(
                        stop, route, pt_feature, pt_mapping,
                        stop_cat, line_mapping, line_feature,
                        route_uid, pt_xy, idx,
                        location.location_id, user_id, now,
                        stop_id_map=stop_id_map,
                    )
                    # Reuse original stop_id if available (round-trip)
                    sid = (orig_stop_ids[idx - 1]
                           if idx - 1 < len(orig_stop_ids)
                           else str(uuid4()))
                    stops_arr.append({
                        "stop_id": sid or str(uuid4()),
                        "stopNumber": idx,
                        "singleLoc": single_loc,
                    })

                multi_locs.append({
                    "id": multi_id or str(uuid4()),
                    "multiName": route.line_name,
                    "Stops": stops_arr,
                    "dualLoc": dual_loc,
                })

    # Standalone single LOCs — point features from single-type categories
    single_locs_list = _build_standalone_single_locs(
        project, layer_mappings, categories, transforms,
        location.location_id, user_id, now,
    )

    payload = {
        "location_id": location.location_id,
        "organization_id": organization_id,
        "multiLocs": multi_locs,
        "dual_locs": dual_locs_list,
        "single_locs": single_locs_list,
        "Location": {
            "center": {"x": location.longitude, "y": location.latitude},
            "radius": location.radius,
            "name": location.name,
            "is_update": True,
        },
    }

    return payload


def payload_summary(payload: dict) -> dict:
    """Return a quick summary of a payload for preview display."""
    multi = payload.get("multiLocs", [])
    dual = payload.get("dual_locs", [])
    single = payload.get("single_locs", [])
    total_stops = sum(len(m.get("Stops", [])) for m in multi)
    # Total LOCs: each multiLoc has 1 dualLoc + N singleLoc stops,
    # each standalone dual_loc = 1, each single_loc = 1.
    total_locs = (
        sum(1 + len(m.get("Stops", [])) for m in multi)
        + len(dual)
        + len(single)
    )
    return {
        "location": payload.get("Location", {}).get("name", ""),
        "routes_with_stops": len(multi),
        "routes_without_stops": len(dual),
        "total_routes": len(multi) + len(dual),
        "total_stops": total_stops,
        "standalone_assets": len(single),
        "total_locs": total_locs,
    }


def validate_payload(
    payload: dict,
    expected_stop_counts: Optional[Dict[str, int]] = None,
    counts_authoritative: bool = False,
) -> list:
    """Preflight invariant validation before push.

    Checks structural correctness only — no size-based limits.

    Returns a list of ``(level, message)`` tuples:
    - ``"error"``   → blocks push
    - ``"warning"`` → logged, user may proceed

    Parameters
    ----------
    payload : dict
        The push payload from ``build_payload()``.
    expected_stop_counts : dict, optional
        ``{multiLoc_id: expected_stop_count}`` from pulled data.
        When provided, verifies that per-route stop counts were
        preserved on a pull→push round-trip (no regeneration).
    counts_authoritative : bool
        If *True*, stop-count mismatches are hard errors (source is
        raw multiLoc data with ghost-stop filtering).
        If *False*, they are warnings only (source is derived layer
        attributes which may not match export counting rules).
    """
    issues: list = []
    multi = payload.get("multiLocs", [])

    # Invariant 1: A stop singleLoc.loc_id may belong to exactly ONE
    # multiLoc route (no duplicates across routes).
    seen_loc_ids: dict = {}
    for ml in multi:
        name = ml.get("multiName", "?")
        for stop in ml.get("Stops", []):
            sl = stop.get("singleLoc", {})
            lid = sl.get("loc_id", "")
            if not lid:
                continue
            if lid in seen_loc_ids:
                issues.append((
                    "error",
                    f"Duplicate stop loc_id {lid[:12]}... "
                    f"in routes '{seen_loc_ids[lid]}' and '{name}'",
                ))
            else:
                seen_loc_ids[lid] = name

    # Invariant 2: Stop ordering must be deterministic and sequential
    # (stopNumber = 1, 2, ..., N with no gaps).
    for ml in multi:
        name = ml.get("multiName", "?")
        stops = ml.get("Stops", [])
        numbers = [s.get("stopNumber", 0) for s in stops]
        expected_seq = list(range(1, len(stops) + 1))
        if numbers != expected_seq:
            issues.append((
                "error",
                f"Route '{name}' has non-sequential stop ordering: "
                f"{numbers} (expected {expected_seq})",
            ))

    # Invariant 3: On pull→push with no regeneration, per-route stop
    # counts must remain unchanged.
    if expected_stop_counts:
        level = "error" if counts_authoritative else "warning"
        for ml in multi:
            mid = ml.get("id", "")
            name = ml.get("multiName", "?")
            if mid and mid in expected_stop_counts:
                expected_n = expected_stop_counts[mid]
                actual_n = len(ml.get("Stops", []))
                if actual_n != expected_n:
                    issues.append((
                        level,
                        f"Route '{name}' stop count changed: "
                        f"expected {expected_n} (from pull), got {actual_n}.",
                    ))

    # Invariant 4: multiLoc.id and dualLoc.loc_id must be non-empty
    # (required for server to identify updates vs creates).
    for ml in multi:
        name = ml.get("multiName", "?")
        if not ml.get("id"):
            issues.append((
                "warning",
                f"Route '{name}' has no multiLoc ID — "
                f"server will treat this as a new route.",
            ))
        dl = ml.get("dualLoc", {})
        if not dl.get("loc_id"):
            issues.append((
                "error",
                f"Route '{name}' dualLoc is missing loc_id.",
            ))

    for dl in payload.get("dual_locs", []):
        uid = dl.get("unique_asset_id", "?")
        if not dl.get("loc_id"):
            issues.append((
                "error",
                f"Standalone dual LOC '{uid}' is missing loc_id.",
            ))

    # Invariant 5: Standalone singleLOC unique_asset_id values must be
    # unique.  The server silently deduplicates by this key — duplicates
    # cause entries to be dropped without error.
    sloc_uid_counts: dict = {}
    for sl in payload.get("single_locs", []):
        uid = sl.get("unique_asset_id", "")
        if uid:
            sloc_uid_counts[uid] = sloc_uid_counts.get(uid, 0) + 1
    for uid, count in sloc_uid_counts.items():
        if count > 1:
            issues.append((
                "warning",
                f"Standalone single LOC unique_asset_id '{uid}' is used "
                f"by {count} features — the server will keep only one and "
                f"silently drop the other {count - 1}. Check your Unique "
                f"Asset Identifier mapping / attribute values.",
            ))

    return issues


def extract_expected_stop_counts(
    routes: List[Route],
) -> Dict[str, int]:
    """Build a map of ``{multiLoc_id: expected_stop_count}`` from routes.

    Reads the ``_multi_id`` and ``_stop_ids`` hidden attributes on
    pulled line features to determine the original stop counts.
    Only routes with pulled IDs are included.
    """
    project = QgsProject.instance()
    result: Dict[str, int] = {}
    for route in routes:
        feat = _get_feature(project, route.line_layer_id,
                            route.line_feature_id)
        if feat is None:
            continue
        multi_id = _safe_value(feat, "_multi_id")
        raw_stop_ids = _safe_value(feat, "_stop_ids")
        if multi_id and raw_stop_ids:
            result[multi_id] = len(
                [s for s in raw_stop_ids.split(",") if s]
            )
    return result


def log_payload_metrics(payload: dict) -> str:
    """Return a human-readable summary of payload metrics for logging.

    Includes counts, per-route stop breakdown, payload byte-size,
    and duplicate stop loc_id detection.
    """
    multi = payload.get("multiLocs", [])
    dual = payload.get("dual_locs", [])
    single = payload.get("single_locs", [])

    lines = [
        "Push payload metrics:",
        f"  multiLocs: {len(multi)}",
        f"  dual_locs: {len(dual)}",
        f"  single_locs: {len(single)}",
    ]

    total_stops = 0
    for ml in multi:
        name = ml.get("multiName", "?")
        n_stops = len(ml.get("Stops", []))
        total_stops += n_stops
        lines.append(f"  Route '{name}': {n_stops} stop(s)")
    lines.append(f"  Total stops: {total_stops}")

    # Payload size
    payload_bytes = len(_json.dumps(payload, default=str).encode())
    lines.append(f"  Payload size: {payload_bytes / 1024:.1f} KB")

    # Duplicate stop loc_ids
    all_stop_lids: list = []
    for ml in multi:
        for stop in ml.get("Stops", []):
            sl = stop.get("singleLoc", {})
            lid = sl.get("loc_id", "")
            if lid:
                all_stop_lids.append(lid)
    n_dups = len(all_stop_lids) - len(set(all_stop_lids))
    if n_dups:
        lines.append(f"  DUPLICATE stop loc_ids: {n_dups}")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Dual LOC object builder
# ------------------------------------------------------------------

def _build_dual_loc_obj(
    route: Route,
    line_feature: Optional[QgsFeature],
    mapping: LayerMapping,
    category: Optional[Category],
    origin_xy: Tuple[float, float],
    dest_xy: Tuple[float, float],
    location_id: str,
    user_id: str,
    now: str,
) -> dict:
    """Build one dualLoc object (used in both multiLocs and dual_locs)."""
    # Fast path: use raw server JSON as base for round-trip fidelity
    raw = _get_raw_json(line_feature, "_dloc_json")
    if raw is not None:
        return _patch_dual_loc(raw, route, line_feature, mapping, category,
                               origin_xy, dest_xy, now, location_id)

    loc_id = _get_tracking_id(line_feature, "_loc_id")
    origin_id = _get_tracking_id(line_feature, "_origin_id")
    dest_id = _get_tracking_id(line_feature, "_dest_id")

    # Numbered fields for origin side
    # Top-level keys are field_N; sub-dict uses human-readable names.
    cat_fields = category.fields if category else []
    origin_top, origin_sub = _numbered_fields(
        cat_fields, mapping.field_mappings, line_feature,
        _DUAL_ORIGIN_KNOWN, top_prefix="field",
    )

    # Numbered fields for destination side
    # Top-level keys are destination_field_N; sub-dict uses human-readable names.
    cat_dest_fields = category.destination_fields if category else []
    dest_top, dest_sub = _numbered_fields(
        cat_dest_fields, mapping.field_mappings, line_feature,
        _DUAL_DEST_KNOWN, top_prefix="destination_field",
    )

    # Resolve unique_asset_id from mapped "Route ID" field, fallback to route name
    route_id_qfield = mapping.qgis_field_for("Route ID")
    unique_asset_id = ""
    if route_id_qfield and line_feature is not None:
        unique_asset_id = _safe_value(line_feature, route_id_qfield)
    if not unique_asset_id:
        unique_asset_id = route.line_name

    # Resolve actual_asset_name from mapped "Actual Asset Name" field
    aan_qfield = mapping.qgis_field_for("Actual Asset Name")
    actual_asset_name = ""
    if aan_qfield and line_feature is not None:
        actual_asset_name = _safe_value(line_feature, aan_qfield)
    if not actual_asset_name:
        actual_asset_name = route.origin

    # Resolve destination from mapped "Destination" field
    dest_qfield = mapping.qgis_field_for("Destination")
    destination = ""
    if dest_qfield and line_feature is not None:
        destination = _safe_value(line_feature, dest_qfield)
    if not destination:
        destination = route.destination

    entry = {
        "loc_id": loc_id,
        "unique_asset_id": unique_asset_id,
        "origin_id": origin_id,
        "actual_asset_name": actual_asset_name,
        "is_flagged": False,
        "notes": None,
        "notes_history": [],
        "imageNotes": [],
        "audioNotes": [],
        "videoNotes": [],
        "origin_status": "unassigned",
        "LOC_type": "dual",
        "category": mapping.category_id,
        "fields": origin_sub,
        "hard_lock": 0,
        "origin_longitude": origin_xy[0],
        "origin_latitude": origin_xy[1],
        "origin_radius": None,
        "transaction_id": None,
        "createdAt": now,
        "updatedAt": now,
        "location_id": location_id,
        "user_id": user_id,
        "LOCDestination": {
            "destination_id": dest_id,
            "loc_id": loc_id,
            "destination": destination,
            "longitude": dest_xy[0],
            "latitude": dest_xy[1],
            "radius": None,
            "destination_status": "unassigned",
            "hard_lock": 0,
            "destination_transaction_id": None,
            "destination_fields": dest_sub,
            "createdAt": now,
            "updatedAt": now,
        },
        "category_name": mapping.category_name,
        "category_type": "dual",
    }

    # Merge numbered top-level field keys
    entry.update(origin_top)
    entry["LOCDestination"].update(dest_top)

    return entry


# ------------------------------------------------------------------
# Single LOC object builder  (stop within a multiLoc)
# ------------------------------------------------------------------

def _build_single_loc_obj(
    stop: Stop,
    route: Route,
    pt_feature: Optional[QgsFeature],
    pt_mapping: Optional[LayerMapping],
    stop_category: Optional[Category],
    line_mapping: LayerMapping,
    line_feature: Optional[QgsFeature],
    route_uid: str,
    pt_xy: Tuple[float, float],
    stop_number: int,
    location_id: str,
    user_id: str,
    now: str,
    stop_id_map: Optional[Dict[Tuple[float, float], list]] = None,
) -> dict:
    """Build one singleLoc object for a stop inside a multiLoc.

    Uses the *stop category* (chosen on the line layer mapping) rather
    than the point layer's own category, so stops are distinct from
    standalone single LOCs for the same physical asset.

    Field values are read from the LINE feature via stop_field_mappings.
    Essential fields (uid/name) use stop_field_mappings first, then fall
    back to the point layer's mapping for the asset identity.

    If *stop_id_map* is provided (from a pulled line feature's
    ``_stop_loc_ids`` attribute), original server IDs and exact
    coordinates are reused for stops — preserving audit history and
    avoiding precision drift on round-trip.
    """
    # Try to reuse server IDs + exact coords from pulled stop data
    loc_id = ""
    origin_id = ""
    exact_lon = 0.0
    exact_lat = 0.0
    if stop_id_map:
        coord_key = (round(pt_xy[0], 8), round(pt_xy[1], 8))
        id_list = stop_id_map.get(coord_key)
        if id_list:
            entry = id_list.pop(0)
            loc_id = entry[0]
            origin_id = entry[1] if len(entry) > 1 else ""
            exact_lon = entry[2] if len(entry) > 2 else 0.0
            exact_lat = entry[3] if len(entry) > 3 else 0.0
    if not loc_id:
        loc_id = str(uuid4())
    if not origin_id:
        origin_id = str(uuid4())
    # Use exact server coordinates if available, else point feature
    if exact_lon and exact_lat:
        pt_xy = (exact_lon, exact_lat)

    # Stop category comes from the line layer's mapping
    cat_id = stop_category.category_id if stop_category else ""
    cat_name = stop_category.name if stop_category else ""
    cat_fields = stop_category.fields if stop_category else []

    # Numbered fields — use stop category fields + stop_field_mappings,
    # reading values from the LINE feature.
    # Stop singleLocs are singleLOC objects — use named keys like
    # standalone singleLOCs (server processes them the same way).
    stop_fm = line_mapping.stop_field_mappings
    top_fields, sub_fields = _numbered_fields(
        cat_fields, stop_fm, line_feature,
        _SINGLE_KNOWN | _DUAL_ORIGIN_KNOWN, top_prefix="field",
    )

    # Essential fields (uid / name) ALWAYS come from the POINT feature
    # (the physical asset at the stop), never from the line feature.
    # All stops on a route share the same line feature, so reading from
    # it would give every stop the same identity.
    pt_uid_qfield = pt_mapping.qgis_field_for("Unique Asset Identifier") if pt_mapping else ""
    asset_uid = _safe_value(pt_feature, pt_uid_qfield) if pt_uid_qfield and pt_feature else ""
    if not asset_uid:
        pt_name_qfield = pt_mapping.qgis_field_for("Actual Asset Name") if pt_mapping else ""
        asset_uid = _safe_value(pt_feature, pt_name_qfield) if pt_name_qfield and pt_feature else ""
    if not asset_uid:
        asset_uid = stop.display_name

    pt_aan_qfield = pt_mapping.qgis_field_for("Actual Asset Name") if pt_mapping else ""
    actual_asset_name = _safe_value(pt_feature, pt_aan_qfield) if pt_aan_qfield and pt_feature else ""
    if not actual_asset_name:
        actual_asset_name = asset_uid

    # Composite unique_asset_id: route ID + asset ID (guaranteed unique)
    unique_asset_id = f"{route_uid}_{asset_uid}" if route_uid else asset_uid

    entry = {
        "loc_id": loc_id,
        "unique_asset_id": unique_asset_id,
        "origin_id": origin_id,
        "actual_asset_name": actual_asset_name,
        "is_flagged": False,
        "notes": None,
        "notes_history": [],
        "imageNotes": [],
        "audioNotes": [],
        "videoNotes": [],
        "origin_status": "unassigned",
        "LOC_type": "single",
        "category": cat_id,
        "fields": sub_fields,
        "hard_lock": 0,
        "origin_longitude": pt_xy[0],
        "origin_latitude": pt_xy[1],
        "origin_radius": None,
        "transaction_id": None,
        "createdAt": now,
        "updatedAt": now,
        "location_id": location_id,
        "user_id": user_id,
        "category_name": cat_name,
        "category_type": "single",
        "name": f"Stop_{stop_number}_{route.line_name}",
    }

    # Merge numbered top-level field keys
    entry.update(top_fields)

    return entry


# ------------------------------------------------------------------
# Standalone single LOCs  (point features not part of a route)
# ------------------------------------------------------------------

def _build_standalone_single_locs(
    project: QgsProject,
    layer_mappings: List[LayerMapping],
    categories: Dict[str, Category],
    transforms: Dict[str, Optional[QgsCoordinateTransform]],
    location_id: str,
    user_id: str,
    now: str,
) -> List[dict]:
    """Build single_locs entries for every feature in point layers
    mapped to single-type categories."""
    entries: List[dict] = []

    for lm in layer_mappings:
        cat = categories.get(lm.category_id)
        if not cat or not cat.is_single:
            continue

        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue

        # Only point layers
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            continue

        cat_fields = cat.fields
        uid_qfield = lm.qgis_field_for("Unique Asset Identifier")
        name_qfield = lm.qgis_field_for("Actual Asset Name")
        fallback_qfield = lm.first_mapped_qgis_field()

        for feat in layer.getFeatures():
            if not feat.isValid():
                continue

            pt_xy = _point_wgs84(feat, lm.layer_id, transforms)

            # Fast path: use raw server JSON as base for round-trip fidelity
            raw_sloc = _get_raw_json(feat, "_sloc_json")
            if raw_sloc is not None:
                result = _patch_standalone_sloc(
                    raw_sloc, feat, lm, cat_fields, pt_xy, now,
                    location_id,
                )
                # Always include in payload — server deletes LOCs not
                # present in the push.  updatedAt is preserved from raw
                # JSON so the server won't reprocess unchanged entries.
                _sloc_unchanged(raw_sloc, result["attributes"])  # diagnostic log only
                entries.append(result)
                continue

            unique_asset_id = _safe_value(feat, uid_qfield) if uid_qfield else ""
            actual_asset_name = _safe_value(feat, name_qfield) if name_qfield else ""
            # Fallback: use first mapped field value, then feature ID
            if not unique_asset_id:
                fb = _safe_value(feat, fallback_qfield) if fallback_qfield else ""
                unique_asset_id = actual_asset_name or fb or f"Feature_{feat.id()}"
            if not actual_asset_name:
                actual_asset_name = unique_asset_id

            top_fields, sub_fields = _numbered_fields(
                cat_fields, lm.field_mappings, feat,
                _SINGLE_KNOWN | _DUAL_ORIGIN_KNOWN, top_prefix="field",
            )

            loc_id = _get_tracking_id(feat, "_loc_id")
            origin_id = _get_tracking_id(feat, "_origin_id")

            # Minimal attributes matching the old plugin's format.
            # Server processes single_locs through a separate code path
            # that expects "category_id" (not "category") and a lean
            # attribute set.
            attrs = {
                "loc_id": loc_id,
                "origin_id": origin_id,
                "is_flagged": False,
                "notes": None,
                "origin_status": "unassigned",
                "LOC_type": "single",
                "fields": sub_fields,
                "origin_longitude": pt_xy[0],
                "origin_latitude": pt_xy[1],
                "category_id": lm.category_id,
                "category_name": lm.category_name,
                "category_type": "single",
            }
            attrs.update(top_fields)

            entries.append({
                "id": str(uuid4()),
                "category": lm.category_id,
                "coordinates": [pt_xy[0], pt_xy[1]],
                "unique_asset_id": unique_asset_id,
                "actual_asset_name": actual_asset_name,
                "name": actual_asset_name,
                "fields": sub_fields,
                "attributes": attrs,
            })

    return entries


# ------------------------------------------------------------------
# Raw JSON patching helpers (round-trip fidelity)
# ------------------------------------------------------------------

def _patch_standalone_sloc(
    raw_sloc: dict,
    feat: QgsFeature,
    lm: LayerMapping,
    cat_fields: List[CategoryField],
    pt_xy: Tuple[float, float],
    now: str,
    location_id: str = "",
) -> dict:
    """Patch a raw standalone singleLOC dict with current QGIS values.

    Starts from the original server JSON so all server-only fields
    (QA_reject_reason, imageDescription, sla_fields, notes_history,
    imageNotes, audioNotes, videoNotes, etc.) are automatically preserved.
    Only coordinates and field values are overwritten (updatedAt preserved).
    """
    patched = dict(raw_sloc)

    # Patch coordinates — only overwrite if the point actually moved
    # (>1mm).  Preserving originals avoids false "changed" detection
    # from precision drift through QGIS geometry storage.
    raw_lon = _float_val(raw_sloc.get("origin_longitude", 0))
    raw_lat = _float_val(raw_sloc.get("origin_latitude", 0))
    if (round(raw_lon, 8) != round(pt_xy[0], 8)
            or round(raw_lat, 8) != round(pt_xy[1], 8)):
        patched["origin_longitude"] = pt_xy[0]
        patched["origin_latitude"] = pt_xy[1]

    # Patch fields sub-dict only from current QGIS attributes.
    # Do NOT touch top-level field_N keys — the raw JSON already has them
    # and the category API field order may not match the server's numbering.
    _top_fields, sub_fields = _numbered_fields(
        cat_fields, lm.field_mappings, feat,
        _SINGLE_KNOWN | _DUAL_ORIGIN_KNOWN, top_prefix="field",
        mapped_only=True,
    )
    if sub_fields:
        raw_fields = patched.get("fields")
        if isinstance(raw_fields, dict):
            merged = dict(raw_fields)
            merged.update(sub_fields)
            patched["fields"] = merged
        else:
            patched["fields"] = sub_fields

    # Patch essential identity fields
    uid_qfield = lm.qgis_field_for("Unique Asset Identifier")
    name_qfield = lm.qgis_field_for("Actual Asset Name")
    fallback_qfield = lm.first_mapped_qgis_field()

    unique_asset_id = _safe_value(feat, uid_qfield) if uid_qfield else ""
    actual_asset_name = _safe_value(feat, name_qfield) if name_qfield else ""
    if not unique_asset_id:
        fb = _safe_value(feat, fallback_qfield) if fallback_qfield else ""
        unique_asset_id = actual_asset_name or fb or f"Feature_{feat.id()}"
    if not actual_asset_name:
        actual_asset_name = unique_asset_id

    patched["unique_asset_id"] = unique_asset_id
    patched["actual_asset_name"] = actual_asset_name

    # Preserve original updatedAt from server — only the server should
    # update this timestamp.  Changing it causes the server to reprocess
    # every entry even when nothing else differs, leading to timeouts.

    # Push endpoint expects "category_id" (not "category") for single_locs.
    # The fetch response uses "category"; ensure the push-required key exists.
    patched["category_id"] = lm.category_id
    patched["category_name"] = lm.category_name
    patched["category_type"] = "single"
    patched["LOC_type"] = "single"
    if location_id:
        patched["location_id"] = location_id

    return {
        "id": str(uuid4()),
        "category": lm.category_id,
        "coordinates": [pt_xy[0], pt_xy[1]],
        "unique_asset_id": unique_asset_id,
        "actual_asset_name": actual_asset_name,
        "name": actual_asset_name,
        "fields": patched.get("fields", {}),
        "attributes": patched,
    }


def _sloc_unchanged(raw: dict, patched: dict) -> bool:
    """Return True if no user-editable values changed on a singleLoc.

    Uses type-normalized comparisons so that raw JSON types (int, float,
    null) compare equal to their QGIS string equivalents ("42", "N/A").
    """
    # Coordinates: compare as floats (handles "-6.123" vs -6.123)
    if _norm_coord(raw.get("origin_longitude")) != _norm_coord(patched.get("origin_longitude")):
        QgsMessageLog.logMessage(
            f"singleLoc changed: origin_longitude "
            f"{raw.get('origin_longitude')!r} → {patched.get('origin_longitude')!r}",
            "LOC", Qgis.Info,
        )
        return False
    if _norm_coord(raw.get("origin_latitude")) != _norm_coord(patched.get("origin_latitude")):
        QgsMessageLog.logMessage(
            f"singleLoc changed: origin_latitude "
            f"{raw.get('origin_latitude')!r} → {patched.get('origin_latitude')!r}",
            "LOC", Qgis.Info,
        )
        return False

    # Identity fields: compare as normalized strings
    if _norm_val(raw.get("unique_asset_id")) != _norm_val(patched.get("unique_asset_id")):
        QgsMessageLog.logMessage(
            f"singleLoc changed: unique_asset_id "
            f"{raw.get('unique_asset_id')!r} → {patched.get('unique_asset_id')!r}",
            "LOC", Qgis.Info,
        )
        return False
    if _norm_val(raw.get("actual_asset_name")) != _norm_val(patched.get("actual_asset_name")):
        QgsMessageLog.logMessage(
            f"singleLoc changed: actual_asset_name "
            f"{raw.get('actual_asset_name')!r} → {patched.get('actual_asset_name')!r}",
            "LOC", Qgis.Info,
        )
        return False

    # Fields sub-dict: compare key-by-key with normalization
    if not _fields_equal(raw.get("fields"), patched.get("fields")):
        QgsMessageLog.logMessage(
            f"singleLoc changed: fields sub-dict differs",
            "LOC", Qgis.Info,
        )
        return False

    # Top-level field_N values
    for k in raw:
        if k.startswith("field_") and _norm_val(raw[k]) != _norm_val(patched.get(k)):
            QgsMessageLog.logMessage(
                f"singleLoc changed: {k} "
                f"{raw[k]!r} → {patched.get(k)!r}",
                "LOC", Qgis.Info,
            )
            return False
    return True


def _patch_dual_loc(
    raw: dict,
    route: Route,
    line_feature: Optional[QgsFeature],
    mapping: LayerMapping,
    category: Optional[Category],
    origin_xy: Tuple[float, float],
    dest_xy: Tuple[float, float],
    now: str,
    location_id: str = "",
) -> dict:
    """Patch a raw dualLOC dict with current QGIS values.

    Starts from the original server JSON so all server-only fields
    are automatically preserved.  Only coordinates, field values,
    and identity fields are overwritten (updatedAt preserved).
    """
    patched = dict(raw)

    # Patch origin coordinates — only overwrite if the point actually
    # moved (>1mm).  The standalone point feature's _origin_lon/_origin_lat
    # can differ slightly from the dualLoc's origin coordinates on the
    # server; preserving the originals avoids false "changed" detection.
    raw_o_lon = _float_val(raw.get("origin_longitude", 0))
    raw_o_lat = _float_val(raw.get("origin_latitude", 0))
    if (round(raw_o_lon, 8) != round(origin_xy[0], 8)
            or round(raw_o_lat, 8) != round(origin_xy[1], 8)):
        patched["origin_longitude"] = origin_xy[0]
        patched["origin_latitude"] = origin_xy[1]

    # Patch destination coordinates (same preservation logic)
    loc_dest = patched.get("LOCDestination")
    if isinstance(loc_dest, dict):
        loc_dest = dict(loc_dest)
        raw_d_lon = _float_val(loc_dest.get("longitude", 0))
        raw_d_lat = _float_val(loc_dest.get("latitude", 0))
        if (round(raw_d_lon, 8) != round(dest_xy[0], 8)
                or round(raw_d_lat, 8) != round(dest_xy[1], 8)):
            loc_dest["longitude"] = dest_xy[0]
            loc_dest["latitude"] = dest_xy[1]
    else:
        loc_dest = {
            "longitude": dest_xy[0],
            "latitude": dest_xy[1],
        }
    patched["LOCDestination"] = loc_dest

    # Patch origin-side fields sub-dict only.
    # Sub-dict uses human-readable names; merge mapped values into the raw
    # dict so unmapped fields keep their server values.
    # Do NOT touch top-level field_N keys — the raw JSON already has the
    # correct values, and the category API field order may not match the
    # server's internal field_N numbering.
    cat_fields = category.fields if category else []
    _origin_top, origin_sub = _numbered_fields(
        cat_fields, mapping.field_mappings, line_feature,
        _DUAL_ORIGIN_KNOWN, top_prefix="field",
        mapped_only=True,
    )
    if origin_sub:
        raw_fields = patched.get("fields")
        if isinstance(raw_fields, dict):
            merged = dict(raw_fields)
            merged.update(origin_sub)
            patched["fields"] = merged
        else:
            patched["fields"] = origin_sub

    # Patch destination-side fields sub-dict only (same merge strategy).
    cat_dest_fields = category.destination_fields if category else []
    _dest_top, dest_sub = _numbered_fields(
        cat_dest_fields, mapping.field_mappings, line_feature,
        _DUAL_DEST_KNOWN, top_prefix="destination_field",
        mapped_only=True,
    )
    if dest_sub:
        raw_dest_fields = loc_dest.get("destination_fields")
        if isinstance(raw_dest_fields, dict):
            merged = dict(raw_dest_fields)
            merged.update(dest_sub)
            loc_dest["destination_fields"] = merged
        else:
            loc_dest["destination_fields"] = dest_sub

    # Patch identity fields from current QGIS attributes
    route_id_qfield = mapping.qgis_field_for("Route ID")
    unique_asset_id = ""
    if route_id_qfield and line_feature is not None:
        unique_asset_id = _safe_value(line_feature, route_id_qfield)
    if not unique_asset_id:
        unique_asset_id = route.line_name
    patched["unique_asset_id"] = unique_asset_id

    aan_qfield = mapping.qgis_field_for("Actual Asset Name")
    actual_asset_name = ""
    if aan_qfield and line_feature is not None:
        actual_asset_name = _safe_value(line_feature, aan_qfield)
    if not actual_asset_name:
        actual_asset_name = route.origin
    patched["actual_asset_name"] = actual_asset_name

    dest_qfield = mapping.qgis_field_for("Destination")
    destination = ""
    if dest_qfield and line_feature is not None:
        destination = _safe_value(line_feature, dest_qfield)
    if not destination:
        destination = route.destination
    # Patch both the top-level and nested destination name
    if isinstance(patched["LOCDestination"], dict):
        patched["LOCDestination"]["destination"] = destination

    # Preserve original updatedAt — only the server should bump this.

    # Ensure push-required keys are present (from-scratch path sets these)
    patched["category"] = mapping.category_id
    patched["category_name"] = mapping.category_name
    patched["category_type"] = "dual"
    patched["LOC_type"] = "dual"
    if location_id:
        patched["location_id"] = location_id

    return patched


def _dloc_unchanged(raw: dict, patched: dict) -> bool:
    """Return True if no user-editable values changed on a dualLoc.

    Uses type-normalized comparisons so that raw JSON types (int, float,
    null) compare equal to their QGIS string equivalents ("42", "N/A").
    """
    # Origin coordinates
    if _norm_coord(raw.get("origin_longitude")) != _norm_coord(patched.get("origin_longitude")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: origin_longitude "
            f"{raw.get('origin_longitude')!r} → {patched.get('origin_longitude')!r}",
            "LOC", Qgis.Info,
        )
        return False
    if _norm_coord(raw.get("origin_latitude")) != _norm_coord(patched.get("origin_latitude")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: origin_latitude "
            f"{raw.get('origin_latitude')!r} → {patched.get('origin_latitude')!r}",
            "LOC", Qgis.Info,
        )
        return False

    # Identity fields
    if _norm_val(raw.get("unique_asset_id")) != _norm_val(patched.get("unique_asset_id")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: unique_asset_id "
            f"{raw.get('unique_asset_id')!r} → {patched.get('unique_asset_id')!r}",
            "LOC", Qgis.Info,
        )
        return False
    if _norm_val(raw.get("actual_asset_name")) != _norm_val(patched.get("actual_asset_name")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: actual_asset_name "
            f"{raw.get('actual_asset_name')!r} → {patched.get('actual_asset_name')!r}",
            "LOC", Qgis.Info,
        )
        return False

    # Origin fields sub-dict
    if not _fields_equal(raw.get("fields"), patched.get("fields")):
        QgsMessageLog.logMessage(
            "dualLoc changed: origin fields sub-dict differs",
            "LOC", Qgis.Info,
        )
        return False

    # Top-level field_N values
    for k in raw:
        if k.startswith("field_") and _norm_val(raw[k]) != _norm_val(patched.get(k)):
            QgsMessageLog.logMessage(
                f"dualLoc changed: {k} "
                f"{raw[k]!r} → {patched.get(k)!r}",
                "LOC", Qgis.Info,
            )
            return False

    # Destination side
    raw_dest = raw.get("LOCDestination") or {}
    pat_dest = patched.get("LOCDestination") or {}

    if _norm_coord(raw_dest.get("longitude")) != _norm_coord(pat_dest.get("longitude")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: dest longitude "
            f"{raw_dest.get('longitude')!r} → {pat_dest.get('longitude')!r}",
            "LOC", Qgis.Info,
        )
        return False
    if _norm_coord(raw_dest.get("latitude")) != _norm_coord(pat_dest.get("latitude")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: dest latitude "
            f"{raw_dest.get('latitude')!r} → {pat_dest.get('latitude')!r}",
            "LOC", Qgis.Info,
        )
        return False
    if _norm_val(raw_dest.get("destination")) != _norm_val(pat_dest.get("destination")):
        QgsMessageLog.logMessage(
            f"dualLoc changed: destination "
            f"{raw_dest.get('destination')!r} → {pat_dest.get('destination')!r}",
            "LOC", Qgis.Info,
        )
        return False

    # Destination fields sub-dict
    if not _fields_equal(raw_dest.get("destination_fields"), pat_dest.get("destination_fields")):
        QgsMessageLog.logMessage(
            "dualLoc changed: destination_fields sub-dict differs",
            "LOC", Qgis.Info,
        )
        return False

    # Destination top-level destination_field_N values
    for k in raw_dest:
        if k.startswith("destination_field_") and _norm_val(raw_dest[k]) != _norm_val(pat_dest.get(k)):
            QgsMessageLog.logMessage(
                f"dualLoc changed: {k} "
                f"{raw_dest[k]!r} → {pat_dest.get(k)!r}",
                "LOC", Qgis.Info,
            )
            return False
    return True


def _all_stops_unchanged(orig_stops: list, rebuilt_stops: list) -> bool:
    """Return True if every stop's singleLoc is unchanged."""
    if len(orig_stops) != len(rebuilt_stops):
        return False
    for orig, rebuilt in zip(
        sorted(orig_stops, key=lambda s: s.get("stopNumber", 0)),
        rebuilt_stops,
    ):
        o_sloc = (orig.get("singleLoc")
                  or orig.get("singleLOC")
                  or orig.get("single_loc")
                  or {})
        r_sloc = rebuilt.get("singleLoc") or {}
        if not _sloc_unchanged(o_sloc, r_sloc):
            return False
    return True


def _rebuild_stops_from_raw(
    raw_mloc: dict,
    intermediates: List[Stop],
    route: Route,
    mapping_by_layer: Dict[str, LayerMapping],
    categories: Dict[str, Category],
    line_mapping: LayerMapping,
    line_feature: Optional[QgsFeature],
    transforms: Dict[str, Optional[QgsCoordinateTransform]],
    location: Location,
    user_id: str,
    now: str,
) -> List[dict]:
    """Rebuild Stops array using raw server stops where possible.

    For each current intermediate stop:
    - If its coordinate matches an original stop, shallow-copy the
      original stop entry and patch coordinates (updatedAt preserved).
    - If coordinate matching fails but stop counts match, fall back to
      sequential (index-based) matching — safe because reconstruction
      preserves stop order from line vertices.
    - If unmatched (new stop), build from scratch using existing logic.

    Stops that existed on server but are no longer present are simply
    omitted — the server handles deletion by omission.
    """
    project = QgsProject.instance()

    # Index original stops by coordinate key → list of entries
    # (to handle IN/OUT pairs at the same coordinate)
    orig_stops = raw_mloc.get("Stops", raw_mloc.get("stops", []))
    sorted_orig = sorted(
        orig_stops, key=lambda s: s.get("stopNumber", 0)
    )
    orig_by_coord: Dict[Tuple[float, float], list] = {}
    for stop_entry in sorted_orig:
        sloc = (stop_entry.get("singleLoc")
                or stop_entry.get("singleLOC")
                or stop_entry.get("single_loc")
                or {})
        lon = _float_val(sloc.get("origin_longitude", 0))
        lat = _float_val(sloc.get("origin_latitude", 0))
        if lon == 0 and lat == 0:
            continue
        key = (round(lon, 8), round(lat, 8))
        orig_by_coord.setdefault(key, []).append(stop_entry)

    # Parse coord→ID map for fallback (same as existing logic)
    stop_id_map: dict = {}
    orig_stop_ids: List[str] = []
    if line_feature is not None:
        raw_ids = _safe_value(line_feature, "_stop_loc_ids")
        if raw_ids:
            stop_id_map = _parse_stop_loc_ids(raw_ids)
        raw_stop_ids_str = _safe_value(line_feature, "_stop_ids")
        if raw_stop_ids_str:
            orig_stop_ids = raw_stop_ids_str.split(",")

    route_uid = route.line_name
    if line_feature is not None:
        route_id_qfield = line_mapping.qgis_field_for("Route ID")
        if route_id_qfield:
            v = _safe_value(line_feature, route_id_qfield)
            if v:
                route_uid = v

    stop_cat = categories.get(line_mapping.stop_category_id)

    # Determine if sequential fallback is safe: same stop count means
    # reconstruction preserved the exact same stops in the same order.
    can_seq_fallback = len(intermediates) == len(sorted_orig)

    stops_arr: List[dict] = []
    for idx, stop in enumerate(intermediates, start=1):
        pt_feature = _get_feature(
            project, stop.point_layer_id, stop.point_feature_id,
        )
        pt_xy = _point_wgs84(
            pt_feature, stop.point_layer_id, transforms,
        )
        coord_key = (round(pt_xy[0], 8), round(pt_xy[1], 8))

        # Try to match to an original stop by coordinate
        orig_list = orig_by_coord.get(coord_key)
        matched_entry = None

        if orig_list:
            matched_entry = orig_list.pop(0)
        elif can_seq_fallback and idx - 1 < len(sorted_orig):
            # Sequential fallback: standalone singleLOC coordinates
            # can differ from stop singleLoc coordinates on the server.
            # When stop counts match, reconstruction preserved order
            # from line vertices, so the Nth intermediate is the Nth
            # original stop.
            matched_entry = sorted_orig[idx - 1]
            QgsMessageLog.logMessage(
                f"Stop {idx} of '{route.line_name}': coord mismatch, "
                f"using sequential fallback (pt={coord_key})",
                "LOC", Qgis.Info,
            )

        if matched_entry is not None:
            orig_entry = dict(matched_entry)
            # Patch the singleLoc within the stop entry
            orig_sloc = (orig_entry.get("singleLoc")
                         or orig_entry.get("singleLOC")
                         or orig_entry.get("single_loc")
                         or {})
            patched_sloc = dict(orig_sloc)
            # Preserve original stop coordinates — the point feature's
            # _origin_lon/_origin_lat may differ slightly from the stop
            # singleLoc's coords (different API objects on the server).
            # Only overwrite if the user actually moved the point feature
            # (geometry differs from stored coords by more than ~1mm).
            orig_lon = _float_val(orig_sloc.get("origin_longitude", 0))
            orig_lat = _float_val(orig_sloc.get("origin_latitude", 0))
            if (round(orig_lon, 8) != round(pt_xy[0], 8)
                    or round(orig_lat, 8) != round(pt_xy[1], 8)):
                patched_sloc["origin_longitude"] = pt_xy[0]
                patched_sloc["origin_latitude"] = pt_xy[1]

            # Patch identity fields from current point feature
            pt_mapping = mapping_by_layer.get(stop.point_layer_id)
            pt_uid_qfield = pt_mapping.qgis_field_for("Unique Asset Identifier") if pt_mapping else ""
            asset_uid = _safe_value(pt_feature, pt_uid_qfield) if pt_uid_qfield and pt_feature else ""
            if not asset_uid:
                pt_name_qfield = pt_mapping.qgis_field_for("Actual Asset Name") if pt_mapping else ""
                asset_uid = _safe_value(pt_feature, pt_name_qfield) if pt_name_qfield and pt_feature else ""
            if not asset_uid:
                asset_uid = stop.display_name
            new_base_uid = f"{route_uid}_{asset_uid}" if route_uid else asset_uid
            # Preserve original unique_asset_id if the base matches — the
            # server may have added a disambiguation suffix (e.g. "_3") for
            # egress stops in IN/OUT pairs. The server needs the suffix
            # to correctly match and delete stops.
            orig_uid = orig_sloc.get("unique_asset_id", "")
            if orig_uid and orig_uid.startswith(new_base_uid):
                patched_sloc["unique_asset_id"] = orig_uid
            else:
                patched_sloc["unique_asset_id"] = new_base_uid

            pt_aan_qfield = pt_mapping.qgis_field_for("Actual Asset Name") if pt_mapping else ""
            actual_name = _safe_value(pt_feature, pt_aan_qfield) if pt_aan_qfield and pt_feature else ""
            if not actual_name:
                actual_name = asset_uid
            patched_sloc["actual_asset_name"] = actual_name
            patched_sloc["location_id"] = location.location_id

            # Normalize to canonical "singleLoc" key for push endpoint.
            # Remove any case-variant keys from the original entry.
            for k in list(orig_entry.keys()):
                if k.lower() in ("singleloc", "single_loc"):
                    del orig_entry[k]
            orig_entry["singleLoc"] = patched_sloc
            orig_entry["stopNumber"] = idx
            stops_arr.append(orig_entry)
        else:
            # New stop — build from scratch (existing logic)
            pt_mapping = mapping_by_layer.get(stop.point_layer_id)
            single_loc = _build_single_loc_obj(
                stop, route, pt_feature, pt_mapping,
                stop_cat, line_mapping, line_feature,
                route_uid, pt_xy, idx,
                location.location_id, user_id, now,
                stop_id_map=stop_id_map,
            )
            sid = (orig_stop_ids[idx - 1]
                   if idx - 1 < len(orig_stop_ids)
                   else str(uuid4()))
            stops_arr.append({
                "stop_id": sid or str(uuid4()),
                "stopNumber": idx,
                "singleLoc": single_loc,
            })

    return stops_arr


# ------------------------------------------------------------------
# Numbered field helpers
# ------------------------------------------------------------------

def _numbered_fields(
    cat_field_list: List[CategoryField],
    field_mappings: List[FieldMapping],
    feature: Optional[QgsFeature],
    known_names: set,
    top_prefix: str = "field",
    use_named_keys: bool = True,
    mapped_only: bool = False,
) -> Tuple[dict, dict]:
    """Build field dicts from a category's field list.

    Returns ``(top_level_dict, sub_dict)`` where:

    - *top_level_dict* has keys like ``field_1: value_or_None``
      (legacy fixed top-level keys on the LOC object)
    - *sub_dict* keys depend on *use_named_keys*:
      - True (default): real field names (e.g. ``"Serial Number": "N/A"``).
        The server stores all ``fields`` / ``destination_fields`` sub-dicts
        with human-readable names.
      - False: numbered keys (e.g. ``"field_1": "N/A"``).
        Not currently used — kept for potential future needs.

    If *mapped_only* is True, fields with no QGIS mapping are omitted
    from both dicts.  Use this when merging into a raw server JSON base
    so that unmapped fields keep their original server values.

    Fields whose names (case-insensitive) appear in *known_names* are
    skipped (they get dedicated top-level keys instead).
    """
    if not cat_field_list:
        return {}, {}

    fm_lookup: Dict[str, str] = {}
    for fm in field_mappings:
        if fm.loc_field and fm.qgis_field:
            fm_lookup[fm.loc_field.lower()] = fm.qgis_field

    top: dict = {}
    sub: dict = {}
    n = 0

    for cf in cat_field_list:
        if cf.name.lower() in known_names:
            continue
        n += 1
        top_key = f"{top_prefix}_{n}"
        sub_key = cf.name if use_named_keys else f"field_{n}"

        qgis_field = fm_lookup.get(cf.name.lower(), "")
        val = ""
        if qgis_field and feature is not None:
            val = _safe_value(feature, qgis_field)

        # In mapped_only mode, skip fields with no QGIS mapping so
        # the caller's merge preserves the raw server value.
        if mapped_only and not qgis_field:
            continue

        top[top_key] = val if val else None
        sub[sub_key] = val if val else "N/A"

    return top, sub


# ------------------------------------------------------------------
# Coordinate helpers — WGS 84
# ------------------------------------------------------------------

def _build_wgs84_transforms(
    project: QgsProject,
    layer_mappings: List[LayerMapping],
) -> Dict[str, Optional[QgsCoordinateTransform]]:
    """Pre-build CRS transforms (layer CRS → WGS 84) per layer."""
    ctx = project.transformContext()
    transforms: Dict[str, Optional[QgsCoordinateTransform]] = {}
    for lm in layer_mappings:
        lid = lm.layer_id
        if lid in transforms:
            continue
        layer = project.mapLayer(lid)
        if not isinstance(layer, QgsVectorLayer):
            continue
        if layer.crs() != WGS84:
            transforms[lid] = QgsCoordinateTransform(
                layer.crs(), WGS84, ctx,
            )
        else:
            transforms[lid] = None
    return transforms


def _route_endpoint_coords(
    route: Route,
    project: QgsProject,
    transforms: Dict[str, Optional[QgsCoordinateTransform]],
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Return (origin_xy, dest_xy) from the route's origin/destination
    point features rather than from line geometry endpoints.

    The origin and destination of a dualLoc should sit at the first and
    last *structure* (point feature) along the line, not at the raw
    line-geometry vertices which may extend beyond.

    Falls back to line endpoints if the point features cannot be found
    (e.g. on pulled layers that lack the original point features).
    """
    zero = (0.0, 0.0)
    origin_xy = zero
    dest_xy = zero

    for stop in route.stops:
        if stop.removed:
            continue
        if stop.stop_type == StopType.ORIGIN:
            feat = _get_feature(project, stop.point_layer_id,
                                stop.point_feature_id)
            if feat is not None:
                origin_xy = _point_wgs84(feat, stop.point_layer_id,
                                         transforms)
        elif stop.stop_type == StopType.DESTINATION:
            feat = _get_feature(project, stop.point_layer_id,
                                stop.point_feature_id)
            if feat is not None:
                dest_xy = _point_wgs84(feat, stop.point_layer_id,
                                       transforms)

    # Fallback to line endpoints if point features not found
    if origin_xy == zero or dest_xy == zero:
        line_feat = _get_feature(project, route.line_layer_id,
                                 route.line_feature_id)
        fallback_o, fallback_d = _line_endpoints_wgs84(
            line_feat, route.line_layer_id, transforms,
        )
        if origin_xy == zero:
            origin_xy = fallback_o
        if dest_xy == zero:
            dest_xy = fallback_d

    return origin_xy, dest_xy


def _line_endpoints_wgs84(
    line_feature: Optional[QgsFeature],
    layer_id: str,
    transforms: Dict[str, Optional[QgsCoordinateTransform]],
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Return (origin_xy, dest_xy) for a line feature in WGS 84.

    Prefers stored ``_origin_lon``/``_origin_lat`` and
    ``_dest_lon``/``_dest_lat`` string attributes for bit-exact
    coordinate preservation on round-trip.
    """
    zero = (0.0, 0.0)
    if line_feature is None:
        return zero, zero

    # Try stored original coordinates first
    origin_xy = zero
    dest_xy = zero
    o_lon = _safe_value(line_feature, "_origin_lon")
    o_lat = _safe_value(line_feature, "_origin_lat")
    d_lon = _safe_value(line_feature, "_dest_lon")
    d_lat = _safe_value(line_feature, "_dest_lat")
    try:
        if o_lon and o_lat:
            origin_xy = (float(o_lon), float(o_lat))
        if d_lon and d_lat:
            dest_xy = (float(d_lon), float(d_lat))
    except ValueError:
        pass
    if origin_xy != zero and dest_xy != zero:
        return origin_xy, dest_xy

    # Fallback to geometry
    geom = QgsGeometry(line_feature.geometry())
    if geom.isEmpty() or geom.isNull():
        return origin_xy or zero, dest_xy or zero

    xform = transforms.get(layer_id)
    if xform is not None:
        geom.transform(xform)

    if geom.isMultipart():
        parts = geom.asMultiPolyline()
        if not parts or not parts[0]:
            return origin_xy or zero, dest_xy or zero
        first = parts[0][0]
        last = parts[-1][-1]
    else:
        pts = geom.asPolyline()
        if not pts:
            return origin_xy or zero, dest_xy or zero
        first = pts[0]
        last = pts[-1]

    if origin_xy == zero:
        origin_xy = (first.x(), first.y())
    if dest_xy == zero:
        dest_xy = (last.x(), last.y())

    return origin_xy, dest_xy


def _point_wgs84(
    pt_feature: Optional[QgsFeature],
    layer_id: str,
    transforms: Dict[str, Optional[QgsCoordinateTransform]],
) -> Tuple[float, float]:
    """Return (lon, lat) for a point feature in WGS 84.

    Prefers stored ``_origin_lon``/``_origin_lat`` string attributes
    (set during pull) for bit-exact coordinate preservation on
    round-trip.  Falls back to geometry if the attributes are absent
    (e.g. for features created locally in QGIS).
    """
    if pt_feature is None:
        return (0.0, 0.0)

    # Try stored original coordinates first (avoids precision drift)
    stored_lon = _safe_value(pt_feature, "_origin_lon")
    stored_lat = _safe_value(pt_feature, "_origin_lat")
    if stored_lon and stored_lat:
        try:
            return (float(stored_lon), float(stored_lat))
        except ValueError:
            pass

    geom = QgsGeometry(pt_feature.geometry())
    if geom.isEmpty() or geom.isNull():
        return (0.0, 0.0)

    xform = transforms.get(layer_id)
    if xform is not None:
        geom.transform(xform)

    pt = geom.asPoint()
    return (pt.x(), pt.y())


# ------------------------------------------------------------------
# Generic helpers
# ------------------------------------------------------------------

def _get_feature(
    project: QgsProject,
    layer_id: str,
    feature_id: int,
) -> Optional[QgsFeature]:
    """Retrieve a specific feature from a layer by ID."""
    layer = project.mapLayer(layer_id)
    if not isinstance(layer, QgsVectorLayer):
        return None
    feat = layer.getFeature(feature_id)
    if not feat.isValid():
        return None
    return feat


def _get_raw_json(
    feature: Optional[QgsFeature], field_name: str,
) -> Optional[dict]:
    """Read a stored raw JSON attribute, returning parsed dict or None."""
    if feature is None:
        return None
    raw = _safe_value(feature, field_name)
    if not raw:
        return None
    try:
        data = _json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _get_tracking_id(
    feature: Optional[QgsFeature], field_name: str,
) -> str:
    """Return a stored server ID from a feature, or generate a new UUID.

    Pulled features have hidden attributes (``_loc_id``, ``_origin_id``,
    ``_dest_id``) populated with the server's IDs.  If present, we reuse
    them so the push is treated as an update rather than a create.
    """
    if feature is not None:
        val = _safe_value(feature, field_name)
        if val:
            return val
    return str(uuid4())


def _collect_payload_loc_ids(payload: dict) -> set:
    """Collect all loc_id values present in a push payload."""
    ids: set = set()

    for ml in payload.get("multiLocs", []):
        dl = ml.get("dualLoc", {})
        if dl.get("loc_id"):
            ids.add(dl["loc_id"])
        for stop in ml.get("Stops", []):
            sl = stop.get("singleLoc", {})
            if sl.get("loc_id"):
                ids.add(sl["loc_id"])

    for dl in payload.get("dual_locs", []):
        if dl.get("loc_id"):
            ids.add(dl["loc_id"])

    for sl in payload.get("single_locs", []):
        attrs = sl.get("attributes", {})
        lid = attrs.get("loc_id", sl.get("loc_id", ""))
        if lid:
            ids.add(lid)

    return ids


def _parse_stop_loc_ids(
    raw: str,
) -> Dict[Tuple[float, float], List[Tuple[str, str, float, float]]]:
    """Parse a ``_stop_loc_ids`` attribute string into a coord→ID map.

    Format: ``"lon,lat=locid,originid,exactlon,exactlat|..."``
    Returns ``{(lon, lat): [(loc_id, origin_id, exact_lon, exact_lat), ...]}``.

    Multiple entries at the same coordinate (from ingress/egress pairs)
    are stored as a list; callers pop from the front so each stop at
    the same location gets its own unique server ID and exact coords.
    """
    result: Dict[Tuple[float, float], List[Tuple[str, str, float, float]]] = {}
    if not raw:
        return result
    for part in raw.split("|"):
        if "=" not in part:
            continue
        coord_str, ids_str = part.split("=", 1)
        coord_parts = coord_str.split(",")
        id_parts = ids_str.split(",")
        if len(coord_parts) == 2 and len(id_parts) >= 1:
            try:
                lon = float(coord_parts[0])
                lat = float(coord_parts[1])
            except ValueError:
                continue
            loc_id = id_parts[0]
            origin_id = id_parts[1] if len(id_parts) > 1 else ""
            # Exact server coordinates (if stored)
            exact_lon = 0.0
            exact_lat = 0.0
            try:
                if len(id_parts) > 3:
                    exact_lon = float(id_parts[2])
                    exact_lat = float(id_parts[3])
            except ValueError:
                pass
            key = (round(lon, 8), round(lat, 8))
            result.setdefault(key, []).append(
                (loc_id, origin_id, exact_lon, exact_lat)
            )
    return result


def _safe_value(feature: QgsFeature, field_name: str) -> str:
    """Read a feature attribute, returning '' for NULL/missing values."""
    try:
        val = feature.attribute(field_name)
    except Exception:
        return ""
    if val is None:
        return ""
    text = str(val)
    if text == "NULL":
        return ""
    return text


def _float_val(val) -> float:
    """Safe float conversion, returns 0.0 on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
