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

import hashlib as _hashlib
import json as _json
import os
import pathlib as _pathlib
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

from ..qt_compat import GEOM_POINT, GEOM_LINE
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


def _fields_fingerprint(*values) -> str:
    """Compute a short hash of field values for change detection."""
    raw = _json.dumps(values, sort_keys=True, default=str)
    return _hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


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

    The push endpoint is a full replacement: the server bins every LOC
    in the location that the payload no longer references.  There is no
    delete list in the API — leaving an entry out of the payload IS the
    delete.

    If *pulled_loc_ids* is provided (non-empty set), any pulled loc_id
    that is no longer present in the payload is listed under the
    internal ``_deleted_loc_ids`` key (stripped before sending) so the
    push preview can warn the operator about impending deletions.
    """
    project = QgsProject.instance()
    now = _now_iso()

    global _active_location_id, _current_stamp_layer
    _active_location_id = location.location_id
    _current_stamp_layer = None

    # Re-key stamp cache entries if memory layer UIDs changed
    # (e.g. after route regeneration).
    _migrate_stamps_for_routes(routes, location.location_id)

    mapping_by_layer: Dict[str, LayerMapping] = {
        lm.layer_id: lm for lm in layer_mappings
        if lm.enabled_for_export
    }
    transforms = _build_wgs84_transforms(project, layer_mappings)

    multi_locs: List[dict] = []
    dual_locs_list: List[dict] = []
    # Maps payload entries back to layer features for post-push ID sync.
    # Each entry: {"layer_id": str, "feature_id": int, "type": "multi"|"dual"}
    _feature_map: List[dict] = []

    for route in routes:
        line_mapping = mapping_by_layer.get(route.line_layer_id)
        if not line_mapping:
            continue

        line_feature = _get_feature(
            project, route.line_layer_id, route.line_feature_id,
        )
        if line_feature is None:
            # The source feature no longer exists (deleted after
            # generation or pull).  Leave the route out: the push is a
            # full replacement, so absence is what deletes it server-side.
            # Emitting the stale route would resurrect the deleted asset
            # under a fresh UUID.
            continue
        line_cat = categories.get(line_mapping.category_id)

        # Use the actual origin/destination POINT feature coordinates,
        # not the line endpoints (which may extend beyond the structures).
        origin_xy, dest_xy = _route_endpoint_coords(
            route, project, transforms,
        )

        # _route_endpoint_coords calls _get_feature on point layers,
        # which overwrites _current_stamp_layer.  Restore the line
        # layer so _build_dual_loc_obj looks up the right cache entry.
        _line_layer = project.mapLayer(route.line_layer_id)
        if isinstance(_line_layer, QgsVectorLayer):
            _set_current_layer(_line_layer)

        dual_loc = _build_dual_loc_obj(
            route, line_feature, line_mapping, line_cat,
            origin_xy, dest_xy,
            location.location_id, user_id, now,
        )

        intermediates = route.intermediate_stops
        fmap_entry = {"layer_id": route.line_layer_id,
                      "feature_id": route.line_feature_id}
        if not intermediates:
            # 0 stops → standalone dual_loc
            # Always include — server deletes LOCs not in the push.
            dual_locs_list.append(dual_loc)
            _feature_map.append({**fmap_entry, "type": "dual",
                                 "index": len(dual_locs_list) - 1})
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
                _feature_map.append({**fmap_entry, "type": "multi",
                                     "index": len(multi_locs) - 1})
            else:
                # From-scratch path (locally created features)
                stop_cat = categories.get(line_mapping.stop_category_id)
                route_uid = dual_loc.get("unique_asset_id", route.line_name)

                # Parse coord→ID map so we can reuse original server IDs
                # for stops at unchanged coordinates.
                stop_id_map: dict = {}
                cached_stop_count = 0
                if line_feature is not None:
                    raw_ids = _safe_value(line_feature, "_stop_loc_ids")
                    if raw_ids:
                        stop_id_map = _parse_stop_loc_ids(raw_ids)
                        # Count cached stops to detect adds/removes
                        cached_stop_count = len(raw_ids.split("|"))

                # If stop count changed and cache lacks per-stop numbers
                # (old format), bump ALL stop timestamps in this route so
                # the server reprocesses renumbered entries.
                force_bump_ts = (
                    cached_stop_count > 0
                    and cached_stop_count != len(intermediates)
                )

                stops_arr: List[dict] = []
                _matched = 0
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
                    # Force-bump updatedAt if stop count changed and the
                    # per-stop orig_stop_num check didn't already fire
                    # (backward compat with old cache format).
                    if force_bump_ts:
                        single_loc["updatedAt"] = now

                    # Reuse stop_id by coordinate match (from stamp cache
                    # or pulled data). Fresh UUID only for genuinely new stops.
                    sid = single_loc.pop("_matched_stop_id", "")
                    if sid:
                        _matched += 1
                    else:
                        sid = str(uuid4())
                    stops_arr.append({
                        "stop_id": sid,
                        "stopNumber": idx,
                        "singleLoc": single_loc,
                    })

                multi_locs.append({
                    "id": multi_id or str(uuid4()),
                    "multiName": route.line_name,
                    "Stops": stops_arr,
                    "dualLoc": dual_loc,
                })
                _feature_map.append({**fmap_entry, "type": "multi",
                                     "index": len(multi_locs) - 1})

    # Standalone single LOCs — point features from single-type categories
    single_locs_list, single_fmap = _build_standalone_single_locs(
        project, layer_mappings, categories, transforms,
        location.location_id, user_id, now,
    )
    _feature_map.extend(single_fmap)

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
        # Internal: maps payload entries → layer features for post-push sync.
        # Stripped before sending to server.
        "_feature_map": _feature_map,
    }

    if pulled_loc_ids:
        present_ids = _collect_payload_loc_ids(payload)
        payload["_deleted_loc_ids"] = sorted(pulled_loc_ids - present_ids)

    return payload


def _collect_payload_loc_ids(payload: dict) -> set:
    """Return every loc_id referenced by the outgoing payload."""
    ids: set = set()
    for sl in payload.get("single_locs", []):
        if sl.get("loc_id"):
            ids.add(sl["loc_id"])
    for dl in payload.get("dual_locs", []):
        if dl.get("loc_id"):
            ids.add(dl["loc_id"])
    for ml in payload.get("multiLocs", []):
        dl = ml.get("dualLoc", {})
        if isinstance(dl, dict) and dl.get("loc_id"):
            ids.add(dl["loc_id"])
        for stop in ml.get("Stops", []):
            sl = stop.get("singleLoc", {})
            if isinstance(sl, dict) and sl.get("loc_id"):
                ids.add(sl["loc_id"])
    return ids


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
        "will_be_deleted": len(payload.get("_deleted_loc_ids", [])),
    }


def stamp_ids_after_push(payload: dict) -> int:
    """Write payload IDs to a sidecar JSON cache after a successful push.

    Uses the ``_feature_map`` built during ``build_payload()`` to match
    payload entries to their source layer features, then saves the
    generated UUIDs and raw JSON to ``~/.loc_stamp_cache.json`` so
    subsequent pushes treat them as updates rather than creates.

    This avoids modifying source layers (Shapefiles have field-name and
    field-length limits that truncate UUIDs/JSON).

    Returns the number of features stamped.
    """
    feature_map = payload.get("_feature_map", [])
    if not feature_map:
        return 0

    location_id = payload.get("location_id", "")
    if not location_id:
        return 0

    global _active_location_id
    _active_location_id = location_id

    project = QgsProject.instance()
    multi_locs = payload.get("multiLocs", [])
    dual_locs = payload.get("dual_locs", [])
    single_locs = payload.get("single_locs", [])

    cache = _load_stamp_cache()
    loc_stamps = cache.setdefault(location_id, {})
    stamped = 0

    for fm in feature_map:
        layer = project.mapLayer(fm["layer_id"])
        if not isinstance(layer, QgsVectorLayer):
            continue

        fid = fm["feature_id"]
        feat = layer.getFeature(fid)
        if not feat.isValid():
            continue

        entry_type = fm["type"]
        idx = fm["index"]
        cache_key = _stamp_cache_key(layer, fid)
        entry: Dict[str, str] = {}

        if entry_type == "multi" and idx < len(multi_locs):
            mloc = multi_locs[idx]
            dloc = mloc.get("dualLoc", {})
            dest = dloc.get("LOCDestination", {})

            entry["_loc_id"] = dloc.get("loc_id", "")
            entry["_origin_id"] = dloc.get("origin_id", "")
            entry["_dest_id"] = dest.get("destination_id", "")
            entry["_multi_id"] = mloc.get("id", "")
            entry["_route_name"] = mloc.get("multiName", "")
            # Preserve timestamps so server skips reprocessing unchanged entries
            entry["_createdAt"] = dloc.get("createdAt", "")
            entry["_updatedAt"] = dloc.get("updatedAt", "")
            entry["_dest_createdAt"] = dest.get("createdAt", "")
            entry["_dest_updatedAt"] = dest.get("updatedAt", "")

            entry["_fields_hash"] = _fields_fingerprint(
                dloc.get("fields", {}),
                dest.get("destination_fields", {}),
                dloc.get("unique_asset_id", ""),
                dloc.get("actual_asset_name", ""),
                dest.get("destination", ""),
            )

            # Cache stop IDs so they can be reused on re-push
            stops = mloc.get("Stops", [])
            entry["_stop_ids"] = ",".join(
                s.get("stop_id", "") for s in stops
            )

            stop_loc_parts = []
            for s in stops:
                sl = s.get("singleLoc", {})
                slid = sl.get("loc_id", "")
                sloid = sl.get("origin_id", "")
                slon = sl.get("origin_longitude", "")
                slat = sl.get("origin_latitude", "")
                sid = s.get("stop_id", "")
                sl_ca = sl.get("createdAt", "")
                sl_ua = sl.get("updatedAt", "")
                snum = s.get("stopNumber", "")
                if slid and slon and slat:
                    key = f"{round(float(slon), 8)},{round(float(slat), 8)}"
                    stop_loc_parts.append(
                        f"{key}={slid},{sloid},{slon},{slat},{sid},{sl_ca},{sl_ua},{snum}"
                    )
            if stop_loc_parts:
                entry["_stop_loc_ids"] = "|".join(stop_loc_parts)

        elif entry_type == "dual" and idx < len(dual_locs):
            dloc = dual_locs[idx]
            dest = dloc.get("LOCDestination", {})
            entry["_loc_id"] = dloc.get("loc_id", "")
            entry["_origin_id"] = dloc.get("origin_id", "")
            entry["_dest_id"] = dest.get("destination_id", "")
            entry["_createdAt"] = dloc.get("createdAt", "")
            entry["_updatedAt"] = dloc.get("updatedAt", "")
            entry["_dest_createdAt"] = dest.get("createdAt", "")
            entry["_dest_updatedAt"] = dest.get("updatedAt", "")
            entry["_fields_hash"] = _fields_fingerprint(
                dloc.get("fields", {}),
                dest.get("destination_fields", {}),
                dloc.get("unique_asset_id", ""),
                dloc.get("actual_asset_name", ""),
                dest.get("destination", ""),
            )

        elif entry_type == "single" and idx < len(single_locs):
            sloc = single_locs[idx]
            attrs = sloc.get("attributes", {})
            entry["_loc_id"] = attrs.get("loc_id", "")
            entry["_origin_id"] = attrs.get("origin_id", "")
            entry["_createdAt"] = attrs.get("createdAt", "")
            entry["_updatedAt"] = attrs.get("updatedAt", "")
            entry["_wrapper_id"] = sloc.get("id", "")
            entry["_fields_hash"] = _fields_fingerprint(
                attrs.get("fields", {}),
                attrs.get("unique_asset_id", ""),
                attrs.get("actual_asset_name", ""),
            )

        if entry:
            loc_stamps[cache_key] = entry
            stamped += 1

    _save_stamp_cache(cache)

    QgsMessageLog.logMessage(
        f"stamp_ids_after_push: cached {stamped} feature(s)",
        "LOC Export", Qgis.MessageLevel.Info,
    )
    return stamped


def sync_stamps_from_server(
    locs_data: dict,
    location_id: str,
    layer_mappings: List[LayerMapping],
) -> Tuple[int, int]:
    """Match server LOC data to existing QGIS features and write stamp cache.

    Fetches ``unique_asset_id`` from each QGIS feature (via its layer mapping)
    and matches it against server LOC records.  Matched features get stamp
    cache entries populated with the server's IDs and timestamps, so
    subsequent pushes treat them as updates rather than creates.

    Returns ``(matched, total_server)`` — number of features successfully
    matched and total server LOC count.
    """
    project = QgsProject.instance()

    global _active_location_id
    _active_location_id = location_id

    cache = _load_stamp_cache()
    loc_stamps = cache.setdefault(location_id, {})

    # ------------------------------------------------------------------
    # 1. Index ALL server LOCs by unique_asset_id
    # ------------------------------------------------------------------
    # server_index: { unique_asset_id: { ...entry data... } }
    server_index: Dict[str, dict] = {}
    total_server = 0

    # singleLOCs
    for sloc in locs_data.get("singleLOCs", []):
        uid = sloc.get("unique_asset_id", "")
        if uid:
            server_index[uid] = {
                "_loc_id": sloc.get("loc_id", ""),
                "_origin_id": sloc.get("origin_id", ""),
                "_createdAt": sloc.get("createdAt", ""),
                "_updatedAt": sloc.get("updatedAt", ""),
                "_wrapper_id": sloc.get("id", str(uuid4())),
                "_type": "single",
            }
            total_server += 1

    # dualLOCs (standalone)
    for dloc in locs_data.get("dualLOCs", []):
        uid = dloc.get("unique_asset_id", "")
        dest = dloc.get("LOCDestination") or {}
        if uid:
            server_index[uid] = {
                "_loc_id": dloc.get("loc_id", ""),
                "_origin_id": dloc.get("origin_id", ""),
                "_dest_id": dest.get("destination_id", ""),
                "_createdAt": dloc.get("createdAt", ""),
                "_updatedAt": dloc.get("updatedAt", ""),
                "_dest_createdAt": dest.get("createdAt", ""),
                "_dest_updatedAt": dest.get("updatedAt", ""),
                "_type": "dual",
            }
            total_server += 1

    # multiLoc entries
    for mloc in locs_data.get("multiLoc", []):
        multi_id = mloc.get("id", mloc.get("_id", ""))
        dual_loc = (mloc.get("dualLoc") or mloc.get("dualLOC")
                    or mloc.get("dual_loc") or {})
        stops = mloc.get("Stops", mloc.get("stops", []))
        uid = dual_loc.get("unique_asset_id", "")
        dest = dual_loc.get("LOCDestination") or {}
        if uid:
            entry: Dict[str, str] = {
                "_loc_id": dual_loc.get("loc_id", ""),
                "_origin_id": dual_loc.get("origin_id", ""),
                "_dest_id": dest.get("destination_id", ""),
                "_multi_id": multi_id,
                "_route_name": mloc.get("multiName", uid),
                "_createdAt": dual_loc.get("createdAt", ""),
                "_updatedAt": dual_loc.get("updatedAt", ""),
                "_dest_createdAt": dest.get("createdAt", ""),
                "_dest_updatedAt": dest.get("updatedAt", ""),
                "_type": "multi",
            }
            # Cache stop IDs
            sorted_stops = sorted(stops, key=lambda s: s.get("stopNumber", 0))
            entry["_stop_ids"] = ",".join(
                s.get("stop_id", "") for s in sorted_stops
            )
            stop_loc_parts = []
            for s in sorted_stops:
                sl = (s.get("singleLoc") or s.get("singleLOC")
                      or s.get("single_loc") or {})
                slid = sl.get("loc_id", "")
                sloid = sl.get("origin_id", "")
                slon = sl.get("origin_longitude", "")
                slat = sl.get("origin_latitude", "")
                sid = s.get("stop_id", "")
                sl_ca = sl.get("createdAt", "")
                sl_ua = sl.get("updatedAt", "")
                snum = s.get("stopNumber", "")
                if slid and slon and slat:
                    try:
                        key = f"{round(float(slon), 8)},{round(float(slat), 8)}"
                    except (ValueError, TypeError):
                        continue
                    stop_loc_parts.append(
                        f"{key}={slid},{sloid},{slon},{slat},{sid},{sl_ca},{sl_ua},{snum}"
                    )
            if stop_loc_parts:
                entry["_stop_loc_ids"] = "|".join(stop_loc_parts)

            server_index[uid] = entry
            total_server += 1

    if not server_index:
        QgsMessageLog.logMessage(
            "Sync: no server LOCs found to match", "LOC Export", Qgis.MessageLevel.Warning,
        )
        return 0, 0

    # ------------------------------------------------------------------
    # 2. Walk QGIS features and match by unique_asset_id
    # ------------------------------------------------------------------
    matched = 0
    mapping_by_layer = {lm.layer_id: lm for lm in layer_mappings}

    for lm in layer_mappings:
        if not lm.category_id:
            continue
        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue

        geom_type = layer.geometryType()
        is_line = geom_type == GEOM_LINE

        # Determine the QGIS field that holds the matching key
        if is_line:
            uid_qfield = lm.qgis_field_for("Route ID")
        else:
            uid_qfield = lm.qgis_field_for("Unique Asset Identifier")

        if not uid_qfield:
            # Try the other key as fallback
            uid_qfield = (lm.qgis_field_for("Unique Asset Identifier")
                          or lm.qgis_field_for("Route ID"))
        if not uid_qfield:
            continue

        for feat in layer.getFeatures():
            if not feat.isValid():
                continue
            try:
                feat_uid = feat.attribute(uid_qfield)
            except (KeyError, RuntimeError):
                feat_uid = None
            if not feat_uid or str(feat_uid) == "NULL":
                continue
            feat_uid = str(feat_uid).strip()

            server_entry = server_index.get(feat_uid)
            if server_entry is None:
                continue

            cache_key = _stamp_cache_key(layer, feat.id())
            # Build stamp entry (exclude internal _type marker)
            stamp = {k: v for k, v in server_entry.items() if k != "_type"}
            loc_stamps[cache_key] = stamp
            matched += 1

    _save_stamp_cache(cache)

    QgsMessageLog.logMessage(
        f"Sync: matched {matched} feature(s) to {total_server} server LOC(s)",
        "LOC Export", Qgis.MessageLevel.Info,
    )
    return matched, total_server


# ---------------------------------------------------------------------------
# Stamp cache — sidecar JSON stored next to the QGIS project file
# (e.g. MyProject.qgz → MyProject_loc_stamps.json)
#
# Structure:  { location_id: { "layer_source:fid": { field: value } } }
# Scoped by location_id so stamps from Location A don't leak into Location B.
# ---------------------------------------------------------------------------

_stamp_cache: Optional[Dict[str, dict]] = None  # full file contents
_stamp_cache_path: Optional[_pathlib.Path] = None  # resolved per-project
_active_location_id: str = ""  # set by build_payload / stamp_ids_after_push


def _get_stamp_cache_path() -> Optional[_pathlib.Path]:
    """Return the stamp cache path derived from the current QGIS project file.

    Returns None if the project hasn't been saved yet.
    """
    global _stamp_cache_path
    project_path = QgsProject.instance().absoluteFilePath()
    if not project_path:
        return None
    p = _pathlib.Path(project_path)
    new_path = p.parent / f"{p.stem}_loc_stamps.json"
    if _stamp_cache_path != new_path:
        # Project changed — invalidate in-memory cache
        global _stamp_cache
        _stamp_cache = None
        _stamp_cache_path = new_path
    return new_path


def _stamp_cache_key(layer: QgsVectorLayer, fid: int) -> str:
    """Build a stable cache key from the layer's data source and feature ID."""
    return f"{layer.source()}:{fid}"


