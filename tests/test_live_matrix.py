"""Edit → push use cases against a real LOC server, run once with the
project in capture mode and once without (real pushes, staging only).

    LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh \\
        LOC_LIVE_LOCATION=Howth LOC_LIVE_CAPTURE=on \\
        tests/run_live_videos.sh --allow-push -k matrix

``LOC_LIVE_CAPTURE`` (``on`` / ``off``) switches the location's project to
that mode before every scenario (``PATCH projects/{id}``), so run ``on``
first and ``off`` last to leave the project as it was.

Every scenario:

1. backs the location up,
2. drives the plugin as a user would (pull, edit, delete, add, Generate,
   push — real pushes, every prompt answered as stated),
3. re-reads the location and compares it with the backup record by record:
   each record that must change, appear or go is named, and **any other
   difference fails the scenario**,
4. restores the backup through the plugin and checks the location is back
   to the backup exactly.

Results are appended to ``tests/reports/videos/live/matrix/results.jsonl``
(one line per scenario and mode) for the verification report.
"""

import copy
import datetime
import json
import os
import time

import pytest
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer, QgsWkbTypes,
)

from helpers import pump, wait_until
from label_on_a_cable.models.route import StopType
from test_live_delete import _without_clashing_records
# the `lv` / `target` fixtures live in test_live_videos
from test_live_videos import (  # noqa: F401
    _layer, _location_id, act, close_review, find_route, generate, lv,
    new_project, open_location, pick_route, preview_then_cancel, pull,
    push_for_real, review_route, show_map, sign_in, target, title,
)

pytestmark = [pytest.mark.live, pytest.mark.live_write]

WANT = os.environ.get("LOC_LIVE_CAPTURE", "").lower()
STAMP = datetime.datetime.now().strftime("%H%M%S")
COORD_TOL = 1e-7          # degrees, ~1 cm
CONTENT = ("unique_asset_id", "actual_asset_name", "category", "fields")


# ----------------------------------------------------------------------
# Server state, compared record by record
# ----------------------------------------------------------------------

def records(data):
    """loc_id → (role, record) for every LOC the location holds."""
    out = {}
    for s in data.get("singleLOCs", []):
        out[s["loc_id"]] = ("structure", s)
    for d in data.get("dualLOCs", []):
        out[d["loc_id"]] = ("cable", d)
    for m in data.get("multiLoc", []):
        dl = m.get("dualLoc") or {}
        out[dl.get("loc_id")] = ("route cable", dl)
        for st in m.get("Stops", m.get("stops", [])):
            sl = st.get("singleLoc") or {}
            out.setdefault(sl.get("loc_id"), ("route stop", sl))
    return out


def routes(data):
    """multi id → (name, cable loc_id, stop loc_ids in order)."""
    out = {}
    for m in data.get("multiLoc", []):
        stops = sorted(m.get("Stops", m.get("stops", [])), key=lambda s: s.get("stopNumber", 0))
        out[m["id"]] = (m.get("multiName"), (m.get("dualLoc") or {}).get("loc_id"),
                        tuple((s.get("singleLoc") or {}).get("loc_id") for s in stops))
    return out


def _coord_diff(a, b):
    out = {}
    for k in ("origin_longitude", "origin_latitude"):
        try:
            if abs(float(a.get(k) or 0) - float(b.get(k) or 0)) > COORD_TOL:
                out[k] = (a.get(k), b.get(k))
        except (TypeError, ValueError):
            if a.get(k) != b.get(k):
                out[k] = (a.get(k), b.get(k))
    # a cable's far end
    da, db = a.get("LOCDestination") or {}, b.get("LOCDestination") or {}
    for k in ("longitude", "latitude"):
        try:
            if abs(float(da.get(k) or 0) - float(db.get(k) or 0)) > COORD_TOL:
                out[f"destination_{k}"] = (da.get(k), db.get(k))
        except (TypeError, ValueError):
            if da.get(k) != db.get(k):
                out[f"destination_{k}"] = (da.get(k), db.get(k))
    return out


def diff(before, after):
    a, b = records(before), records(after)
    changed = {}
    for lid in set(a) & set(b):
        (ra, x), (rb, y) = a[lid], b[lid]
        d = {k: (x.get(k), y.get(k)) for k in CONTENT if x.get(k) != y.get(k)}
        d.update(_coord_diff(x, y))
        if ra != rb:
            d["role"] = (ra, rb)
        if d:
            changed[lid] = d
    ra, rb = routes(before), routes(after)
    rchanged = {mid: (ra[mid], rb[mid]) for mid in set(ra) & set(rb) if ra[mid] != rb[mid]}
    return dict(removed=set(a) - set(b), added=set(b) - set(a), changed=changed,
                r_removed=set(ra) - set(rb), r_added=set(rb) - set(ra), r_changed=rchanged,
                a=a, b=b)


def _uid(d, lid):
    rec = d["a"].get(lid) or d["b"].get(lid)
    return (rec[1].get("unique_asset_id") if rec else None) or lid[:8]


