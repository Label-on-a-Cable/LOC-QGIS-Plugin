"""A continuous pull → push → edit → push → pull → revert → push chain
against a real LOC server, with real pushes (``--allow-push``, staging
only).  Same shape as the fake-server chain in
``TestEditCycles::test_push_edit_push_pull_edit_pull_delete_chain``.

    LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=... \\
        LOC_LIVE_LOCATION=... tests/run_live_videos.sh --allow-push -k chain

The location is backed up first; the chain ends with the server compared
against that backup.
"""

import pytest
from qgis.core import QgsVectorLayer, QgsWkbTypes

from helpers import pump
# the `lv` / `target` fixtures live in test_live_videos
from test_live_videos import (  # noqa: F401
    act, backup, inspector, lv, new_project, open_location, preview_then_cancel,
    pull, push_for_real, sign_in, target, title, _location_id, _updated_total,
)

pytestmark = pytest.mark.live

_ESSENTIAL = {"Unique Asset Identifier", "Actual Asset Name", "Route ID"}


def _counts(stats):
    def n(k):
        try:
            return int(stats.get(k, "0"))
        except ValueError:
            return 0
    return n("Created LOCs"), _updated_total(stats), n("Deleted LOCs")


def _pick_editable_point(plugin):
    """A pulled point layer, its mapping, one mapped category field and the
    first feature that carries a value for it."""
    from qgis.core import QgsProject
    for lm in plugin.layer_mappings:
        layer = QgsProject.instance().mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not lm.category_id:
            continue
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            continue
        uid_q = lm.qgis_field_for("Unique Asset Identifier")
        for fm in lm.field_mappings:
            if not fm.qgis_field or fm.loc_field in _ESSENTIAL or not uid_q:
                continue
            for f in layer.getFeatures():
                v = f[fm.qgis_field]
                if v not in (None, "") and str(v) not in ("NULL", "N/A"):
                    return layer, lm, fm, f, str(v), str(f[uid_q])
    raise AssertionError("no pulled point layer with a mapped, populated category field")


def _set(layer, fid, field, value):
    idx = layer.fields().indexOf(field)
    assert idx >= 0, field
    assert layer.dataProvider().changeAttributeValues({fid: {idx: value}})
    layer.triggerRepaint()


def _single_on_server(lv, uid):
    data = lv.plugin.api.get_locs_for_location(_location_id(lv))
    return next(s for s in data["singleLOCs"] if s.get("unique_asset_id") == uid), data


def _index(data):
    out = {}
    for s in data.get("singleLOCs", []):
        out[("single", s["loc_id"])] = s
    for s in data.get("dualLOCs", []):
        out[("dual", s["loc_id"])] = s
    for m in data.get("multiLoc", []):
        dl = m.get("dualLoc") or {}
        out[("route", dl.get("loc_id"))] = dl
        for st in m.get("Stops", []):
            sl = st.get("singleLoc") or {}
            out[("stop", sl.get("loc_id"))] = sl
    return out


_KEEP = ("unique_asset_id", "actual_asset_name", "category", "fields", "createdAt",
         "origin_longitude", "origin_latitude")


def compare_to_backup(before, after, edited_uid):
    """Lines describing every difference that matters, and whether it is clean."""
    a, b = _index(before), _index(after)
    lines, clean = [], True
    gone, new = set(a) - set(b), set(b) - set(a)
    if gone or new:
        clean = False
        lines.append(f"records removed: {len(gone)}, added: {len(new)}")
        for k in list(gone)[:5]:
            lines.append(f"  removed {k[0]} {a[k].get('unique_asset_id')}")
        for k in list(new)[:5]:
            lines.append(f"  added   {k[0]} {b[k].get('unique_asset_id')}")
    bumped = []
    for k in set(a) & set(b):
        x, y = a[k], b[k]
        diff = {f: (x.get(f), y.get(f)) for f in _KEEP if x.get(f) != y.get(f)}
        if diff:
            clean = False
            lines.append(f"  changed {k[0]} {x.get('unique_asset_id')}: "
                         + ", ".join(f"{f} {o!r} → {n!r}" for f, (o, n) in diff.items()))
        elif x.get("updatedAt") != y.get("updatedAt"):
            bumped.append((k[0], x.get("unique_asset_id")))
    unexpected = [bu for bu in bumped if bu[1] != edited_uid]
    lines.append(f"records with a new updatedAt: {len(bumped)} "
                 f"(expected only the edited {edited_uid})")
    for bu in unexpected[:10]:
        lines.append(f"  bumped {bu[0]} {bu[1]}")
    if unexpected:
        clean = False
    return lines, clean