def _migrate_stamps_for_routes(
    routes: List["Route"],
    location_id: str,
) -> int:
    """Re-key stamp cache entries after route regeneration.

    Memory layer UIDs change when routes are regenerated, breaking the
    primary cache key (layer.source():fid).  This scans cached entries
    for a ``_route_name`` field and copies them under the new key so
    subsequent ``_safe_value`` lookups succeed.

    Returns the number of entries migrated.
    """
    cache = _load_stamp_cache()
    loc_stamps = cache.get(location_id)
    if not loc_stamps:
        return 0

    project = QgsProject.instance()

    # Build reverse index: route_name → (old_key, entry)
    by_route_name: Dict[str, tuple] = {}
    for old_key, entry in loc_stamps.items():
        rname = entry.get("_route_name", "")
        if rname:
            by_route_name[rname] = (old_key, entry)

    if not by_route_name:
        return 0

    migrated = 0
    for route in routes:
        layer = project.mapLayer(route.line_layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue
        new_key = _stamp_cache_key(layer, route.line_feature_id)

        # Check if the existing entry (if any) belongs to THIS route.
        # After Shapefile FID renumbering, FID N may now point to a
        # different route's cache entry.
        existing = loc_stamps.get(new_key, {})
        if existing.get("_route_name") == route.line_name:
            continue  # Correctly linked — no migration needed

        old_entry = by_route_name.get(route.line_name)
        if old_entry is None:
            continue
        _, entry = old_entry
        loc_stamps[new_key] = entry
        migrated += 1

    if migrated:
        _save_stamp_cache(cache)
        QgsMessageLog.logMessage(
            f"Migrated {migrated} stamp cache entries to current layers",
            "LOC Export", Qgis.MessageLevel.Info,
        )
    return migrated


def _load_stamp_cache() -> Dict[str, dict]:
    """Load the full stamp cache from disk (or return the in-memory copy)."""
    global _stamp_cache
    if _stamp_cache is not None:
        return _stamp_cache
    path = _get_stamp_cache_path()
    if path is not None and path.exists():
        try:
            _stamp_cache = _json.loads(path.read_text("utf-8"))
            if not isinstance(_stamp_cache, dict):
                _stamp_cache = {}
        except Exception:
            _stamp_cache = {}
    else:
        _stamp_cache = {}
    return _stamp_cache


def _save_stamp_cache(cache: Dict[str, dict]) -> None:
    """Persist the stamp cache to disk next to the project file."""
    global _stamp_cache
    _stamp_cache = cache
    path = _get_stamp_cache_path()
    if path is None:
        QgsMessageLog.logMessage(
            "Cannot save stamp cache: QGIS project not saved yet. "
            "Save the project first so LOC IDs persist across sessions.",
            "LOC Export", Qgis.MessageLevel.Warning,
        )
        return
    try:
        path.write_text(
            _json.dumps(cache, default=str), "utf-8",
        )
    except Exception as exc:
        QgsMessageLog.logMessage(
            f"Failed to save stamp cache: {exc}",
            "LOC Export", Qgis.MessageLevel.Warning,
        )


def _get_stamp_value(layer: QgsVectorLayer, fid: int,
                     field_name: str) -> str:
    """Look up a stamped value from the sidecar cache for the active location."""
    if not _active_location_id:
        return ""
    cache = _load_stamp_cache()
    loc_stamps = cache.get(_active_location_id, {})
    key = _stamp_cache_key(layer, fid)
    entry = loc_stamps.get(key, {})
    return entry.get(field_name, "")


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

    deleted_ids = payload.get("_deleted_loc_ids", [])
    if deleted_ids:
        lines.append(
            f"  Will be deleted on server (absent from payload): "
            f"{len(deleted_ids)}"
        )

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

    # Preserve timestamps from stamp cache so server skips reprocessing
    # unchanged entries.  Fresh `now` only for genuinely new entries.
    cached_ca = _safe_value(line_feature, "_createdAt") if line_feature else ""
    cached_ua = _safe_value(line_feature, "_updatedAt") if line_feature else ""
    cached_dca = _safe_value(line_feature, "_dest_createdAt") if line_feature else ""
    cached_dua = _safe_value(line_feature, "_dest_updatedAt") if line_feature else ""

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
        # Unmatched routes have no origin structure — fall back to the
        # line name so the asset is identifiable in LOC.
        actual_asset_name = route.origin or route.line_name

    # Resolve destination from mapped "Destination" field
    dest_qfield = mapping.qgis_field_for("Destination")
    destination = ""
    if dest_qfield and line_feature is not None:
        destination = _safe_value(line_feature, dest_qfield)
    if not destination:
        destination = route.destination

    # Bump timestamps if field values changed since last push
    cached_hash = _safe_value(line_feature, "_fields_hash") if line_feature else ""
    if cached_hash:
        current_hash = _fields_fingerprint(
            origin_sub, dest_sub,
            unique_asset_id, actual_asset_name, destination,
        )
        if current_hash != cached_hash:
            cached_ua = now
            cached_dua = now

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
        "createdAt": cached_ca or now,
        "updatedAt": cached_ua or now,
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
            "createdAt": cached_dca or now,
            "updatedAt": cached_dua or now,
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
    matched_stop_id = ""
    matched_created_at = ""
    matched_updated_at = ""
    orig_stop_num = 0
    if stop_id_map:
        coord_key = (round(pt_xy[0], 8), round(pt_xy[1], 8))
        id_list = stop_id_map.get(coord_key)
        if id_list:
            entry = id_list.pop(0)
            loc_id = entry[0]
            origin_id = entry[1] if len(entry) > 1 else ""
            exact_lon = entry[2] if len(entry) > 2 else 0.0
            exact_lat = entry[3] if len(entry) > 3 else 0.0
            matched_stop_id = entry[4] if len(entry) > 4 else ""
            matched_created_at = entry[5] if len(entry) > 5 else ""
            matched_updated_at = entry[6] if len(entry) > 6 else ""
            orig_stop_num = entry[7] if len(entry) > 7 else 0
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
        "createdAt": matched_created_at or now,
        # Bump updatedAt when stopNumber changed (signals server to reprocess)
        "updatedAt": (now if (orig_stop_num and orig_stop_num != stop_number)
                      else (matched_updated_at or now)),
        "location_id": location_id,
        "user_id": user_id,
        "category_name": cat_name,
        "category_type": "single",
        "name": f"Stop_{stop_number}_{route.line_name}",
    }

    # Merge numbered top-level field keys
    entry.update(top_fields)

    # Stash matched stop_id for caller (stripped before sending to server)
    if matched_stop_id:
        entry["_matched_stop_id"] = matched_stop_id

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
) -> Tuple[List[dict], List[dict]]:
    """Build single_locs entries for every feature in point layers
    mapped to single-type categories.

    Returns (entries, feature_map).
    """
    entries: List[dict] = []
    fmap: List[dict] = []

    for lm in layer_mappings:
        if not lm.enabled_for_export:
            continue

        cat = categories.get(lm.category_id)
        if not cat or not cat.is_single:
            continue

        layer = project.mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer):
            continue

        # Only point layers
        if layer.geometryType() != GEOM_POINT:
            continue

        cat_fields = cat.fields
        uid_qfield = lm.qgis_field_for("Unique Asset Identifier")
        name_qfield = lm.qgis_field_for("Actual Asset Name")
        fallback_qfield = lm.first_mapped_qgis_field()

        _set_current_layer(layer)
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
                entries.append(result)
                fmap.append({"layer_id": lm.layer_id, "feature_id": feat.id(),
                             "type": "single", "index": len(entries) - 1})
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
            sl_ca = _safe_value(feat, "_createdAt") or now
            sl_ua = _safe_value(feat, "_updatedAt") or now
            wrapper_id = _safe_value(feat, "_wrapper_id") or str(uuid4())

            # Bump timestamp if field values changed since last push
            cached_hash = _safe_value(feat, "_fields_hash")
            if cached_hash:
                current_hash = _fields_fingerprint(
                    sub_fields, unique_asset_id, actual_asset_name,
                )
                if current_hash != cached_hash:
                    sl_ua = now

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
                "createdAt": sl_ca,
                "updatedAt": sl_ua,
            }
            attrs.update(top_fields)

            entries.append({
                "id": wrapper_id,
                "category": lm.category_id,
                "coordinates": [pt_xy[0], pt_xy[1]],
                "unique_asset_id": unique_asset_id,
                "actual_asset_name": actual_asset_name,
                "name": actual_asset_name,
                "fields": sub_fields,
                "attributes": attrs,
            })
            fmap.append({"layer_id": lm.layer_id, "feature_id": feat.id(),
                         "type": "single", "index": len(entries) - 1})

    return entries, fmap


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

    # Bump updatedAt if any user-editable value actually changed.
    # Coordinates are already handled above (only overwritten on real move).
    # Check fields sub-dict and identity fields against the raw original.
    _changed = False
    if (patched.get("origin_longitude") != raw_sloc.get("origin_longitude")
            or patched.get("origin_latitude") != raw_sloc.get("origin_latitude")):
        _changed = True
    if patched.get("unique_asset_id") != raw_sloc.get("unique_asset_id"):
        _changed = True
    if patched.get("actual_asset_name") != raw_sloc.get("actual_asset_name"):
        _changed = True
    if patched.get("fields") != raw_sloc.get("fields"):
        _changed = True
    if _changed:
        patched["updatedAt"] = now

    # Push endpoint expects "category_id" (not "category") for single_locs.
    # The fetch response uses "category"; ensure the push-required key exists.
    patched["category_id"] = lm.category_id
    patched["category_name"] = lm.category_name
    patched["category_type"] = "single"
    patched["LOC_type"] = "single"
    if location_id:
        patched["location_id"] = location_id

    wrapper_id = _safe_value(feat, "_wrapper_id") or str(uuid4())

    return {
        "id": wrapper_id,
        "category": lm.category_id,
        "coordinates": [pt_xy[0], pt_xy[1]],
        "unique_asset_id": unique_asset_id,
        "actual_asset_name": actual_asset_name,
        "name": actual_asset_name,
        "fields": patched.get("fields", {}),
        "attributes": patched,
    }


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

    # Bump updatedAt if any user-editable value actually changed.
    _changed = False
    if (patched.get("origin_longitude") != raw.get("origin_longitude")
            or patched.get("origin_latitude") != raw.get("origin_latitude")):
        _changed = True
    if patched.get("unique_asset_id") != raw.get("unique_asset_id"):
        _changed = True
    if patched.get("actual_asset_name") != raw.get("actual_asset_name"):
        _changed = True
    if patched.get("fields") != raw.get("fields"):
        _changed = True
    raw_dest = raw.get("LOCDestination") or {}
    pat_dest = patched.get("LOCDestination") or {}
    if (pat_dest.get("longitude") != raw_dest.get("longitude")
            or pat_dest.get("latitude") != raw_dest.get("latitude")):
        _changed = True
    if pat_dest.get("destination") != raw_dest.get("destination"):
        _changed = True
    if pat_dest.get("destination_fields") != raw_dest.get("destination_fields"):
        _changed = True
    if _changed:
        patched["updatedAt"] = now
        if isinstance(patched.get("LOCDestination"), dict):
            patched["LOCDestination"]["updatedAt"] = now

    # Ensure push-required keys are present (from-scratch path sets these)
    patched["category"] = mapping.category_id
    patched["category_name"] = mapping.category_name
    patched["category_type"] = "dual"
    patched["LOC_type"] = "dual"
    if location_id:
        patched["location_id"] = location_id

    return patched


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
                "LOC", Qgis.MessageLevel.Info,
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

            # If stopNumber changed, bump updatedAt and patch name
            orig_snum = matched_entry.get("stopNumber", 0)
            if orig_snum and orig_snum != idx:
                patched_sloc["updatedAt"] = now
                patched_sloc["name"] = f"Stop_{idx}_{route.line_name}"

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
            # Prefer coord-matched stop_id, then positional fallback
            sid = single_loc.pop("_matched_stop_id", "")
            if not sid:
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