def expect(d, removed=(), added_uids=(), changed=None, routes_removed=(),
           routes_changed=(), allow_changed=(), added_like=()):
    """Compare a diff with what the scenario must do.  *changed* maps
    loc_id → keys that must differ; *allow_changed* are loc_ids that may
    differ (records of an edited route).  Returns (ok, lines)."""
    changed = changed or {}
    lines, ok = [], True
    removed, routes_removed = set(removed), set(routes_removed)
    added = {lid: d["b"][lid][1].get("unique_asset_id") for lid in d["added"]}

    def bad(msg):
        nonlocal ok
        ok = False
        lines.append("  UNEXPECTED " + msg)

    for lid in removed:
        (lines.append(f"  removed  {_uid(d, lid)}  ok") if lid in d["removed"]
         else bad(f"{_uid(d, lid)} is still in LOC"))
    for lid in d["removed"] - removed:
        bad(f"{_uid(d, lid)} ({d['a'][lid][0]}) removed")
    for uid in added_uids:
        (lines.append(f"  added    {uid}  ok") if uid in added.values()
         else bad(f"{uid} was not created"))
    for sub in added_like:
        hits = [u for u in added.values() if sub in (u or "") and u not in added_uids]
        (lines.append(f"  added    {', '.join(hits)}  ok") if hits
         else bad(f"no record named like {sub} was created"))
    for lid, uid in added.items():
        if uid not in added_uids and not any(sub in (uid or "") for sub in added_like):
            bad(f"{uid} ({d['b'][lid][0]}) created")
    for lid, keys in changed.items():
        got = d["changed"].get(lid, {})
        missing = set(keys) - set(got)
        if missing:
            bad(f"{_uid(d, lid)}: {', '.join(sorted(missing))} not changed")
        else:
            lines.append(f"  changed  {_uid(d, lid)}: " + "; ".join(
                f"{k} {str(o)[:40]!r} → {str(n)[:40]!r}" for k, (o, n) in got.items()) + "  ok")
    for lid, got in d["changed"].items():
        if lid in changed:
            extra = set(got) - set(changed[lid])
            if extra:
                bad(f"{_uid(d, lid)}: also changed {', '.join(sorted(extra))}")
        elif lid not in allow_changed:
            bad(f"{_uid(d, lid)} changed: " + "; ".join(
                f"{k} {str(o)[:40]!r} → {str(n)[:40]!r}" for k, (o, n) in got.items()))
    for mid in routes_removed:
        (lines.append(f"  route    {mid[:8]} removed  ok") if mid in d["r_removed"]
         else bad(f"route {mid[:8]} is still in LOC"))
    for mid in d["r_removed"] - routes_removed:
        bad(f"route {mid[:8]} removed")
    for mid in d["r_added"]:
        bad(f"route {mid[:8]} created")
    for mid, (o, n) in d["r_changed"].items():
        if mid in routes_changed:
            lines.append(f"  route    {o[0]} → {n[0]}; stops {len(o[2])} → {len(n[2])}  ok")
        else:
            bad(f"route {o[0]} changed: name {o[0]!r} → {n[0]!r}, stops {len(o[2])} → {len(n[2])}")
    for mid in routes_changed:
        if mid not in d["r_changed"]:
            bad(f"route {mid[:8]} unchanged")
    n_other = len(d["a"]) - len(d["removed"]) - len([l for l in d["changed"]])
    lines.append(f"  {n_other} other record(s) unchanged")
    return ok, lines


# ----------------------------------------------------------------------
# Scenario harness
# ----------------------------------------------------------------------

