"""Bring structures captured in the field into the layers the user maps.

"Pull now" in the Map Is Behind LOC prompt used to run a full pull: every
LOC record arrived again in new "(LOC)" layers and the category mapping
moved to them, so a design project lost its mapping, and remapping both
sets made every stop a duplicate (Brandon, 15 Nov 2026).  The prompt only
ever needs the records the map has never seen, and they belong in the
layers already mapped to their category.

Only standalone structures (single LOCs) are placed.  Cables and route
stops captured in the field stay in LOC, which a push keeps; the caller
lists them.
"""

import json
from uuid import uuid4
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorDataProvider,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..models.category import Category
from ..models.mapping import LayerMapping
from ..qt_compat import GEOM_POINT
from . import export_builder as eb
from .import_builder import _build_field_name_maps, _clean_val, _float

_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
_UID, _NAME = "Unique Asset Identifier", "Actual Asset Name"


class PlaceReport:
    """``placed``  ``(asset id, layer name)`` added to the map.
    ``skipped`` ``(asset id, reason)`` left in LOC (a push keeps them).
    ``placed_ids`` their loc_ids, which the project now holds."""

    def __init__(self):
        self.placed: List[Tuple[str, str]] = []
        self.skipped: List[Tuple[str, str]] = []
        self.placed_ids: List[str] = []


def _named_values(sloc: dict, fmap: Dict[str, str]) -> Dict[str, str]:
    """``{LOC field name: value}`` for a single LOC, as the pull reads it."""
    out = {_UID: sloc.get("unique_asset_id") or "",
           _NAME: sloc.get("actual_asset_name") or ""}
    for key, name in fmap.items():
        if sloc.get(key):
            out[name] = _clean_val(sloc[key])
    fields = sloc.get("fields")
    if isinstance(fields, dict):
        for key, val in fields.items():
            name = fmap.get(key, "")
            if name:
                if name in fields or not val:
                    continue        # a stale field_N never beats the named key
                out[name] = _clean_val(val)
            elif val:
                out[key] = _clean_val(val)
    return out


def _point_layer_for(category_id: str, mappings: Iterable[LayerMapping]):
    project = QgsProject.instance()
    for lm in mappings:
        if lm.category_id != category_id or not lm.enabled_for_export:
            continue
        layer = project.mapLayer(lm.layer_id)
        if isinstance(layer, QgsVectorLayer) and layer.geometryType() == GEOM_POINT:
            return lm, layer
    return None, None


def _geometry(sloc: dict, layer: QgsVectorLayer) -> QgsGeometry:
    geom = QgsGeometry.fromPointXY(QgsPointXY(
        _float(sloc.get("origin_longitude")), _float(sloc.get("origin_latitude"))))
    if layer.crs().isValid() and layer.crs() != _WGS84:
        geom.transform(QgsCoordinateTransform(_WGS84, layer.crs(), QgsProject.instance()))
    wkb = layer.wkbType()
    if QgsWkbTypes.isMultiType(wkb):
        geom.convertToMultiType()
    if QgsWkbTypes.hasZ(wkb):
        geom.get().addZValue(0)
    if QgsWkbTypes.hasM(wkb):
        geom.get().addMValue(0)
    return geom


def _stored(field, value):
    """*value* as *field* holds it, or raise ValueError when the field
    cannot hold it exactly (too long, not a number, a fraction in an
    integer).  A value written short or rounded would be pushed back over
    LOC's, and a missing asset ID would rename the record."""
    text = str(value)
    if not field.isNumeric():
        if field.length() > 0 and len(text) > field.length():
            raise ValueError(f"is limited to {field.length()} characters")
        return text
    try:
        out = field.convertCompatible(value)
    except (ValueError, TypeError):
        raise ValueError("cannot hold this value")
    if out is None or str(out) == "NULL":
        raise ValueError("cannot hold this value")
    try:
        if float(out) != float(text):
            raise ValueError("cannot hold this value exactly")
    except (TypeError, ValueError) as exc:
        if "exactly" in str(exc):
            raise
        if str(out).strip() != text.strip():
            raise ValueError("cannot hold this value exactly")
    return out


def _set(feat: QgsFeature, layer: QgsVectorLayer, name: str, value) -> None:
    """Set an attribute; ValueError (with the reason) when it cannot be
    stored exactly."""
    idx = layer.fields().indexOf(name)
    if idx < 0 or value in (None, ""):
        return
    try:
        feat.setAttribute(idx, _stored(layer.fields().at(idx), value))
    except ValueError as exc:
        raise ValueError(f"{layer.name()}.{name} {exc} ('{value}')") from None


def _uids_on(layer: QgsVectorLayer, field: str) -> set:
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return set()
    return {str(f.attribute(idx)).strip() for f in layer.getFeatures()
            if f.attribute(idx) not in (None, "")}


