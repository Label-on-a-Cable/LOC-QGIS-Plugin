"""Deleting LOCs in QGIS must delete them in LOC — also in a capture-mode
project, where the server skips the push's delete sweep (loc-next M0.5,
merge mode) and the plugin has to bin what the user removed itself.

Real pushes, staging only:

    LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh \\
        LOC_LIVE_LOCATION=Howth tests/run_live_videos.sh --allow-push -k delete

Reported 9 Nov 2026 on production (USA, every project in capture mode):
"I deleted some LOCs on my QGIS map and the plugin flags that they will get
deleted. Yet when I push, they don't get deleted in LOC."
"""

import pytest
from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes

from test_live_chain import _index
# the `lv` / `target` fixtures live in test_live_videos
from test_live_videos import (  # noqa: F401
    act, backup, inspector, lv, new_project, open_location, pull,
    push_for_real, restore, sign_in, target, title, _location_id,
)

pytestmark = pytest.mark.live


def _standalone_feature(plugin, data):
    """A pulled point that is a standalone structure (not a route stop)."""
    singles = {s["loc_id"] for s in data.get("singleLOCs", [])}
    for lm in plugin.layer_mappings:
        layer = QgsProject.instance().mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not lm.category_id:
            continue
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            continue
        if layer.fields().indexOf("_loc_id") < 0:
            continue
        for f in layer.getFeatures():
            if str(f["_loc_id"]) in singles:
                return layer, f
    raise AssertionError("no standalone structure to delete")


def _route_feature(plugin, data):
    """A pulled route line, with its dual and stop loc_ids."""
    by_multi = {m["id"]: m for m in data.get("multiLoc", [])}
    for r in plugin.cached_routes:
        layer = QgsProject.instance().mapLayer(r.line_layer_id)
        if layer is None or layer.fields().indexOf("_multi_id") < 0:
            continue
        f = layer.getFeature(r.line_feature_id)
        m = by_multi.get(str(f["_multi_id"]))
        if m:
            return layer, f, m
    raise AssertionError("no pulled route to delete")


def _without_clashing_records(backup_data, current):
    """The backup minus the records LOC must re-create whose asset ID is
    held by another record now (whole routes go if any of their records
    clash), or None when nothing clashes."""
    import copy
    a, c = _index(backup_data), _index(current)
    held = {(r.get("unique_asset_id") or ""): k[1] for k, r in c.items()}
    clash = {k[1] for k, r in a.items()
             if k not in c and r.get("unique_asset_id")
             and held.get(r["unique_asset_id"], k[1]) != k[1]}
    if not clash:
        return None
    out = copy.deepcopy(backup_data)
    out["singleLOCs"] = [s for s in out.get("singleLOCs", []) if s["loc_id"] not in clash]
    out["dualLOCs"] = [d for d in out.get("dualLOCs", []) if d["loc_id"] not in clash]

    def route_ids(m):
        return {(m.get("dualLoc") or {}).get("loc_id")} | {
            (s.get("singleLoc") or {}).get("loc_id") for s in m.get("Stops", [])}
    out["multiLoc"] = [m for m in out.get("multiLoc", []) if not route_ids(m) & clash]
    return out


def _delete(layer, fid):
    assert layer.dataProvider().deleteFeatures([fid])
    layer.triggerRepaint()