def _int(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return 0


def counts(stats):
    c = sum(_int(v) for k, v in (stats or {}).items() if k.lower().startswith("created"))
    u = sum(_int(v) for k, v in (stats or {}).items() if k.lower().startswith("updated"))
    d = sum(_int(v) for k, v in (stats or {}).items() if k.lower().startswith("deleted"))
    return c, u, d


class Run:
    def __init__(self, lv, name):
        self.lv, self.name, self.steps = lv, name, []
        self.capture = None
        self.t0 = time.monotonic()

    @property
    def api(self):
        return self.lv.plugin.api

    def server(self):
        return self.api.get_locs_for_location(_location_id(self.lv))

    def step(self, what, expected, actual, ok, lines=()):
        self.steps.append(dict(step=what, expected=expected, actual=actual, ok=bool(ok),
                               detail=list(lines)))
        act(self.lv, f"{'PASS' if ok else 'FAIL'} · {what}: {actual}",
            kind="ok" if ok else "bug")
        return ok

    def push(self, what, want=None, answers=(), want_prompt=None, want_stale=False):
        """Push for real; check server dry-run counters (created, updated)
        and the prompts.  *want_prompt*: loc_ids the deletion prompt must
        list (None: no prompt)."""
        lv = self.lv
        lv.msgbox.answers = list(answers)
        seen = push_for_real(lv)
        dialogs = seen.get("dialogs", [])
        titles = [c[1] for c in dialogs]
        c, u, d = counts(seen.get("stats"))
        notes = [f"server dry run: created {c}, updated {u}, deleted {d}",
                 f"prompts: {', '.join(titles) or 'none'}"]
        ok = seen.get("pushed", False)
        if not ok:
            notes.append("push was not sent")
        for bad in ("Push Validation Failed", "Some LOCs Were Not Deleted",
                    "Push Failed", "Push — Server Warning"):
            if bad in titles:
                ok = False
                notes.append(f"{bad}: " + next(t for k, tt, t in dialogs if tt == bad)[:300])
        if want is not None and (c, u) != want:
            ok = False
            notes.append(f"EXPECTED created {want[0]}, updated {want[1]}")
        prompt = [t for k, tt, t in dialogs if tt == "LOCs Will Be Deleted"]
        if want_prompt is None and prompt:
            ok = False
            notes.append("UNEXPECTED deletion prompt: " + prompt[0][:300])
        if want_prompt is not None:
            if not prompt:
                ok = False
                notes.append("EXPECTED a deletion prompt")
            else:
                notes.append("deletion prompt: " + prompt[0].replace("\n", " ")[:300])
        stale = [t for k, tt, t in dialogs if tt == "Map Is Behind LOC"]
        if bool(stale) != want_stale:
            ok = False
            notes.append("UNEXPECTED " + ("missing" if want_stale else "")
                         + " stale-map warning" + (": " + stale[0][:200] if stale else ""))
        elif stale:
            notes.append("stale-map warning: " + stale[0].replace("\n", " ")[:300])
        bar = lv.iface.messageBar().last()
        if bar:
            notes.append(f"message bar: {bar[2]}")
        self.step(what, f"push sent, created/updated = {want}" if want else "push sent",
                  f"created {c}, updated {u}, deleted {d}", ok, notes)
        return seen, ok

    def check(self, what, before, after, **kw):
        ok, lines = expect(diff(before, after), **kw)
        return self.step(what, "only the records named below differ", "as expected" if ok
                         else "server differs from expected", ok, lines)


def set_capture(lv, want):
    pid = lv.plugin.active_location.project.project_id
    cur = bool(lv.plugin.api.get_project(pid).get("capture_mode"))
    if cur != want:
        lv.plugin.api._send("PATCH", f"projects/{pid}", json={"capture_mode": want})
    got = bool(lv.plugin.api.get_project(pid).get("capture_mode"))
    assert got == want, f"project capture mode is {got}, wanted {want}"
    return got


def restore_to(run, data):
    """Load *data* into a fresh project and push it: LOC ends equal to it.
    Records LOC holds now are marked as known, so in capture mode the
    plugin removes the ones *data* does not hold (the server's sweep does
    it in a normal project)."""
    from label_on_a_cable.core import export_builder as eb
    from label_on_a_cable.core import import_builder as ib
    from label_on_a_cable.models.category import Category
    lv = run.lv
    for part in (_without_clashing_records(data, run.server()), data):
        if part is None:
            continue
        current = set(records(run.server()))
        new_project(lv, "restore")
        cats = Category.list_from_api(lv.plugin.api.get_categories_by_location(_location_id(lv)))
        layers, maps, ids = ib.build_layers(part, cats)
        lv.plugin._on_pull_complete(layers, maps, ids, ib.extract_multi_stop_counts(part))
        lv.plugin._categories_cache = {c.category_id: c for c in cats}
        lv.plugin._pulled_loc_ids = set(lv.plugin._pulled_loc_ids) | current
        eb.remember_loc_ids(_location_id(lv), current)
        show_map(lv.iface)
        lv.msgbox.answers = []
        # A restore overwrites LOC on purpose: no merge with LOC's changes.
        from label_on_a_cable.core import rebase as rb
        real = rb.rebase_payload
        rb.rebase_payload = lambda payload, base, live: rb.RebaseReport()
        try:
            push_for_real(lv)
        finally:
            rb.rebase_payload = real


@pytest.fixture
def mx(lv, request):
    if WANT not in ("on", "off"):
        pytest.skip("set LOC_LIVE_CAPTURE=on or off")
    sign_in(lv)
    open_location(lv)
    run = Run(lv, request.node.name.replace("test_live_matrix_", ""))
    run.capture = set_capture(lv, WANT == "on")
    act(lv, f"Project capture mode: {run.capture}")
    run.baseline = run.server()
    from conftest import VIDEO_DIR
    out = VIDEO_DIR / "live" / "matrix"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"baseline-{run.name}-{WANT}-{STAMP}.json").write_text(json.dumps(run.baseline))
    yield run
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        msg = str(rep.longrepr).strip().splitlines()
        err = next((ln for ln in reversed(msg) if ln.startswith("E ")), msg[-1] if msg else "")
        run.step("scenario ran to the end", "no error", f"stopped: {err[:200]}", False,
                 msg[-12:])
    # Restore and prove it.
    try:
        restore_to(run, run.baseline)
        final = run.server()
        ok, lines = expect(diff(run.baseline, final))
        run.step("cleanup: backup pushed back", "location equals the backup",
                 "identical" if ok else "differs from the backup", ok, lines)
    except Exception as exc:  # report, then fail below
        run.step("cleanup: backup pushed back", "location equals the backup",
                 f"error: {exc}", False)
    rec = dict(scenario=run.name, title=getattr(request.function, "video_title", run.name),
               mode="capture on" if run.capture else "capture off",
               host=lv.t.base, location=f"{lv.t.gid} › {lv.t.location}",
               records=len(records(run.baseline)), routes=len(routes(run.baseline)),
               seconds=round(time.monotonic() - run.t0), steps=run.steps,
               ok=all(s["ok"] for s in run.steps), at=datetime.datetime.now().isoformat())
    with open(out / getattr(request.module, "RESULTS_FILE", "results.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"\n== {run.name} [{rec['mode']}] {'PASS' if rec['ok'] else 'FAIL'}")
    for s in run.steps:
        print(f"  {'PASS' if s['ok'] else 'FAIL'}  {s['step']}: {s['actual']}")
        for ln in s["detail"]:
            print(f"        {ln}")
    assert rec["ok"], f"{run.name}: a step failed (see output)"


# ----------------------------------------------------------------------
# Picking features on the pulled map
# ----------------------------------------------------------------------

_ESSENTIAL = {"Unique Asset Identifier", "Actual Asset Name", "Route ID"}


def _stop_ids(data):
    return {(st.get("singleLoc") or {}).get("loc_id")
            for m in data.get("multiLoc", []) for st in m.get("Stops", m.get("stops", []))}


def _xy(r):
    return (round(float(r.get("origin_longitude") or 0), 7),
            round(float(r.get("origin_latitude") or 0), 7))


def stops_on(data, rec):
    """loc_id → route id of the stop records standing on structure *rec*."""
    at = _xy(rec)
    return {(st.get("singleLoc") or {}).get("loc_id"): m["id"]
            for m in data.get("multiLoc", []) for st in m.get("Stops", m.get("stops", []))
            if _xy(st.get("singleLoc") or {}) == at}


def standalone_points(lv, data, n):
    """*n* pulled structures, each with a mapped, populated category field,
    fewest route stops first.  (On Howth all but one structure carry route
    stops.)  Each pick lists the stop records standing on it."""
    singles = {s["loc_id"]: s for s in data.get("singleLOCs", [])}
    cands = []
    for lm in lv.plugin.layer_mappings:
        layer = QgsProject.instance().mapLayer(lm.layer_id)
        if not isinstance(layer, QgsVectorLayer) or not lm.category_id:
            continue
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
            continue
        uid_q = lm.qgis_field_for("Unique Asset Identifier")
        if not uid_q or layer.fields().indexOf("_loc_id") < 0:
            continue
        fms = [fm for fm in lm.field_mappings if fm.qgis_field and fm.loc_field not in _ESSENTIAL]
        for f in layer.getFeatures():
            rec = singles.get(str(f["_loc_id"]))
            if rec is None:
                continue
            fm = next((fm for fm in fms if f[fm.qgis_field] not in (None, "")
                       and str(f[fm.qgis_field]) != "NULL"), None)
            if fm is None:
                continue
            cands.append(dict(layer=layer, lm=lm, fq=fm.qgis_field, lf=fm.loc_field,
                              uid_q=uid_q, fid=f.id(), lid=rec["loc_id"],
                              uid=str(f[uid_q]), old=str(f[fm.qgis_field]),
                              stops=stops_on(data, rec)))
    cands.sort(key=lambda c: (len(c["stops"]), c["uid"]))
    assert len(cands) >= n, f"only {len(cands)} structures with a mapped field"
    return cands[:n]


def find_point(lid):
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.fields().indexOf("_loc_id") >= 0:
            for f in layer.getFeatures():
                if str(f["_loc_id"]) == lid:
                    return layer, f
    raise AssertionError(f"{lid} not on the map")


def set_field(layer, fid, field, value):
    assert layer.dataProvider().changeAttributeValues(
        {fid: {layer.fields().indexOf(field): value}})
    layer.triggerRepaint()


def edit(lv, p, value):
    layer, f = find_point(p["lid"])
    lm = next(m for m in lv.plugin.layer_mappings if m.layer_id == layer.id())
    set_field(layer, f.id(), lm.qgis_field_for(p["lf"]), value)
    act(lv, f"Action: set {p['lf']} of {p['uid']} to {value!r}")


def delete(lv, p):
    layer, f = find_point(p["lid"])
    assert layer.dataProvider().deleteFeatures([f.id()])
    layer.triggerRepaint()
    act(lv, f"Action: delete {p['uid']} from the map")


def add_point(lv, near, uid, value):
    """A new structure in *near*'s layer, ~20 m from it."""
    layer, f = find_point(near["lid"])
    lm = next(m for m in lv.plugin.layer_mappings if m.layer_id == layer.id())
    pt = f.geometry().asPoint()
    nf = QgsFeature(layer.fields())
    nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pt.x() + 0.0003, pt.y() + 0.0001)))
    nf.setAttribute(lm.qgis_field_for("Unique Asset Identifier"), uid)
    name_q = lm.qgis_field_for("Actual Asset Name")
    if name_q:
        nf.setAttribute(name_q, f"QA {uid}")
    nf.setAttribute(lm.qgis_field_for(near["lf"]), value)
    assert layer.dataProvider().addFeatures([nf])[0]
    layer.triggerRepaint()
    act(lv, f"Action: add a new structure {uid} with {near['lf']} = {value!r}")


