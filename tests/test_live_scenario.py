"""One user's session on a pulled location, end to end, against a real LOC
server (staging) — the flow found by hand on Alabama › Clanton, 24 Nov 2026:

    pull → push unchanged → edit a cable in the attribute table → delete a
    cable → add a cable (drawn on the map) and a row with no line →
    Generate → Toggle Type on an IN stop → Remove a passthrough → push →
    compare LOC and the location's history → push again → pull again →
    redo the toggles the other way → try to break it.

    LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh \\
        LOC_LIVE_LOCATION=Howth LOC_LIVE_CAPTURE=on|off \\
        tests/run_live_videos.sh --allow-push -k live_scenario

Fake server: ``tests/run_tests.sh -m live --no-cov --live-target=fake -k
live_scenario`` with ``LOC_LIVE_CAPTURE=on|off``.

Every push is checked three ways: the prompts (titles, the exact assets the
deletion prompt lists), LOC record by record against what this session did
(any other difference fails), and the location's history (one "Moved LOC …
to the recycle bin" row per deleted LOC; on loc-next the push's summary row
counts match what changed).  The ``mx`` fixture backs the location up first
and pushes the backup back afterwards.  Results go to ``matrix/scenario.jsonl``.
"""

import json
import re

import pytest
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject
from qgis.PyQt.QtWidgets import QMessageBox

from label_on_a_cable.models.route import StopType
from test_live_matrix import (  # noqa: F401  (fixtures)
    STAMP, diff, expect, lv, mx, records, route_lids, routes, target,
)
from test_live_merge import _history, push, user, user_delete, user_set  # noqa: F401
from test_live_videos import (
    _layer, act, close_review, generate, new_project, pull, review_route, title,
)

pytestmark = [pytest.mark.live, pytest.mark.live_write]
RESULTS_FILE = "scenario.jsonl"
SB = QMessageBox.StandardButton
_ESSENTIAL = {"Unique Asset Identifier", "Actual Asset Name", "Route ID"}


# ----------------------------------------------------------------------
# The pulled map: routes, their server records, picks
# ----------------------------------------------------------------------

def _mid(route):
    f = _layer(route.line_layer_id).getFeature(route.line_feature_id)
    return str(f["_multi_id"] or "") if f.fields().indexOf("_multi_id") >= 0 else ""


def _server_stops(data, mid):
    """loc_ids of route *mid*'s stops, in stop order."""
    return list(routes(data)[mid][2])


def _stop_lid(data, route, stop):
    """The server record of *stop* on a freshly pulled, unedited *route*:
    intermediate stops line up with the server's stops in order."""
    return _server_stops(data, _mid(route))[route.intermediate_stops.index(stop)]


def _uid(data, lid):
    rec = records(data).get(lid)
    return (rec[1].get("unique_asset_id") or "") if rec else ""


def _pulled_routes(lv, data):
    rs = [r for r in lv.plugin.cached_routes if _mid(r) in routes(data)]
    assert rs, "no pulled routes on this location"
    return rs


def pick(lv, data):
    """Routes for the session, smallest first so the diffs stay readable:
    *pair* has an IN/OUT pair, *pas* a passthrough at its own position
    (may be the same route), *gone* is deleted, *edit* gets a field edit."""
    rs = sorted(_pulled_routes(lv, data), key=lambda r: len(r.stops))

    def own_spot(r, s):
        at = [x for x in r.intermediate_stops if x.point_feature_id == s.point_feature_id
              and x.point_layer_id == s.point_layer_id]
        return len(at) == 1

    pair = next(r for r in rs if any(s.stop_type == StopType.INGRESS for s in r.stops))
    # Removing the only stop of a route turns it into a cable without stops
    # (the mode-dependent case below); here the route keeps other stops.
    pas = next((r for r in [pair] + rs if len(r.intermediate_stops) >= 2 and any(
        s.stop_type == StopType.PASSTHROUGH and s.point_feature_id >= 0 and own_spot(r, s)
        for s in r.intermediate_stops)), None)
    assert pas is not None, "no route with a passthrough stop"
    taken = {id(pair), id(pas)}
    # Another route; failing that a cable without stops (a dual LOC).
    others = sorted((r for r in lv.plugin.cached_routes if id(r) not in taken),
                    key=lambda r: (_mid(r) not in routes(data), len(r.stops)))
    assert others, "no third cable to delete"
    gone = others[0]
    taken.add(id(gone))
    edit = next((r for r in rs if id(r) not in taken and edit_field(lv, r)), None) \
        or next((r for r in (pair, pas) if edit_field(lv, r)), None)
    return pair, pas, gone, edit