def place_field_captures(
    locs_data: dict,
    loc_ids: Iterable[str],
    layer_mappings: List[LayerMapping],
    categories: Dict[str, Category],
    location_id: str,
) -> PlaceReport:
    """Add the single LOCs *loc_ids* to the point layers mapped to their
    category; record each as held by the project and stamp its LOC ids so
    the next push updates it rather than creating it again."""
    report = PlaceReport()
    wanted = set(loc_ids)
    if not wanted:
        return report
    singles = {s.get("loc_id"): s for s in (locs_data or {}).get("singleLOCs", []) or []
               if s.get("loc_id") in wanted}
    index = eb.server_loc_index(locs_data or {})
    for lid in sorted(wanted - set(singles), key=lambda i: index.get(i, ("", ""))):
        uid, ltype = index.get(lid, ("", ""))
        what = "cable" if ltype == "dual" else "route stop"
        report.skipped.append((uid or "(no asset ID)",
                               f"{what} captured in the field; LOC keeps it"))

    fmaps = _build_field_name_maps(list(categories.values()))
    batches: Dict[str, Tuple[QgsVectorLayer, LayerMapping, list]] = {}
    for lid, sloc in sorted(singles.items(), key=lambda kv: kv[1].get("unique_asset_id") or ""):
        uid = (sloc.get("unique_asset_id") or "").strip()
        cat_id = sloc.get("category") or sloc.get("category_id") or ""
        cat = categories.get(cat_id)
        cat_name = cat.name if cat else "Default"
        if not uid:
            report.skipped.append(("(no asset ID)", "has no asset ID to match on"))
            continue
        lm, layer = _point_layer_for(cat_id, layer_mappings)
        if layer is None:
            report.skipped.append((uid, f"no point layer is mapped to {cat_name}"))
            continue
        uid_field = lm.qgis_field_for(_UID)
        if not uid_field:
            report.skipped.append((uid, f"{layer.name()} has no field mapped to {_UID}"))
            continue
        caps = layer.dataProvider().capabilities()
        if not caps & QgsVectorDataProvider.Capability.AddFeatures:
            report.skipped.append((uid, f"{layer.name()} is read-only"))
            continue
        if layer.isEditable():
            report.skipped.append((uid, f"{layer.name()} is being edited; save or "
                                        "stop editing, then try again"))
            continue
        batch = batches.setdefault(layer.id(), (layer, lm, []))
        if not batch[2]:
            batch[2].append(_uids_on(layer, uid_field))
        if uid in batch[2][0]:
            report.skipped.append((uid, f"{layer.name()} already has a feature "
                                        "with this asset ID"))
            continue
        batch[2][0].add(uid)

        feat = QgsFeature(layer.fields())
        feat.setGeometry(_geometry(sloc, layer))
        values = _named_values(sloc, fmaps.get(cat_id, {}))
        try:
            for fm in lm.field_mappings:
                if fm.qgis_field:
                    _set(feat, layer, fm.qgis_field, values.get(fm.loc_field))
            # A layer made by a pull carries the pulled copy, which the
            # merge on the next push compares against.
            _set(feat, layer, "_loc_id", lid)
            _set(feat, layer, "_origin_id", sloc.get("origin_id"))
            _set(feat, layer, "_origin_lon", str(sloc.get("origin_longitude", "")))
            _set(feat, layer, "_origin_lat", str(sloc.get("origin_latitude", "")))
            _set(feat, layer, "_sloc_json", json.dumps(sloc, default=str))
        except ValueError as exc:
            batch[2][0].discard(uid)
            report.skipped.append((uid, str(exc)))
            continue
        batch[2].append((lid, uid, sloc, feat))

    placed: List[Tuple[QgsVectorLayer, int, str, str, dict]] = []
    for layer, _lm, items in batches.values():
        todo = items[1:]
        if not todo:
            continue
        ok, added = layer.dataProvider().addFeatures([t[3] for t in todo])
        if not ok or len(added) != len(todo):
            for _lid, uid, _s, _f in todo:
                report.skipped.append((uid, f"{layer.name()} refused the new feature"))
            continue
        layer.updateExtents()
        layer.triggerRepaint()
        for (lid, uid, sloc, _f), feat in zip(todo, added):
            placed.append((layer, feat.id(), lid, uid, sloc))

    if placed:
        eb._active_location_id = location_id
        eb.set_layer_mappings(layer_mappings)
        cache = eb._load_stamp_cache()
        stamps = cache.setdefault(location_id, {})
        for layer, fid, lid, uid, sloc in placed:
            stamps[eb._stamp_cache_key(layer, fid)] = {
                "_loc_id": lid,
                "_origin_id": sloc.get("origin_id", ""),
                "_unique_asset_id": uid,
                "_createdAt": sloc.get("createdAt", ""),
                "_updatedAt": sloc.get("updatedAt", ""),
                "_wrapper_id": sloc.get("id") or str(uuid4()),
                "_feature_key": eb._feature_key(layer, fid),
            }
            report.placed.append((uid, layer.name()))
            report.placed_ids.append(lid)
        eb._save_stamp_cache(cache)
        eb.remember_loc_ids(location_id, report.placed_ids)
        remember_field_captures(report.placed_ids)
    return report


#: Project entry listing the LOC ids Pull now placed.  LOC had not put
#: these on any route when they were captured, so the next Generate may
#: make them stops (a pulled structure off a route is otherwise left off).
_CAPTURES_SCOPE, _CAPTURES_KEY = "LOC", "field_capture_ids"


def field_capture_ids(project=None) -> set:
    from qgis.core import QgsProject
    project = project or QgsProject.instance()
    ids, _ok = project.readListEntry(_CAPTURES_SCOPE, _CAPTURES_KEY)
    return {i for i in ids if i}


def remember_field_captures(loc_ids, project=None) -> None:
    from qgis.core import QgsProject
    project = project or QgsProject.instance()
    ids = field_capture_ids(project) | {str(i) for i in loc_ids if i}
    project.writeEntry(_CAPTURES_SCOPE, _CAPTURES_KEY, sorted(ids))


def forget_field_captures(project=None) -> None:
    """A full pull: LOC's routes are now what the project holds."""
    from qgis.core import QgsProject
    (project or QgsProject.instance()).removeEntry(_CAPTURES_SCOPE, _CAPTURES_KEY)