def server_rec(data, uid):
    return next((r for _role, r in records(data).values()
                 if r.get("unique_asset_id") == uid), None)


def field_on(data, lid, loc_field):
    rec = records(data).get(lid)
    return ((rec[1].get("fields") or {}).get(loc_field)) if rec else None


def route_lids(data, mid):
    m = next(m for m in data["multiLoc"] if m["id"] == mid)
    return {(m.get("dualLoc") or {}).get("loc_id")} | {
        (s.get("singleLoc") or {}).get("loc_id") for s in m.get("Stops", m.get("stops", []))}


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------

@title("Matrix 1 · pull → push, nothing edited")
def test_live_matrix_01_pull_push(mx):
    pull(mx.lv)
    mx.push("unedited pull → push", want=(0, 0))
    mx.check("LOC after the push", mx.baseline, mx.server())


@title("Matrix 2 · pull → edit a field → push → push again")
def test_live_matrix_02_pull_edit_push(mx):
    lv = mx.lv
    pull(lv)
    (p,) = standalone_points(lv, mx.baseline, 1)
    edit(lv, p, f"QA-MX2-{STAMP}")
    mx.push(f"{p['uid']}.{p['lf']} edited → push", want=(0, 1))
    after = mx.server()
    mx.check("LOC after the push", mx.baseline, after, changed={p["lid"]: ["fields"]})
    v = field_on(after, p["lid"], p["lf"])
    mx.step("edited value in LOC", f"QA-MX2-{STAMP}", repr(v), v == f"QA-MX2-{STAMP}")
    mx.push("same project pushed again, nothing edited", want=(0, 0))
    mx.check("LOC after the second push", after, mx.server())