def edit_field(lv, route):
    """A mapped origin field of *route*'s cable layer (one LOC keeps in
    ``fields``), or None."""
    lm = next(m for m in lv.plugin.layer_mappings if m.layer_id == route.line_layer_id)
    cat = lv.plugin._categories_cache.get(lm.category_id)
    origin = {cf.name for cf in (cat.fields if cat else [])}
    return next((fm for fm in lm.field_mappings if fm.qgis_field and fm.loc_field in origin
                 and fm.loc_field not in _ESSENTIAL), None)


def route_mapping(lv, route):
    return next(m for m in lv.plugin.layer_mappings if m.layer_id == route.line_layer_id)


# ----------------------------------------------------------------------
# What the user does on the map and in Route Review
# ----------------------------------------------------------------------

def add_cable(lv, route_like, rid, xy=None):
    """Draw a cable in *route_like*'s layer: a straight line between its
    origin and destination structures (Add Line Feature, snapped), or a row
    with no line when *xy* is False (Add Feature in the attribute table)."""
    layer = _layer(route_like.line_layer_id)
    lm = route_mapping(lv, route_like)
    f = QgsFeature(layer.fields())
    f.setAttribute(lm.qgis_field_for("Route ID"), rid)
    name_q = lm.qgis_field_for("Actual Asset Name")
    if name_q:
        f.setAttribute(name_q, f"QA {rid}")
    if xy is not False:
        a, b = xy or _ends(route_like)
        f.setGeometry(QgsGeometry.fromPolylineXY([a, b]))
    assert layer.startEditing() or layer.isEditable()
    assert layer.addFeature(f)
    assert layer.commitChanges(), layer.commitErrors()
    act(lv, f"Action: {'attribute table → Add Feature' if xy is False else 'Add Line Feature'}"
            f" → Route ID {rid} → Save Edits")
    return next(x.id() for x in layer.getFeatures()
                if str(x[lm.qgis_field_for("Route ID")]) == rid)


def _ends(route):
    pts = []
    for s in (route.stops[0], route.stops[-1]):
        lyr = _layer(s.point_layer_id)
        f = lyr.getFeature(s.point_feature_id) if lyr is not None else None
        pts.append(f.geometry().asPoint() if f is not None and f.isValid() else None)
    if None in pts:
        g = _layer(route.line_layer_id).getFeature(route.line_feature_id).geometry()
        v = g.asPolyline() or [p for part in g.asMultiPolyline() for p in part]
        pts = [QgsPointXY(v[0]), QgsPointXY(v[-1])]
    return pts


def find(lv, line_layer_id, line_feature_id):
    return next(r for r in lv.plugin.cached_routes
                if (r.line_layer_id, r.line_feature_id) == (line_layer_id, line_feature_id))


def toggle(lv, route, stop):
    dlg = review_route(lv, route, stop)
    dlg._on_toggle_type()
    act(lv, f"Action: Route Review → {route.line_name} → select {stop.display_name} → Toggle Type")


def remove(lv, route, stop):
    dlg = review_route(lv, route, stop)
    dlg._on_remove()
    act(lv, f"Action: Route Review → {route.line_name} → select {stop.display_name} → "
            f"{'Remove' if stop.removed else 'Restore'}")


def regenerate(lv):
    """Generate; a review with edits asks before discarding them (Yes)."""
    lv.msgbox.answers = [SB.Yes]
    generate(lv)
    lv.msgbox.answers = []


# ----------------------------------------------------------------------
# Checks: the deletion prompt, LOC, the history
# ----------------------------------------------------------------------

def listed(box):
    """Asset ids the deletion prompt lists."""
    return {m.group(1).strip() for m in re.finditer(r"^\s+- (.+?)\s+\((?:single|dual|multi)\)",
                                                   (box or {}).get("text", ""), re.M)}


def check_prompt(mx, boxes, want_uids):
    box = boxes.get("LOCs Will Be Deleted")
    got = listed(box)
    ok = box is not None and got == set(want_uids) and box.get("default") == "No"
    lines = [f"  listed  {u}" for u in sorted(got)]
    lines += [f"  MISSING {u}" for u in sorted(set(want_uids) - got)]
    lines += [f"  EXTRA   {u}" for u in sorted(got - set(want_uids))]
    return mx.step("deletion prompt lists exactly what this session removed",
                   f"{len(want_uids)} asset(s), default No",
                   f"{len(got)} listed, default {box.get('default') if box else None}", ok, lines)