@pytest.mark.live_write
@title("Staging chain · pull → push → edit → push → pull → revert → push → compare with backup")
def test_live_pull_push_chain(lv):
    sign_in(lv)
    open_location(lv)
    before = backup(lv, "chain")
    pull(lv)
    log = []

    def step(n, what, stats, want):
        got = _counts(stats)
        ok = got == want
        log.append(f"{n:>2}. {what:<58} created/updated/deleted = {got}  "
                   f"{'ok' if ok else 'EXPECTED ' + str(want)}")
        act(lv, f"Result: {what}: {got}", kind="ok" if ok else "bug")
        return ok

    results = []
    # 1. unedited push
    seen = push_for_real(lv)
    results.append(step(1, "unedited pull → push", seen["stats"], (0, 0, 0)))

    # 2. edit one category field of one structure → push
    layer, lm, fm, feat, old, uid = _pick_editable_point(lv.plugin)
    _set(layer, feat.id(), fm.qgis_field, "QA-CHAIN")
    act(lv, f"Action: set {fm.loc_field} of {uid} ({lm.category_name}) "
            f"from {old!r} to 'QA-CHAIN'")
    seen = push_for_real(lv)
    results.append(step(2, f"{uid}.{fm.loc_field} edited → push", seen["stats"], (0, 1, 0)))
    rec, _ = _single_on_server(lv, uid)
    on_server = (rec.get("fields") or {}).get(fm.loc_field)
    results.append(on_server == "QA-CHAIN")
    log.append(f"    server now holds {fm.loc_field} = {on_server!r}")

    # 3. re-push with nothing else edited
    seen = push_for_real(lv)
    results.append(step(3, "same project pushed again", seen["stats"], (0, 0, 0)))

    # 4. pull into a new project: the edit is there, nothing to push
    new_project(lv, "chain_second")
    pull(lv)
    layer2, lm2, fm2, feat2, val2, uid2 = _pick_editable_point(lv.plugin)
    pulled_val = None
    for lm_ in lv.plugin.layer_mappings:
        from qgis.core import QgsProject
        l_ = QgsProject.instance().mapLayer(lm_.layer_id)
        if isinstance(l_, QgsVectorLayer) and lm_.category_id == lm.category_id:
            q_uid = lm_.qgis_field_for("Unique Asset Identifier")
            q_fld = lm_.qgis_field_for(fm.loc_field)
            for f in l_.getFeatures():
                if q_uid and str(f[q_uid]) == uid:
                    pulled_val = str(f[q_fld]) if q_fld else None
                    layer2, fid2, qf2 = l_, f.id(), q_fld
    results.append(pulled_val == "QA-CHAIN")
    log.append(f" 4. fresh pull shows {uid}.{fm.loc_field} = {pulled_val!r} "
               f"{'ok' if pulled_val == 'QA-CHAIN' else 'EXPECTED QA-CHAIN'}")
    seen = preview_then_cancel(lv)
    results.append(step(5, "fresh pull, unedited dry run", seen["stats"], (0, 0, 0)))

    # 6. revert in the new project → push
    _set(layer2, fid2, qf2, old)
    act(lv, f"Action: set {fm.loc_field} of {uid} back to {old!r}")
    seen = push_for_real(lv)
    results.append(step(6, "reverted in the new project → push", seen["stats"], (0, 1, 0)))

    # 7. pull once more → unedited push for real
    new_project(lv, "chain_third")
    pull(lv)
    seen = push_for_real(lv)
    results.append(step(7, "pulled again, unedited real push", seen["stats"], (0, 0, 0)))

    # 8. the server against the backup
    _, after = _single_on_server(lv, uid)
    lines, clean = compare_to_backup(before, after, uid)
    results.append(clean)
    log.append(" 8. server vs backup: " + ("identical apart from the edited record's updatedAt"
                                            if clean else "DIFFERS"))
    log += ["    " + ln for ln in lines]
    ok = all(results)
    inspector(lv, f"{lv.t.location}: pull → push chain", log,
              "Chain clean: every unedited push 0 / 0, each edit exactly 1 update, "
              "server identical to the backup" if ok else "Chain found a difference", ok)
    assert ok, "\n".join(log)