def kept_stops(mx, q, after):
    """The deleted structure's route stops stay on their routes (stops with
    no structure are kept, 1.7.1 review decision; removing one is done in
    Route Review)."""
    if not q["stops"]:
        return
    left = [lid for lid in q["stops"] if lid in records(after)]
    mx.step(f"{q['uid']}'s {len(q['stops'])} route stop(s) stay on their routes",
            f"{len(q['stops'])} kept", f"{len(left)} kept", len(left) == len(q["stops"]))


@title("Matrix 3 · pull → edit one structure, delete another → push")
def test_live_matrix_03_edit_delete_push(mx):
    lv = mx.lv
    pull(lv)
    p, q = standalone_points(lv, mx.baseline, 2)
    edit(lv, p, f"QA-MX3-{STAMP}")
    delete(lv, q)
    mx.push("edit + delete → push", want=(0, 1), want_prompt=[q["lid"]])
    after = mx.server()
    mx.check("LOC after the push", mx.baseline, after,
             changed={p["lid"]: ["fields"]}, removed=[q["lid"]])
    kept_stops(mx, q, after)


@title("Matrix 4 · pull → edit, delete and add structures → push")
def test_live_matrix_04_edit_delete_add_push(mx):
    lv = mx.lv
    pull(lv)
    p, q = standalone_points(lv, mx.baseline, 2)
    new_uid = f"QA-MX4-NEW-{STAMP}"
    edit(lv, p, f"QA-MX4-{STAMP}")
    delete(lv, q)
    add_point(lv, p, new_uid, f"QA-MX4-ADD-{STAMP}")
    mx.push("edit + delete + add → push", want=(1, 1), want_prompt=[q["lid"]])
    after = mx.server()
    mx.check("LOC after the push", mx.baseline, after, changed={p["lid"]: ["fields"]},
             removed=[q["lid"]], added_uids=[new_uid])
    kept_stops(mx, q, after)
    rec = server_rec(after, new_uid)
    got = (rec.get("fields") or {}).get(p["lf"]) if rec else None
    mx.step("new structure's field in LOC", f"QA-MX4-ADD-{STAMP}", repr(got),
            got == f"QA-MX4-ADD-{STAMP}")
    mx.push("same project pushed again, nothing edited", want=(0, 0))
    mx.check("LOC after the second push", after, mx.server())


@title("Matrix 5 · cascaded edits across pushes and pulls in three projects")
def test_live_matrix_05_cascaded(mx):
    lv = mx.lv
    pull(lv)
    p, q, r = standalone_points(lv, mx.baseline, 3)
    edit(lv, p, f"QA-MX5-A1-{STAMP}")
    mx.push("project 1: edit A → push", want=(0, 1))
    edit(lv, p, f"QA-MX5-A2-{STAMP}")
    edit(lv, r, f"QA-MX5-C1-{STAMP}")
    mx.push("project 1: edit A again + edit C → push", want=(0, 2))
    s1 = mx.server()
    mx.check("LOC after project 1", mx.baseline, s1,
             changed={p["lid"]: ["fields"], r["lid"]: ["fields"]})

    new_project(lv, "mx5_second")
    pull(lv)
    la, fa = find_point(p["lid"])
    lc, fc = find_point(r["lid"])
    lm_a = next(m for m in lv.plugin.layer_mappings if m.layer_id == la.id())
    lm_c = next(m for m in lv.plugin.layer_mappings if m.layer_id == lc.id())
    va, vc = str(fa[lm_a.qgis_field_for(p["lf"])]), str(fc[lm_c.qgis_field_for(r["lf"])])
    mx.step("project 2: the pull shows both edits",
            f"A = QA-MX5-A2-{STAMP}, C = QA-MX5-C1-{STAMP}", f"A = {va}, C = {vc}",
            va == f"QA-MX5-A2-{STAMP}" and vc == f"QA-MX5-C1-{STAMP}")
    edit(lv, r, r["old"])
    delete(lv, q)
    new_uid = f"QA-MX5-NEW-{STAMP}"
    add_point(lv, p, new_uid, f"QA-MX5-ADD-{STAMP}")
    mx.push("project 2: revert C + delete B + add N → push", want=(1, 1),
            want_prompt=[q["lid"]])
    s2 = mx.server()
    mx.check("LOC after project 2", s1, s2, changed={r["lid"]: ["fields"]},
             removed=[q["lid"]], added_uids=[new_uid])

    new_project(lv, "mx5_third")
    pull(lv)
    mx.push("project 3: pull → unedited push", want=(0, 0))
    mx.check("LOC after everything, against the backup", mx.baseline, mx.server(),
             changed={p["lid"]: ["fields"]}, removed=[q["lid"]], added_uids=[new_uid])