def history_rows(mx):
    raw = _history(mx.api, mx.lv.plugin.active_location.location_id)
    rows = raw.get("data") or raw.get("logs") or [] if isinstance(raw, dict) else raw or []
    return [r for r in rows if isinstance(r, dict)]


def _key(r):
    return r.get("id") or r.get("_id") or json.dumps(r, sort_keys=True, default=str)


def check_history(mx, before, removed_uids, d):
    """New history rows since *before*: a recycle-bin row per removed
    LOC, and on loc-next a push summary whose counts match the diff."""
    seen = {_key(r) for r in before}
    new = [r for r in history_rows(mx) if _key(r) not in seen]
    descs = [str(r.get("description") or r.get("msg") or "") for r in new]
    lines = [f"  {x[:120]}" for x in descs[:12]] + (
        [f"  … {len(descs) - 12} more"] if len(descs) > 12 else [])
    missing = [u for u in removed_uids
               if not any(x == f"Moved LOC {u} to the recycle bin from the QGIS plugin"
                          for x in descs)]
    lines += [f"  MISSING recycle-bin row for {u}" for u in missing]
    mx.step("history: one recycle-bin row per deleted LOC",
            f"{len(removed_uids)} row(s)", f"{len(removed_uids) - len(missing)} found",
            not missing, lines)

    summary = next((r for r in new if str(r.get("description", "")).startswith("Pushed ")
                    and "from the QGIS plugin" in str(r.get("description", ""))), None)
    if summary is None:
        return mx.step("history: push summary row", "loc-next writes one per push",
                       "none (fake server or older LOC writes no summary)", True)
    p = summary.get("payload") or {}
    added, removed = len(d["added"]), len(d["removed"])
    created_rows = sum(x.startswith("Created LOC ") for x in descs)
    notes = [f"  summary: {json.dumps(p)}",
             f"  diff: {added} created, {len(d['changed'])} changed, {removed} removed",
             f"  per-LOC rows: {created_rows} created"]
    ok = p.get("locs_created") == added == created_rows
    # In a capture-mode project the push bins nothing itself (the plugin
    # does, through delete-many), so its summary counts no removals.
    if not mx.capture:
        ok = ok and p.get("locs_removed") == removed
    return mx.step("history: push summary matches LOC", "created/removed as in the diff",
                   f"created {p.get('locs_created')}, updated {p.get('locs_updated')}, "
                   f"removed {p.get('locs_removed')}", ok, notes)


def check_loc(mx, what, before, after, new_route_names=(), **kw):
    """``expect`` with routes this session created allowed."""
    d = diff(before, after)
    rb = routes(after)
    d["r_added"] = {m for m in d["r_added"] if rb[m][0] not in new_route_names}
    ok, lines = expect(d, **kw)
    mx.step(what, "only the records named below differ",
            "as expected" if ok else "server differs from expected", ok, lines)
    return d


def unchanged(mx, what, before):
    return check_loc(mx, what, before, mx.server())


# ----------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------

@pytest.fixture
def cautious(mx):
    """An unplanned Yes/No prompt is answered No during the session.  Set
    back before ``mx`` restores the backup, which relies on Yes."""
    mx.lv.msgbox.default = SB.No
    yield
    mx.lv.msgbox.default = SB.Yes
    mx.lv.msgbox.answers = []


@title("Scenario · pull → edit, delete, add → Toggle Type, Remove → push → "
       "check LOC and history → again → try to break it")