@pytest.mark.live_write
@title("Staging · delete a structure and a route in QGIS → push → are they gone in LOC?")
def test_live_deleted_in_qgis_is_deleted_in_loc(lv):
    sign_in(lv)
    open_location(lv)
    before = backup(lv, "delete")
    project_id = lv.plugin.active_location.project.project_id
    cap = bool(lv.plugin.api.get_project(project_id).get("capture_mode"))
    act(lv, f"Project capture mode: {cap}")
    pull(lv)

    s_layer, s_feat = _standalone_feature(lv.plugin, before)
    s_lid = str(s_feat["_loc_id"])
    r_layer, r_feat, multi = _route_feature(lv.plugin, before)
    r_lid = multi["dualLoc"]["loc_id"]
    _delete(s_layer, s_feat.id())
    _delete(r_layer, r_feat.id())
    act(lv, f"Action: delete structure {s_lid[:8]}… and route {multi.get('multiName')} "
            "from the QGIS layers")
    # The deleted line leaves the route list (what Generate would do).
    lv.plugin.cached_routes = [r for r in lv.plugin.cached_routes
                               if not (r.line_layer_id == r_layer.id()
                                       and r.line_feature_id == r_feat.id())]

    seen = push_for_real(lv)
    prompts = [c for c in seen.get("dialogs", []) if c[1] == "LOCs Will Be Deleted"]
    after = lv.plugin.api.get_locs_for_location(_location_id(lv))
    a, b = _index(before), _index(after)
    gone = {k[1] for k in set(a) - set(b)}
    multis_after = {m["id"] for m in after.get("multiLoc", [])}
    log = [
        f"capture mode: {cap}",
        f"deletion prompt shown: {bool(prompts)}",
        f"structure {s_lid[:8]}… gone from LOC: {s_lid in gone}",
        f"route {multi.get('multiName')} gone from LOC: {multi['id'] not in multis_after}",
        f"route cable {r_lid[:8]}… gone from LOC: {r_lid in gone}",
        f"records before {len(a)}, after {len(b)}",
    ]
    ok = bool(prompts) and s_lid in gone and multi["id"] not in multis_after and r_lid in gone
    inspector(lv, "deleted in QGIS → pushed", log,
              "Deleted in QGIS, deleted in LOC" if ok
              else "Deleted in QGIS but still in LOC", ok)

    # Clean up: push the backup.  LOC re-creates the deleted records under
    # their original loc_id.  (Not the recycle-bin restore: it re-creates a
    # record under a NEW id and does not rebuild routes — 9 Nov 2026 that
    # left Howth with route OM_FIB_144_1 as 68 loose records.)
    restore(lv, before)
    final = lv.plugin.api.get_locs_for_location(_location_id(lv))
    missing = set(_index(before)) - set(_index(final))
    log.append(f"after cleanup: {len(missing)} record(s) of the backup missing")
    assert not missing, f"cleanup left {len(missing)} records missing"
    assert ok, "\n".join(log)


@pytest.mark.live_write
@title("Staging · restore a location from a backup file (LOC_LIVE_RESTORE_FILE)")
def test_live_restore_backup_file(lv):
    """Push a saved backup back through the plugin.  Records missing from
    the server are re-created under their original loc_id; in a normal
    project the push also bins anything the backup does not hold."""
    import json
    import os
    path = os.environ.get("LOC_LIVE_RESTORE_FILE")
    if not path:
        pytest.skip("set LOC_LIVE_RESTORE_FILE to a backup JSON")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    sign_in(lv)
    open_location(lv)
    current = lv.plugin.api.get_locs_for_location(_location_id(lv))
    first = _without_clashing_records(data, current)
    if first is not None:
        # Records the backup re-creates whose asset ID a different record
        # now holds (a recycle-bin restore re-creates under a NEW id).
        # LOC keeps asset IDs unique per location and does not match route
        # records by asset ID, so pushing the backup straight away fails
        # (HTTP 500).  First push it without them, which in a normal
        # project removes the stand-ins, then push the whole backup.
        act(lv, "Cleanup step 1: push the backup without the records LOC "
                "must re-create, to clear the stand-ins holding their asset IDs")
        restore(lv, first)
    restore(lv, data)
    final = lv.plugin.api.get_locs_for_location(_location_id(lv))
    missing = set(_index(data)) - set(_index(final))
    extra = set(_index(final)) - set(_index(data))
    assert not missing and not extra, f"missing {len(missing)}, extra {len(extra)}"