@title("Matrix 6 · pull → delete a whole route → push")
def test_live_matrix_06_delete_route(mx):
    lv = mx.lv
    pull(lv)
    by_multi = {m["id"]: m for m in mx.baseline["multiLoc"]}
    shared = {}
    for m in mx.baseline["multiLoc"]:
        for lid in route_lids(mx.baseline, m["id"]):
            shared[lid] = shared.get(lid, 0) + 1
    route = next(r for r in lv.plugin.cached_routes
                 if _layer(r.line_layer_id) is not None
                 and str(_layer(r.line_layer_id).getFeature(r.line_feature_id)["_multi_id"])
                 in by_multi)
    layer = _layer(route.line_layer_id)
    mid = str(layer.getFeature(route.line_feature_id)["_multi_id"])
    m = by_multi[mid]
    cable = m["dualLoc"]["loc_id"]
    assert layer.dataProvider().deleteFeatures([route.line_feature_id])
    lv.plugin.cached_routes = [r for r in lv.plugin.cached_routes if r is not route]
    act(lv, f"Action: delete route {m['multiName']} (its cable line) from the map")
    others = set()
    for m2 in mx.baseline["multiLoc"]:
        if m2["id"] != mid:
            others |= route_lids(mx.baseline, m2["id"])
    gone = route_lids(mx.baseline, mid) - others
    mx.push(f"route {m['multiName']} deleted → push", want_prompt=[cable])
    after = mx.server()
    # The route goes with its cable and its stop records (a stop record
    # belongs to its route; the structures it stood on stay).
    mx.check("LOC after the push", mx.baseline, after, removed=gone, routes_removed=[mid])


@title("Matrix 7 · pull → delete a structure that is a route stop → Generate → push")
def test_live_matrix_07_delete_route_stop(mx):
    lv = mx.lv
    pull(lv)
    recs = records(mx.baseline)
    singles = [s_ for s_ in mx.baseline.get("singleLOCs", [])]
    at_xy = {}
    for s_ in singles:
        at_xy.setdefault(_xy(s_), []).append(s_["loc_id"])
    route = victim = None
    for r in sorted(lv.plugin.cached_routes, key=lambda r: len(r.stops)):
        for s_ in r.intermediate_stops:
            if s_.point_feature_id < 0:
                continue
            lid = str(_layer(s_.point_layer_id).getFeature(s_.point_feature_id)["_loc_id"])
            if lid in recs and len(at_xy.get(_xy(recs[lid][1]), [])) == 1:
                route, victim, vlid = r, s_, lid
                break
        if victim:
            break
    assert victim, "no route stop standing on a structure of its own"
    layer = _layer(route.line_layer_id)
    mid = str(layer.getFeature(route.line_feature_id)["_multi_id"])
    on_it = stops_on(mx.baseline, recs[vlid][1])
    pl = _layer(victim.point_layer_id)
    assert pl.dataProvider().deleteFeatures([victim.point_feature_id])
    act(lv, f"Action: delete structure {victim.structure_name} "
            f"(a stop on {len(set(on_it.values()))} route(s)) from the map")
    key = (route.line_layer_id, route.line_feature_id)
    generate(lv)
    close_review(lv)
    r2 = find_route(lv.plugin, *key)
    mx.step("Generate: the route drops the stop", f"{len(route.stops) - 1} stops",
            f"{len(r2.stops)} stops", len(r2.stops) == len(route.stops) - 1)
    mx.push(f"structure {victim.structure_name} deleted → push", want_prompt=[vlid])
    after = mx.server()
    mx.check("LOC after the push: the structure and its stop record on every route gone",
             mx.baseline, after, removed={vlid} | set(on_it),
             routes_changed=sorted(set(on_it.values())))
    got = routes(after)[mid][2]
    want = tuple(l for l in routes(mx.baseline)[mid][2] if l not in on_it)
    mx.step(f"{route.line_name} stop order in LOC", f"{len(want)} stops, the rest in order",
            f"{len(got)} stops", got == want)


@title("Matrix 8 · pull → add a structure on a route → Generate → push")
def test_live_matrix_08_add_route_stop(mx):
    lv = mx.lv
    pull(lv)
    route, a, b, sa, sb = pick_route(lv.plugin)
    layer = _layer(route.line_layer_id)
    mid = str(layer.getFeature(route.line_feature_id)["_multi_id"])
    pl = _layer(sa.point_layer_id)
    lm = next(m for m in lv.plugin.layer_mappings if m.layer_id == pl.id())
    uid = f"QA-MX8-STOP-{STAMP}"
    nf = QgsFeature(pl.fields())
    nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)))
    nf.setAttribute(lm.qgis_field_for("Unique Asset Identifier"), uid)
    if lm.qgis_field_for("Actual Asset Name"):
        nf.setAttribute(lm.qgis_field_for("Actual Asset Name"), f"QA {uid}")
    assert pl.dataProvider().addFeatures([nf])[0]
    act(lv, f"Action: add structure {uid} on {route.line_name} between "
            f"{sa.structure_name} and {sb.structure_name}")
    key = (route.line_layer_id, route.line_feature_id)
    generate(lv)
    close_review(lv)
    r2 = find_route(lv.plugin, *key)
    mx.step("Generate: the route gained the stop", f"{len(route.stops) + 1} stops",
            f"{len(r2.stops)} stops", len(r2.stops) == len(route.stops) + 1)
    mx.push(f"stop {uid} added → push")
    after = mx.server()
    d8 = diff(mx.baseline, after)
    # Every route that changed only gained new stop records at that point.
    grew = [m_ for m_, (o, n) in d8["r_changed"].items()
            if o[0] == n[0] and o[1] == n[1]
            and tuple(l for l in n[2] if l not in d8["added"]) == o[2]]
    mx.check("LOC after the push: the structure and its stop record(s) added",
             mx.baseline, after, added_uids=[uid], added_like=[uid],
             routes_changed=sorted(set(grew) | {mid}))
    names = sorted(routes(after)[m_][0] for m_ in grew)
    mx.step("routes through that point that gained the stop", "the route it was added on, "
            "plus any other route on the same line", ", ".join(names), mid in grew)
    got = routes(after)[mid][2]
    new = next((r for lid, (role, r) in records(after).items()
                if lid in got and uid in (r.get("unique_asset_id") or "")), None)
    mx.step(f"{route.line_name} holds the new stop between its neighbours",
            "new stop in the route", f"{len(got)} stops; new stop "
            f"{'in' if new and new['loc_id'] in got else 'NOT in'} the route",
            bool(new) and new["loc_id"] in got and len(got) == len(routes(mx.baseline)[mid][2]) + 1)