def test_live_scenario_user_session(mx, user, cautious):
    lv = mx.lv
    base = mx.baseline

    # 1 · pull, push unchanged ----------------------------------------
    pull(lv)
    push(mx, user, "1 · pull → push unchanged", titles=[], want=(0, 0))
    unchanged(mx, "1 · LOC unchanged", base)

    # 2 · edit, delete, add, Generate, Toggle Type, Remove, push -------
    pair, pas, gone, edit = pick(lv, base)
    p_stop = next(s for s in pair.stops if s.stop_type == StopType.INGRESS)
    out_stop = next(s for s in pair.stops if s.stop_type == StopType.EGRESS
                    and s.stop_number == p_stop.stop_number)
    in_lid, out_lid = _stop_lid(base, pair, p_stop), _stop_lid(base, pair, out_stop)
    pass_stop = next(s for s in pas.intermediate_stops if s.stop_type == StopType.PASSTHROUGH
                     and s.point_feature_id >= 0 and s is not p_stop
                     and sum(x.point_feature_id == s.point_feature_id
                             and x.point_layer_id == s.point_layer_id
                             for x in pas.intermediate_stops) == 1)
    pass_lid = _stop_lid(base, pas, pass_stop)
    gone_mid = _mid(gone)
    edit_mid = _mid(edit) if edit is not None else None
    if gone_mid in routes(base):
        gone_lids = set(route_lids(base, gone_mid))
    else:                                   # a cable without stops
        gone_mid = None
        gone_lids = {str(_layer(gone.line_layer_id).getFeature(gone.line_feature_id)["_loc_id"])}
    edit_lid = routes(base)[edit_mid][1] if edit_mid in routes(base) else None
    keys = {k: (r.line_layer_id, r.line_feature_id) for k, r in
            dict(pair=pair, pas=pas).items()}
    act(lv, f"Picks: toggle {p_stop.display_name} on {pair.line_name}; remove "
            f"{pass_stop.display_name} on {pas.line_name}; delete {gone.line_name}; "
            f"edit {edit.line_name if edit else '(no cable with a mapped field)'}")

    fm = edit_field(lv, edit) if edit_lid else None
    if fm is not None:
        user_set(lv, _layer(edit.line_layer_id), edit.line_feature_id, fm.qgis_field,
                 f"QA{STAMP}", f"{edit.line_name}.{fm.loc_field}")
    ends = _ends(gone)                       # the new cable replaces it, end to end
    user_delete(lv, _layer(gone.line_layer_id), gone.line_feature_id, gone.line_name)
    new_rid, nogeo_rid = f"QA-{STAMP}-NEW", f"QA-{STAMP}-NOLINE"
    add_cable(lv, gone, new_rid, xy=ends)
    nogeo_fid = add_cable(lv, gone, nogeo_rid, xy=False)

    regenerate(lv)
    pair, pas = (find(lv, *keys["pair"]), find(lv, *keys["pas"]))
    p_stop = next(s for s in pair.stops if s.stop_type == StopType.INGRESS
                  and s.original_name == p_stop.original_name)
    toggle(lv, pair, p_stop)
    pass_stop = next(s for s in pas.stops if s.original_name == pass_stop.original_name)
    remove(lv, pas, pass_stop)
    close_review(lv)

    want_gone = gone_lids | {out_lid, pass_lid}
    hist0 = history_rows(mx)
    boxes = push(mx, user, "2 · edits, delete, new cable, Toggle Type, Remove → push",
                 plan={"Features Excluded From Push": "Yes", "LOCs Will Be Deleted": "Yes"},
                 titles=["Features Excluded From Push", "LOCs Will Be Deleted"])
    excl = boxes.get("Features Excluded From Push", {}).get("text", "")
    mx.step("excluded-features prompt names the row with no line",
            f"no geometry: {nogeo_rid}", excl.replace("\n", " ")[:200],
            f"no geometry: {nogeo_rid}" in excl and new_rid not in excl)
    check_prompt(mx, boxes, {_uid(base, lid) for lid in want_gone})
    s2 = mx.server()
    d = check_loc(mx, "2 · LOC after the push", base, s2, new_route_names=(new_rid,),
                  removed=want_gone, routes_removed=[gone_mid] if gone_mid else [],
                  changed=dict({in_lid: ["unique_asset_id", "actual_asset_name"]},
                               **({edit_lid: ["fields"]} if fm is not None else {})),
                  routes_changed=[_mid(pair), _mid(pas)] if _mid(pair) != _mid(pas)
                  else [_mid(pair)],
                  allow_changed=set(route_lids(base, _mid(pair))) | set(route_lids(base, _mid(pas))),
                  added_like=[new_rid])
    in_rec = records(s2).get(in_lid, (None, {}))[1]
    mx.step("toggled stop is one passthrough record", "asset id and name without _IN",
            f"{in_rec.get('unique_asset_id')} / {in_rec.get('actual_asset_name')}",
            not str(in_rec.get("unique_asset_id", "")).endswith("_IN")
            and not str(in_rec.get("actual_asset_name", "")).endswith("_IN"))
    if fm is not None:
        got = (records(s2).get(edit_lid, (None, {}))[1].get("fields") or {}).get(fm.loc_field)
        mx.step(f"{fm.loc_field} edit landed", f"QA{STAMP}", str(got), got == f"QA{STAMP}")
    check_history(mx, hist0, sorted(_uid(base, lid) for lid in want_gone), d)

    # 3 · push again from the same map: nothing changes -----------------
    push(mx, user, "3 · push again, same map", plan={"Features Excluded From Push": "Yes"},
         titles=["Features Excluded From Push"], want=(0, 0))
    unchanged(mx, "3 · LOC unchanged", s2)
    user_delete(lv, _layer(gone.line_layer_id), nogeo_fid, nogeo_rid)

    # 4 · pull again into a new project: round trip -------------------
    new_project(lv, "again")
    pull(lv)
    push(mx, user, "4 · pull again → push unchanged", titles=[], want=(0, 0))
    unchanged(mx, "4 · LOC unchanged", s2)
    regenerate(lv)
    close_review(lv)
    push(mx, user, "4 · Generate → push unchanged", titles=[], want=(0, 0))
    unchanged(mx, "4 · LOC unchanged after Generate", s2)

    # 5 · redo the other way: toggle the stop back to IN/OUT ----------
    pair_name = pair.line_name
    regenerate(lv)                       # no layer change: Route Review reopens
    pair = next(r for r in lv.plugin.cached_routes if r.line_name == pair_name)
    back = pair.intermediate_stops[_server_stops(s2, _mid(pair)).index(in_lid)]
    toggle(lv, pair, back)
    close_review(lv)
    hist1 = history_rows(mx)
    push(mx, user, "5 · Toggle Type back to IN/OUT → push", titles=[])
    s5 = mx.server()
    d5 = check_loc(mx, "5 · LOC: the pair is back, as a new OUT record", s2, s5,
                   changed={in_lid: ["unique_asset_id", "actual_asset_name"]},
                   routes_changed=[_mid(pair)], allow_changed=set(route_lids(s2, _mid(pair))),
                   added_like=["_OUT"])
    in_rec = records(s5).get(in_lid, (None, {}))[1]
    mx.step("stop is the IN half again", "asset id ends _IN",
            str(in_rec.get("unique_asset_id")), str(in_rec.get("unique_asset_id")).endswith("_IN"))
    check_history(mx, hist1, [], d5)

    # 6 · try to break it (nothing below may change LOC) ---------------
    regenerate(lv)
    close_review(lv)
    pair = find(lv, pair.line_layer_id, pair.line_feature_id)
    any_route = pair

    # a) a structure row with no geometry → excluded prompt → No
    pt_layer = _layer(next(s for s in pair.stops if s.point_feature_id >= 0).point_layer_id)
    pt_lm = next(m for m in lv.plugin.layer_mappings if m.layer_id == pt_layer.id())
    row = QgsFeature(pt_layer.fields())
    row.setAttribute(pt_lm.qgis_field_for("Unique Asset Identifier"), f"QA-{STAMP}-NOPT")
    assert pt_layer.startEditing() or pt_layer.isEditable()
    assert pt_layer.addFeature(row) and pt_layer.commitChanges()
    act(lv, f"Action: attribute table → Add Feature QA-{STAMP}-NOPT (no point) → Save Edits")
    boxes = push(mx, user, "6a · point with no geometry → No",
                 plan={"Features Excluded From Push": "No"},
                 titles=["Features Excluded From Push"], push_sent=False)
    mx.step("6a · prompt names it", f"no geometry: QA-{STAMP}-NOPT",
            boxes.get("Features Excluded From Push", {}).get("text", "")[:160],
            f"QA-{STAMP}-NOPT" in boxes.get("Features Excluded From Push", {}).get("text", ""))
    nopt = next(f.id() for f in pt_layer.getFeatures()
                if str(f[pt_lm.qgis_field_for("Unique Asset Identifier")]) == f"QA-{STAMP}-NOPT")
    user_delete(lv, pt_layer, nopt, f"QA-{STAMP}-NOPT")

    # b) a cable drawn after Generate, pushed without Generate → No
    late = add_cable(lv, any_route, f"QA-{STAMP}-LATE")
    boxes = push(mx, user, "6b · cable drawn after Generate → No",
                 plan={"Features Excluded From Push": "No"},
                 titles=["Features Excluded From Push"], push_sent=False)
    txt = boxes.get("Features Excluded From Push", {}).get("text", "")
    mx.step("6b · prompt names it as not generated", f"not in the generated routes: QA-{STAMP}-LATE",
            txt.replace("\n", " ")[:200], f"not in the generated routes: QA-{STAMP}-LATE" in txt)
    user_delete(lv, _layer(any_route.line_layer_id), late, f"QA-{STAMP}-LATE")

    # c) Toggle Type twice and Remove → Restore: back where it was
    regenerate(lv)
    pair = find(lv, pair.line_layer_id, pair.line_feature_id)
    ing = next(s for s in pair.stops if s.stop_type == StopType.INGRESS)
    name = ing.original_name
    toggle(lv, pair, ing)
    toggle(lv, pair, next(s for s in pair.stops if s.original_name == name))
    extra = next((s for s in pair.intermediate_stops if s.stop_type == StopType.PASSTHROUGH),
                 None)
    if extra is not None:
        remove(lv, pair, extra)
        remove(lv, pair, extra)
    close_review(lv)
    push(mx, user, "6c · Toggle Type twice, Remove → Restore → push", titles=[], want=(0, 0))

    # d) delete a stop's structure → the deletion prompt → No
    st = next(s for s in pair.intermediate_stops if s.point_feature_id >= 0)
    lyr = _layer(st.point_layer_id)
    user_delete(lv, lyr, st.point_feature_id, st.structure_name)
    regenerate(lv)
    close_review(lv)
    push(mx, user, "6d · delete a stop's structure → No at the deletion prompt",
         plan={"LOCs Will Be Deleted": "No"}, titles=["LOCs Will Be Deleted"], push_sent=False)

    # e) two structures with one asset id → Push Warning → No
    lyr = max((_layer(m.layer_id) for m in lv.plugin.layer_mappings
               if m.qgis_field_for("Unique Asset Identifier")
               and _layer(m.layer_id) is not None
               and _layer(m.layer_id).geometryType() == pt_layer.geometryType()),
              key=lambda x: x.featureCount())
    if lyr.featureCount() >= 2:
        uid_q = next(m for m in lv.plugin.layer_mappings
                     if m.layer_id == lyr.id()).qgis_field_for("Unique Asset Identifier")
        f1, f2 = list(lyr.getFeatures())[:2]
        user_set(lv, lyr, f2.id(), uid_q, f1[uid_q], f"{f2[uid_q]} asset ID → {f1[uid_q]}")
        lv.msgbox.answers = [SB.No]
        n = len(lv.msgbox.calls)
        push(mx, user, "6e · duplicate asset id → No at Push Warning", push_sent=False)
        mx.step("6e · Push Warning shown", "Push Warning",
                ", ".join(c[1] for c in lv.msgbox.calls[n:]) or "none",
                "Push Warning" in [c[1] for c in lv.msgbox.calls[n:]])

    unchanged(mx, "6 · nothing above changed LOC", s5)
    s6 = mx.server()

    # 7 · Remove every stop of a route: it becomes a cable without stops --
    # The plugin sends it as a cable (dual LOC).  A normal project's push
    # replaces the location, so LOC drops the route row and keeps the
    # cable; a capture-mode push merges, so LOC keeps the route with no
    # stops (staging Howth, 24 Nov 2026 — open product question).
    new_project(lv, "empty")
    pull(lv)
    regenerate(lv)
    cand = sorted((r for r in _pulled_routes(lv, s6) if r.intermediate_stops
                   and all(x.point_feature_id >= 0 for x in r.intermediate_stops)),
                  key=lambda r: len(r.intermediate_stops))
    if not cand:
        close_review(lv)
        return
    emp = cand[0]
    mid = _mid(emp)
    stop_lids = set(_server_stops(s6, mid))
    for st in list(emp.intermediate_stops):
        if not st.removed:
            remove(lv, emp, st)
    close_review(lv)
    boxes = push(mx, user, f"7 · Remove every stop of {emp.line_name} → push",
                 plan={"LOCs Will Be Deleted": "Yes"}, titles=["LOCs Will Be Deleted"])
    check_prompt(mx, boxes, {_uid(s6, lid) for lid in stop_lids})
    s7 = mx.server()
    check_loc(mx, "7 · LOC: stops gone, the cable kept"
              + ("" if mx.capture else ", the route row gone"), s6, s7,
              removed=stop_lids,
              routes_removed=[] if mx.capture else [mid],
              routes_changed=[mid] if mx.capture else [],
              allow_changed=set(route_lids(s6, mid)))
    cable = routes(s6)[mid][1]
    mx.step("7 · the cable record is still in LOC", "kept", "kept" if cable in records(s7)
            else "gone", cable in records(s7))