# Tracks the layer that the most recently retrieved/registered feature
# belongs to.  Used by _safe_value to look up stamp-cache entries without
# needing the layer threaded through every function call.
# This works because _get_feature / _set_current_layer is always called
# immediately before _safe_value for the same feature.
_current_stamp_layer: Optional[QgsVectorLayer] = None


def _set_current_layer(layer: QgsVectorLayer) -> None:
    """Set the active layer for stamp-cache lookups in _safe_value."""
    global _current_stamp_layer
    _current_stamp_layer = layer


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
    _set_current_layer(layer)
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
    Falls back to the sidecar stamp cache for non-pulled features.
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
) -> Dict[Tuple[float, float], List[tuple]]:
    """Parse a ``_stop_loc_ids`` attribute string into a coord→ID map.

    Format: ``"lon,lat=locid,originid,exactlon,exactlat,stopid,createdAt,updatedAt,stopNumber|..."``
    Returns ``{(lon, lat): [(loc_id, origin_id, exact_lon, exact_lat, stop_id, createdAt, updatedAt, stopNumber), ...]}``.

    Multiple entries at the same coordinate (from ingress/egress pairs)
    are stored as a list; callers pop from the front so each stop at
    the same location gets its own unique server ID and exact coords.
    """
    result: Dict[Tuple[float, float], List[tuple]] = {}
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
            # stop_id (5th field, added in v1.4+)
            stop_id = id_parts[4] if len(id_parts) > 4 else ""
            # Timestamps (6th/7th fields, added in v1.4+)
            created_at = id_parts[5] if len(id_parts) > 5 else ""
            updated_at = id_parts[6] if len(id_parts) > 6 else ""
            # Original stopNumber (8th field, added in v1.4+)
            orig_stop_num = 0
            if len(id_parts) > 7:
                try:
                    orig_stop_num = int(id_parts[7])
                except (ValueError, TypeError):
                    pass
            key = (round(lon, 8), round(lat, 8))
            result.setdefault(key, []).append(
                (loc_id, origin_id, exact_lon, exact_lat,
                 stop_id, created_at, updated_at, orig_stop_num)
            )
    return result


def _safe_value(feature: QgsFeature, field_name: str) -> str:
    """Read a feature attribute, returning '' for NULL/missing values.

    For ``_`` prefixed tracking fields, the sidecar stamp cache is checked
    FIRST (it always has full-length UUIDs), then falls back to the feature
    attribute.  This avoids using truncated values from Shapefile layers
    where the old field-based approach may have written partial UUIDs.
    """
    is_tracking = field_name.startswith("_")

    # For tracking fields, prefer the stamp cache (always correct).
    # Uses _current_stamp_layer (set by _get_feature / _set_current_layer)
    # + feature.id() to construct the cache key.
    if is_tracking and _current_stamp_layer is not None:
        cached = _get_stamp_value(
            _current_stamp_layer, feature.id(), field_name)
        if cached:
            return cached
    # Feature attribute (works for pulled memory layers + regular fields)
    try:
        val = feature.attribute(field_name)
    except Exception:
        return ""
    if val is None:
        return ""
    text = str(val)
    if text == "NULL":
        return ""
    # Guard against truncated UUIDs left by old field-based stamping on
    # Shapefiles.  UUID fields end in "_id"; valid UUIDs are 36 chars.
    if is_tracking and field_name.endswith("_id") and text:
        if len(text) != 36:
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