@title("Matrix 9 · pull → correct a Route ID → Generate → push (cascades to the route)")
def test_live_matrix_09_route_id(mx):
    lv = mx.lv
    pull(lv)
    rt = None
    for r in lv.plugin.cached_routes:
        lm = next((m for m in lv.plugin.layer_mappings if m.layer_id == r.line_layer_id), None)
        q = lm.qgis_field_for("Route ID") if lm else None
        layer = _layer(r.line_layer_id)
        if q and layer is not None:
            f = layer.getFeature(r.line_feature_id)
            if f["_multi_id"] and str(f[q]) not in ("", "NULL") and len(r.stops) >= 2:
                rt = (r, layer, q, f, str(f[q]))
                break
    assert rt, "no pulled route with a mapped Route ID"
    r, layer, q, f, rid = rt
    mid = str(f["_multi_id"])
    set_field(layer, f.id(), q, f"{rid}-MX")
    act(lv, f"Action: correct Route ID {rid} → {rid}-MX")
    generate(lv)
    close_review(lv)
    mx.push(f"Route ID {rid} → {rid}-MX → push")
    after = mx.server()
    mx.check("LOC after the push: only that route's records changed", mx.baseline, after,
             routes_changed=[mid], allow_changed=route_lids(mx.baseline, mid))
    name = routes(after)[mid][0]
    same_stops = routes(after)[mid][1:] == routes(mx.baseline)[mid][1:]
    mx.step("route renamed, same records", f"name {rid}-MX, same cable and stops",
            f"name {name!r}, records {'same' if same_stops else 'DIFFERENT'}",
            name == f"{rid}-MX" and same_stops)


@title("Matrix 10 · pull → move a structure → push")
def test_live_matrix_10_move(mx):
    lv = mx.lv
    pull(lv)
    (p,) = standalone_points(lv, mx.baseline, 1)
    layer, f = find_point(p["lid"])
    pt = f.geometry().asPoint()
    assert layer.dataProvider().changeGeometryValues(
        {f.id(): QgsGeometry.fromPointXY(QgsPointXY(pt.x() + 0.0004, pt.y()))})
    act(lv, f"Action: move {p['uid']} ~27 m east")
    # Records standing on the structure move with it (route stops, a cable
    # that starts there) — the cascade.
    old = _xy(records(mx.baseline)[p["lid"]][1])
    exp = {p["lid"]: ["origin_longitude"]}
    for lid, (role, r) in records(mx.baseline).items():
        if lid == p["lid"]:
            continue
        dest = r.get("LOCDestination") or {}
        keys = (["origin_longitude"] if _xy(r) == old else []) + (
            ["destination_longitude"] if dest and _xy({"origin_longitude": dest.get("longitude"),
                                                         "origin_latitude": dest.get("latitude")}) == old
            else [])
        if keys:
            exp[lid] = keys
    with_it = [l for l in exp if l != p["lid"]]
    mx.push(f"{p['uid']} moved → push", want=(0, len(exp)))
    after = mx.server()
    mx.check("LOC after the push", mx.baseline, after, changed=exp)
    d10 = diff(mx.baseline, after)
    mx.step("records ending or standing on the structure moved with it",
            f"{len(with_it)} moved: " + ", ".join(_uid(d10, l) for l in with_it),
            f"{sum(1 for l in with_it if l in d10['changed'])} moved",
            all(l in d10["changed"] for l in with_it))


def _capture_in_the_field(mx, payload, uid, near_lid):
    """Another user (the field app, or a second QGIS) adds a structure:
    the full location as it is plus one new record, pushed through the
    API.  Unknown to the project under test."""
    from label_on_a_cable.core.tasks import _sent_payload
    p = copy.deepcopy(_sent_payload(payload))
    tmpl = next(s for s in p["single_locs"] if s["attributes"].get("loc_id") == near_lid)
    import uuid
    new = copy.deepcopy(tmpl)
    at = new["attributes"]
    new["id"] = str(uuid.uuid4())
    at["loc_id"] = str(uuid.uuid4())
    at["origin_id"] = str(uuid.uuid4())
    for k in ("id", "createdAt", "updatedAt"):
        at.pop(k, None)
    for d in (new, at):
        if "unique_asset_id" in d:
            d["unique_asset_id"] = uid
        if "actual_asset_name" in d:
            d["actual_asset_name"] = f"QA {uid}"
    new["unique_asset_id"] = uid
    new["actual_asset_name"] = new["name"] = f"QA {uid}"
    lon = float(new["coordinates"][0]) + 0.0005
    new["coordinates"] = [lon, new["coordinates"][1]]
    at["origin_longitude"] = lon
    p["single_locs"].append(new)
    mx.api.push_locs(p)
    act(mx.lv, f"Elsewhere: {uid} is captured in the field and reaches LOC")


@title("Matrix 11 · LOC gains a field capture after the pull → edit → push")
def test_live_matrix_11_field_capture(mx):
    lv = mx.lv
    pull(lv)
    (p,) = standalone_points(lv, mx.baseline, 1)
    seen = preview_then_cancel(lv)
    uid1 = f"QA-MX11-FIELD1-{STAMP}"
    _capture_in_the_field(mx, seen["payload"], uid1, p["lid"])
    s0 = mx.server()
    rec1 = server_rec(s0, uid1)
    mx.step("field capture reached LOC", uid1, "present" if rec1 else "missing", bool(rec1))
    edit(lv, p, f"QA-MX11-{STAMP}")

    if mx.capture:
        # Cancel: nothing is pushed.
        from qgis.PyQt.QtWidgets import QMessageBox
        lv.msgbox.answers = [QMessageBox.StandardButton.Cancel]
        n = len(lv.msgbox.calls)
        lv.plugin.actions["push"].trigger()
        wait_until(lambda: lv.plugin._push_state_task is None, timeout_ms=300000)
        pump(20)
        stale = [c for c in lv.msgbox.calls[n:] if c[1] == "Map Is Behind LOC"]
        mx.step("stale-map warning, Cancel", f"warning lists {uid1}; nothing pushed",
                "shown" if stale else "not shown",
                bool(stale) and uid1 in stale[0][2]
                and not diff(s0, mx.server())["changed"], [stale[0][2][:300]] if stale else [])
        # Push anyway: the edit lands, the field capture stays.
        mx.push("stale-map warning, Push anyway", want=(0, 1),
                answers=[QMessageBox.StandardButton.Ignore], want_stale=True)
        s1 = mx.server()
        mx.check("LOC after Push anyway", s0, s1, changed={p["lid"]: ["fields"]})
        # A second capture, then Pull now: both captures are added to the
        # layers mapped to their category (no second set of layers, the
        # mapping unchanged), nothing is pushed, and the next push does not
        # warn.  (The dry run below warns: Push anyway.)
        lv.msgbox.answers = [QMessageBox.StandardButton.Ignore]
        seen = preview_then_cancel(lv)
        uid2 = f"QA-MX11-FIELD2-{STAMP}"
        _capture_in_the_field(mx, seen["payload"], uid2, p["lid"])
        s2 = mx.server()

        layers_before = sorted(QgsProject.instance().mapLayers())
        mapping_before = [m.to_dict() for m in lv.plugin.layer_mappings]
        lv.msgbox.answers = [QMessageBox.StandardButton.Yes]       # Pull now
        n = len(lv.msgbox.calls)
        act(lv, "Action: click Push to LOC → answer Pull now")
        lv.plugin.actions["push"].trigger()
        wait_until(lambda: lv.plugin._push_state_task is None, timeout_ms=300000)
        pump(50)
        stale = [c for c in lv.msgbox.calls[n:] if c[1] == "Map Is Behind LOC"]
        lid2 = server_rec(s2, uid2)["loc_id"]
        on_map = any(str(f["_loc_id"]) == lid2
                     for l in QgsProject.instance().mapLayers().values()
                     if isinstance(l, QgsVectorLayer) and l.fields().indexOf("_loc_id") >= 0
                     for f in l.getFeatures())
        same = (sorted(QgsProject.instance().mapLayers()) == layers_before
                and [m.to_dict() for m in lv.plugin.layer_mappings] == mapping_before)
        added = [c for c in lv.msgbox.calls[n:] if c[1] == "Field Captures Added"]
        mx.step("stale-map warning, Pull now", f"warning lists {uid2}; it is added to "
                "the mapped layer; layers and mapping unchanged; nothing pushed",
                f"warning {'shown' if stale else 'not shown'}; "
                f"{uid2} {'on' if on_map else 'NOT on'} the map; layers/mapping "
                f"{'unchanged' if same else 'CHANGED'}",
                bool(stale) and uid2 in stale[0][2] and on_map and same and bool(added)
                and not diff(s2, mx.server())["changed"],
                [added[0][2][:300]] if added else [])
        s3 = s2
        uid3 = uid2
        mx.push("after Pull now: push again", want=(0, 0))
        mx.check("LOC after the last push: all field captures kept", s3, mx.server())
        last = mx.server()
        mx.step("field captures kept", f"{uid1}, {uid2} in LOC",
                ", ".join(u for u in (uid1, uid2) if server_rec(last, u)) or "none",
                all(server_rec(last, u) for u in (uid1, uid2)))
    else:
        # A normal project: the push replaces the location, so the record
        # this map does not hold is listed for deletion.  No: nothing sent.
        from qgis.PyQt.QtWidgets import QMessageBox
        lv.msgbox.answers = [QMessageBox.StandardButton.No]
        n = len(lv.msgbox.calls)
        lv.plugin.actions["push"].trigger()
        wait_until(lambda: lv.plugin._push_state_task is None, timeout_ms=300000)
        pump(20)
        prompt = [c for c in lv.msgbox.calls[n:] if c[1] == "LOCs Will Be Deleted"]
        stale = [c for c in lv.msgbox.calls[n:] if c[1] == "Map Is Behind LOC"]
        mx.step("deletion prompt lists the field capture, No",
                f"prompt lists {uid1}; no stale-map warning; nothing pushed",
                f"prompt {'lists it' if prompt and uid1 in prompt[0][2] else 'MISSING'}; "
                f"stale warning {'SHOWN' if stale else 'not shown'}",
                bool(prompt) and uid1 in prompt[0][2] and not stale
                and not diff(s0, mx.server())["changed"], [prompt[0][2][:300]] if prompt else [])
        mx.push("deletion prompt, Yes → push", want=(0, 1),
                answers=[QMessageBox.StandardButton.Yes], want_prompt=[rec1["loc_id"]])
        mx.check("LOC after the push: edit landed, field capture replaced", s0, mx.server(),
                 changed={p["lid"]: ["fields"]}, removed=[rec1["loc_id"]])
