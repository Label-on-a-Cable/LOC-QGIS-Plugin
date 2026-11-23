"""The rows of ``MANUAL_PROD_TEST_PLAN.md`` Part B that no other live suite
automates, driven the way the plan's tester does them:

* edits go through the layer's edit buffer (start editing, change, save),
* every prompt is answered by clicking the button with the planned label
  ("Keep LOC's values", "Push", "Yes" ...); a prompt the plan does not
  answer gets its own default button, as a user pressing Enter would,
* the prompt's text, buttons and default are checked, then the two
  "LOC Push" log lines (``Merge with LOC`` / ``Project capture mode``),
  then LOC record by record (the ``mx`` fixture backs the location up
  first and restores it afterwards).

"In LOC" changes are made the way the plan's Desk B makes them: the full
location as LOC holds it, with one change, pushed through the API.

    LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh \\
        LOC_LIVE_LOCATION=Howth LOC_LIVE_CAPTURE=on|off \\
        tests/run_live_videos.sh --allow-push -k live_merge

Rows covered here: 3h, 11, 13, 14, 15, 16, 17, 18, 20, 21, 22, 23, 26c,
27, 28.  Results go to ``matrix/merge.jsonl``.
"""

import copy
import re
import uuid

import pytest
from qgis.core import (
    QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer,
)
from qgis.PyQt.QtWidgets import QMessageBox

from helpers import pump
from test_live_matrix import (  # noqa: F401  (fixtures)
    STAMP, _xy, counts, diff, find_point, lv, mx, records,
    routes, server_rec, standalone_points, target,
)
from test_live_videos import (
    _layer, act, close_review, generate, new_project, pick_route, preview_then_cancel,
    push_for_real, title,
)

pytestmark = [pytest.mark.live, pytest.mark.live_write]
RESULTS_FILE = "merge.jsonl"

SB = QMessageBox.StandardButton
_ESSENTIAL = {"Unique Asset Identifier", "Actual Asset Name", "Route ID"}


# ----------------------------------------------------------------------
# The user at the screen: prompts clicked by label, the LOC Push log read
# ----------------------------------------------------------------------

class User:
    """Answers the plugin's hand-built prompts by button label.

    ``plan`` maps a prompt title to the label(s) to click, in turn when
    the same prompt comes back.  Every prompt is kept in ``boxes`` with
    its text, button labels and default button."""

    def __init__(self, lv):
        self.lv, self.plan, self.boxes, self.log = lv, {}, [], []
        # Installed on the recorder instance and removed again before the
        # backup restore, which relies on the recorder's own answers.
        lv.msgbox.exec_box = self._exec
        from qgis.core import QgsMessageLog
        real = QgsMessageLog.logMessage
        user = self

        def log_message(message, tag="", *a, **k):
            if tag == "LOC Push":
                user.log.append(message)
            return real(message, tag, *a, **k)
        QgsMessageLog.logMessage = staticmethod(log_message)

        def undo():
            QgsMessageLog.logMessage = real
            lv.msgbox.__dict__.pop("exec_box", None)
        self._disconnect = undo

    @staticmethod
    def _label(btn):
        return btn.text().replace("&", "")

    def _exec(self, box):
        rec = self.lv.msgbox
        kind = rec._ICON_KINDS.get(box.icon(), "information")
        rec.calls.append((kind, box.windowTitle(), box.text()))
        if rec.on_call is not None:
            rec.on_call(kind, box.windowTitle(), box.text())
        buttons = {self._label(b): box.standardButton(b) for b in box.buttons()}
        d = box.defaultButton()
        default = self._label(d) if d is not None else None
        entry = dict(title=box.windowTitle(), text=box.text(), buttons=list(buttons),
                     default=default, clicked=None)
        self.boxes.append(entry)
        want = self.plan.get(box.windowTitle())
        if isinstance(want, list):
            want = want.pop(0) if want else None
        if want is None:
            want = default or next(iter(buttons), None)
        labels = (want,) if isinstance(want, str) else tuple(want)
        hit = next((lbl for lbl in labels if lbl in buttons), None)
        if hit is None:
            raise AssertionError(f"{box.windowTitle()!r}: no button {labels} "
                                 f"(it has {list(buttons)})")
        entry["clicked"] = hit
        act(self.lv, f"Action: “{box.windowTitle()}” → click {hit}")
        return buttons[hit]

    def merge_line(self):
        """(kept, stops kept, stops removed, conflicts, map changes, no base)
        from the last ``Merge with LOC`` line."""
        for line in reversed(self.log):
            m = re.search(r"Merge with LOC: (\d+) .*?, (\d+) stop.*?, (\d+) removed in LOC, "
                          r"(\d+) conflict.*?, (\d+) change.*?, (\d+) record", line)
            if m:
                return tuple(int(x) for x in m.groups())
        return None

    def capture_line(self):
        return next((ln for ln in reversed(self.log) if ln.startswith("Project capture mode")),
                    None)

    def since(self, n):
        return self.boxes[n:]


@pytest.fixture
def user(mx):
    u = User(mx.lv)
    yield u
    u._disconnect()


def push(mx, user, what, plan=None, titles=None, want=None, push_sent=True):
    """Click Push to LOC, answer the prompts by *plan*, and click Push in
    Push Preview.  *titles*: the prompts that must show, in order."""
    user.plan = {k: (list(v) if isinstance(v, list) else v) for k, v in (plan or {}).items()}
    n, nlog = len(user.boxes), len(user.log)
    seen = push_for_real(mx.lv)
    boxes = user.since(n)
    got_titles = [b["title"] for b in boxes]
    c, u, d = counts(seen.get("stats"))
    sent = bool(seen.get("pushed"))
    notes = [f"prompts: {' → '.join(got_titles) or 'none'}"]
    notes += [f"  {b['title']}: buttons {b['buttons']}, default {b['default']}, "
              f"clicked {b['clicked']} · " + b["text"].replace("\n", " ")[:300] for b in boxes]
    notes += [f"log: {ln}" for ln in user.log[nlog:]
              if ln.startswith(("Merge with LOC", "Project capture mode"))]
    if sent:
        notes.append(f"server dry run: created {c}, updated {u}, deleted {d}")
    ok = push_sent is None or sent == push_sent
    if titles is not None and got_titles != list(titles):
        ok = False
        notes.append(f"EXPECTED prompts: {' → '.join(titles) or 'none'}")
    if want is not None and sent and (c, u) != want:
        ok = False
        notes.append(f"EXPECTED created {want[0]}, updated {want[1]}")
    for bad in ("Push Validation Failed", "Some LOCs Were Not Deleted", "Push Failed",
                "Push — Server Warning"):
        hit = [t for _k, tt, t in seen.get("dialogs", []) if tt == bad]
        if hit:
            ok = False
            notes.append(f"{bad}: {hit[0][:300]}")
    mx.step(what, ("push sent" if push_sent else "nothing sent")
            + (f", prompts {' → '.join(titles) or 'none'}" if titles is not None else ""),
            ("sent" if sent else "not sent") + f"; prompts {' → '.join(got_titles) or 'none'}",
            ok, notes)
    return {b["title"]: b for b in boxes}


# ----------------------------------------------------------------------
# Editing the map like a user (edit buffer, then Save Edits)
# ----------------------------------------------------------------------

def user_set(lv, layer, fid, field, value, label=None):
    assert layer.startEditing() or layer.isEditable()
    assert layer.changeAttributeValue(fid, layer.fields().indexOf(field), value)
    assert layer.commitChanges(), layer.commitErrors()
    act(lv, f"Action: attribute table → {label or field} = {value!r} → Save Edits")


def user_delete(lv, layer, fid, label):
    assert layer.startEditing() or layer.isEditable()
    assert layer.deleteFeature(fid)
    assert layer.commitChanges(), layer.commitErrors()
    act(lv, f"Action: select {label} → Delete Selected → Save Edits")


def mapping_of(lv, layer):
    return next(m for m in lv.plugin.layer_mappings if m.layer_id == layer.id())


def set_value(lv, p, loc_field, value):
    layer, f = find_point(p["lid"])
    user_set(lv, layer, f.id(), mapping_of(lv, layer).qgis_field_for(loc_field), value,
             f"{p['uid']}.{loc_field}")


def other_field(lv, p):
    """A second mapped category field on *p*'s layer (not p['lf'])."""
    layer, _f = find_point(p["lid"])
    lm = mapping_of(lv, layer)
    return next((fm.loc_field for fm in lm.field_mappings
                 if fm.qgis_field and fm.loc_field not in _ESSENTIAL
                 and fm.loc_field != p["lf"]), None)


# ----------------------------------------------------------------------
# "In LOC": Desk B changes the location after the pull
# ----------------------------------------------------------------------

def loc_payload(mx):
    """The location as a push payload (the map under test, unedited)."""
    from label_on_a_cable.core.tasks import _sent_payload
    mx.lv.msgbox.answers = []
    return copy.deepcopy(_sent_payload(preview_then_cancel(mx.lv)["payload"]))


def _single(pl, lid):
    for s in pl["single_locs"]:
        if s.get("attributes", {}).get("loc_id") == lid or s.get("loc_id") == lid:
            return s
    for m in pl["multiLocs"]:
        for st in m.get("Stops", []):
            if (st.get("singleLoc") or {}).get("loc_id") == lid:
                return st["singleLoc"]
    raise AssertionError(f"{lid} not in the payload")


def _set_rec(rec, key, value, sub=None):
    for d in (rec, rec.get("attributes")):
        if not isinstance(d, dict):
            continue
        if sub is None:
            d[key] = value
        elif isinstance(d.get(sub), dict) or d is rec:
            d.setdefault(sub, {})[key] = value


def in_loc(mx, pl, what):
    mx.api.push_locs(pl)
    act(mx.lv, f"In LOC (after the pull): {what}")
    return mx.server()


def change_in_loc(mx, pl, p, loc_field, value):
    _set_rec(_single(pl, p["lid"]), loc_field, value, sub="fields")
    s = in_loc(mx, pl, f"{p['uid']}.{loc_field} set to {value!r}")
    got = (records(s)[p["lid"]][1].get("fields") or {}).get(loc_field)
    assert got == value, f"setup: LOC did not take {p['uid']}.{loc_field} = {value!r} ({got!r})"
    return s


def field_of(data, lid, loc_field):
    rec = records(data).get(lid)
    return (rec[1].get("fields") or {}).get(loc_field) if rec else None


def value_step(mx, what, want, got):
    return mx.step(what, repr(want), repr(got), got == want)


def pick_one(mx):
    (p,) = standalone_points(mx.lv, mx.baseline, 1)
    return p


# ----------------------------------------------------------------------
# B1 rows not in the matrix
# ----------------------------------------------------------------------

@title("Plan 11 · correct a standalone structure's asset ID → push")
def test_live_merge_11_asset_id(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    if p["stops"]:
        pytest.skip("no structure without route stops on this location")
    new = f"{p['uid']}-QA{STAMP}"
    layer, f = find_point(p["lid"])
    user_set(lv, layer, f.id(), p["uid_q"], new, f"{p['uid']} asset ID")
    push(mx, user, f"asset ID {p['uid']} → {new}", titles=[], want=(0, 1))
    after = mx.server()
    mx.check("LOC: the same record holds the new asset ID", mx.baseline, after,
             changed={p["lid"]: ["unique_asset_id"]})
    value_step(mx, "asset ID in LOC", new,
               (records(after).get(p["lid"], (None, {}))[1] or {}).get("unique_asset_id"))


def _history(api, location_id):
    """The location's history: loc-next serves it at ``logs?location_id=``
    (newest first); the legacy server at ``locations/{id}/logs``."""
    from label_on_a_cable.services.exceptions import LOCAPIException
    try:
        return api._get("logs", params={"location_id": location_id, "limit": 200})
    except LOCAPIException:
        return api.get_location_logs(location_id)


@title("Plan 3h · delete a structure → push → the location's history names it")
def test_live_merge_3h_history(mx, user):
    import json
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    if p["stops"]:
        pytest.skip("no structure without route stops on this location")
    lid = mx.lv.plugin.active_location.location_id
    before = json.dumps(_history(mx.api, lid))
    layer, f = find_point(p["lid"])
    user_delete(lv, layer, f.id(), p["uid"])
    boxes = push(mx, user, f"{p['uid']} deleted → push",
                 plan={"LOCs Will Be Deleted": "Yes"}, titles=["LOCs Will Be Deleted"])
    box = boxes.get("LOCs Will Be Deleted", {})
    mx.step("deletion prompt", f"lists {p['uid']}; default No",
            f"default {box.get('default')}",
            p["uid"] in box.get("text", "") and box.get("default") == "No")
    mx.check("LOC after the push", mx.baseline, mx.server(), removed=[p["lid"]])
    logs = json.dumps(_history(mx.api, lid))
    per_loc = f"Moved LOC {p['uid']} to the recycle bin from the QGIS plugin"
    # The same asset is deleted and restored on every run: count new rows.
    variant = ("one row per LOC (loc-next >= 1.31.1)"
               if logs.count(per_loc) > before.count(per_loc)
               else "one 'DELETE LOCs' row (older server)"
               if logs.count("DELETE LOCs") > before.count("DELETE LOCs") else None)
    mx.step("location history", "a row for the delete", variant or "NO row for the delete",
            bool(variant), [f"history grew {len(before)} → {len(logs)} characters"])


# ----------------------------------------------------------------------
# B2 — LOC changed after the pull
# ----------------------------------------------------------------------

@title("Plan 13 · LOC changes field X on S1, QGIS edits field Y on S1 → push")
def test_live_merge_13_same_record_other_field(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    y = other_field(lv, p)
    if y is None:
        pytest.skip(f"{p['uid']}'s layer has one mapped field only")
    theirs, mine = f"QA-P13-LOC-{STAMP}", f"QA-P13-QGIS-{STAMP}"
    s0 = change_in_loc(mx, loc_payload(mx), p, p["lf"], theirs)
    set_value(lv, p, y, mine)
    boxes = push(mx, user, f"{p['uid']}.{y} edited on the older map → push",
                 plan={"Map Is Behind LOC": "Push"}, titles=["Map Is Behind LOC"])
    box = boxes.get("Map Is Behind LOC", {})
    mx.step("Map Is Behind LOC", f"lists {p['uid']}; buttons Push / Cancel",
            f"buttons {box.get('buttons')}",
            p["uid"] in box.get("text", "") and sorted(box.get("buttons", [])) == ["Cancel", "Push"])
    ml = user.merge_line()
    mx.step("log: Merge with LOC", ">= 1 record keeps LOC's changes, 1 change on the map",
            str(ml), bool(ml) and ml[0] >= 1 and ml[4] == 1 and ml[3] == 0)
    after = mx.server()
    value_step(mx, f"{p['uid']}.{p['lf']} (LOC's)", theirs, field_of(after, p["lid"], p["lf"]))
    value_step(mx, f"{p['uid']}.{y} (mine)", mine, field_of(after, p["lid"], y))
    mx.check("nothing else changed", s0, after, changed={p["lid"]: ["fields"]})
    # The map's pulled copy is still the older one, so the warning shows again.
    push(mx, user, "push again, nothing edited", plan={"Map Is Behind LOC": "Push"},
         want=(0, 0))
    again = mx.server()
    mx.step("second push keeps both", f"{theirs} / {mine}",
            f"{field_of(again, p['lid'], p['lf'])} / {field_of(again, p['lid'], y)}",
            field_of(again, p["lid"], p["lf"]) == theirs and field_of(again, p["lid"], y) == mine)


@pytest.mark.parametrize("choice", ["mine", "theirs", "cancel"])
@title("Plan 14/15/16 · the same field changed in LOC and in QGIS → push")
def test_live_merge_14_conflict(mx, user, choice):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    theirs, mine = f"LOC-{STAMP}", f"QGIS-{STAMP}"
    s0 = change_in_loc(mx, loc_payload(mx), p, p["lf"], theirs)
    set_value(lv, p, p["lf"], mine)
    click = {"mine": "Keep my QGIS values", "theirs": "Keep LOC's values",
             "cancel": "Cancel"}[choice]
    boxes = push(mx, user, f"{p['uid']}.{p['lf']} changed on both sides → {click}",
                 plan={"Changed in QGIS and in LOC": click, "Map Is Behind LOC": "Push"},
                 push_sent=choice != "cancel")
    box = boxes.get("Changed in QGIS and in LOC", {})
    text = box.get("text", "")
    mx.step("Changed in QGIS and in LOC", f"{p['uid']} · {p['lf']}: QGIS {mine!r}, LOC {theirs!r}; "
            "buttons Keep my QGIS values / Keep LOC's values / Cancel; default Cancel",
            f"buttons {box.get('buttons')}, default {box.get('default')}",
            f"QGIS {mine!r}, LOC {theirs!r}" in text and p["uid"] in text
            and sorted(box.get("buttons", [])) == sorted(
                ["Keep my QGIS values", "Keep LOC's values", "Cancel"])
            and box.get("default") == "Cancel", [text[:300]])
    if choice == "cancel":
        later = [t for t in boxes if t != "Changed in QGIS and in LOC"]
        mx.step("Cancel stops the push", "no further prompts", ", ".join(later) or "none",
                not later)
        mx.check("LOC unchanged", s0, mx.server())
        return
    after = mx.server()
    value_step(mx, f"{p['uid']}.{p['lf']} in LOC", mine if choice == "mine" else theirs,
               field_of(after, p["lid"], p["lf"]))
    mx.check("nothing else changed", s0, after,
             changed={p["lid"]: ["fields"]} if choice == "mine" else None)


@title("Plan 17/18 · LOC changes S1, QGIS changes nothing → Push → push again")
def test_live_merge_17_unedited_push(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    theirs = f"QA-P17-LOC-{STAMP}"
    s0 = change_in_loc(mx, loc_payload(mx), p, p["lf"], theirs)
    boxes = push(mx, user, "unedited older map → push",
                 plan={"Map Is Behind LOC": "Push"}, titles=["Map Is Behind LOC"])
    box = boxes.get("Map Is Behind LOC", {})
    mx.step("17: Map Is Behind LOC offers Push / Cancel only (no Pull now), default Push",
            "Push, Cancel; default Push", f"{box.get('buttons')}; default {box.get('default')}",
            sorted(box.get("buttons", [])) == ["Cancel", "Push"] and box.get("default") == "Push"
            and p["uid"] in box.get("text", ""))
    value_step(mx, f"{p['uid']}.{p['lf']} keeps LOC's value", theirs,
               field_of(mx.server(), p["lid"], p["lf"]))
    push(mx, user, "18: push again from the same map",
         plan={"Map Is Behind LOC": "Push"})
    after = mx.server()
    value_step(mx, "18: still LOC's value", theirs, field_of(after, p["lid"], p["lf"]))
    mx.check("nothing else changed", s0, after)


def _add_stop_in_loc(mx, pl):
    m = next(m for m in pl["multiLocs"] if len(m.get("Stops", [])) >= 2)
    a, b = m["Stops"][0], m["Stops"][1]
    st = copy.deepcopy(a)
    sl = st["singleLoc"]
    uid = f"{m.get('multiName')}_QA-P20-STOP-{STAMP}"
    st["stop_id"] = str(uuid.uuid4())
    sl["loc_id"], sl["origin_id"] = str(uuid.uuid4()), str(uuid.uuid4())
    for k in ("id", "createdAt", "updatedAt"):
        sl.pop(k, None)
    sl["unique_asset_id"], sl["actual_asset_name"] = uid, "QA plan stop"
    for k in ("origin_longitude", "origin_latitude"):
        sl[k] = (float(a["singleLoc"][k]) + float(b["singleLoc"][k])) / 2
    m["Stops"].insert(1, st)
    for i, s_ in enumerate(m["Stops"]):
        s_["stopNumber"] = i + 1
    return m["id"], m.get("multiName"), uid


@title("Plan 20 · a stop added in LOC + my edit elsewhere → Generate → push")
def test_live_merge_20_stop_added_and_edit(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    pl = loc_payload(mx)
    mid, name, uid = _add_stop_in_loc(mx, pl)
    s0 = in_loc(mx, pl, f"a stop is added to {name}")
    new = server_rec(s0, uid)
    assert new and new["loc_id"] in routes(s0)[mid][2], "setup: the stop did not reach the route"
    mine = f"QA-P20-{STAMP}"
    set_value(lv, p, p["lf"], mine)
    generate(lv)
    close_review(lv)
    boxes = push(mx, user, "older map with an edit → Generate → push",
                 plan={"Map Is Behind LOC": "Push"}, titles=["Map Is Behind LOC"])
    box = boxes.get("Map Is Behind LOC", {})
    mx.step("Map Is Behind LOC lists the stop; Push / Cancel only",
            f"“1 stop(s) were added”, {name}: …", f"buttons {box.get('buttons')}",
            "1 stop(s) were added to routes in LOC" in box.get("text", "")
            and sorted(box.get("buttons", [])) == ["Cancel", "Push"])
    after = mx.server()
    mx.step("the stop stays on the route in LOC's order", "same order",
            "same" if routes(after)[mid][2] == routes(s0)[mid][2] else "DIFFERENT",
            routes(after)[mid][2] == routes(s0)[mid][2])
    value_step(mx, "my edit landed", mine, field_of(after, p["lid"], p["lf"]))


@title("Plan 21 · a stop removed from a route in LOC → push from the older map")
def test_live_merge_21_stop_removed(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    pl = loc_payload(mx)
    m = next((m for m in pl["multiLocs"] if len(m.get("Stops", [])) >= 3), None)
    if m is None:
        pytest.skip("no route with 3 stops")
    mid, name = m["id"], m.get("multiName")
    gone = m["Stops"].pop(1)
    for i, s_ in enumerate(m["Stops"]):
        s_["stopNumber"] = i + 1
    glid = gone["singleLoc"]["loc_id"]
    s0 = in_loc(mx, pl, f"stop {gone['singleLoc'].get('unique_asset_id')} removed from {name}")
    if glid in records(s0):
        # A capture-mode push deletes nothing: Desk B's plugin bins the
        # removed stop's record itself after the push, so do the same.
        mx.api.delete_locs([glid], location_id=lv.plugin.active_location.location_id)
        s0 = mx.server()
    assert glid not in routes(s0)[mid][2], "setup: the stop is still on the route"
    boxes = push(mx, user, "unedited older map → push",
                 plan={"Map Is Behind LOC": "Push"}, titles=["Map Is Behind LOC"])
    box = boxes.get("Map Is Behind LOC", {})
    mx.step("Map Is Behind LOC", "“1 stop(s) were removed from routes in LOC”",
            box.get("text", "").split("\n")[0][:200],
            "1 stop(s) were removed from routes in LOC" in box.get("text", ""))
    after = mx.server()
    mx.step("the stop is not put back", f"{name} without it",
            "not put back" if glid not in routes(after)[mid][2] else "PUT BACK",
            glid not in routes(after)[mid][2] and routes(after)[mid][2] == routes(s0)[mid][2])


@title("Plan 22 · a route renamed in LOC → push from the older map")
def test_live_merge_22_route_renamed(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    pl = loc_payload(mx)
    m = next(m for m in pl["multiLocs"] if m.get("multiName"))
    mid, new = m["id"], f"{m['multiName']}-QA{STAMP}"
    m["multiName"] = new
    s0 = in_loc(mx, pl, f"route {mid[:8]} renamed {new}")
    assert routes(s0)[mid][0] == new, "setup: LOC did not take the new route name"
    boxes = push(mx, user, "unedited older map → push",
                 plan={"Map Is Behind LOC": "Push"}, titles=["Map Is Behind LOC"])
    text = boxes.get("Map Is Behind LOC", {}).get("text", "")
    mx.step("Map Is Behind LOC", f"lists “{new}  (Route name)”", "listed"
            if f"{new}  (Route name)" in text else "NOT listed", f"{new}  (Route name)" in text,
            [text[:300]])
    value_step(mx, "route keeps LOC's name", new, routes(mx.server())[mid][0])


@pytest.mark.parametrize("what", ["notes", "is_flagged"])
@title("Plan 23 · a note or flag added on site + my edit on that structure → push")
def test_live_merge_23_note_or_flag(mx, user, what):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    value = f"QA note {STAMP}" if what == "notes" else True
    label = {"notes": "Notes", "is_flagged": "Flag"}[what]
    pl = loc_payload(mx)
    old = records(mx.baseline)[p["lid"]][1].get(what)
    _set_rec(_single(pl, p["lid"]), what, value)
    s0 = in_loc(mx, pl, f"{label.lower()} added to {p['uid']}")
    if records(s0)[p["lid"]][1].get(what) != value:
        pytest.skip(f"the API does not take a {label.lower()} from a push; this row needs "
                    "the LOC app or dashboard (not automated)")
    try:
        mine = f"QA-P23-{STAMP}"
        set_value(lv, p, p["lf"], mine)
        boxes = push(mx, user, f"edit {p['uid']} → push",
                     plan={"Map Is Behind LOC": "Push"}, titles=["Map Is Behind LOC"])
        text = boxes.get("Map Is Behind LOC", {}).get("text", "")
        mx.step("Map Is Behind LOC", f"lists {p['uid']} ({label}) under Changed in LOC",
                "listed" if p["uid"] in text and label in text else "NOT listed",
                p["uid"] in text and label in text, [text[:300]])
        after = mx.server()
        value_step(mx, f"{label} still there", value, records(after)[p["lid"]][1].get(what))
        value_step(mx, "my edit landed", mine, field_of(after, p["lid"], p["lf"]))
    finally:
        # The backup restore does not carry notes or flags: undo by hand.
        rec = records(mx.server()).get(p["lid"])
        if rec is not None and rec[1].get(what) != old:
            pl = _restore_payload(mx)
            _set_rec(_single(pl, p["lid"]), what, old)
            mx.api.push_locs(pl)
            act(lv, f"Cleanup: {label.lower()} on {p['uid']} set back to {old!r}")


def _restore_payload(mx):
    """The location as LOC holds it now, as a payload, from a fresh pull."""
    from test_live_videos import pull
    new_project(mx.lv, f"cleanup_{STAMP}")
    pull(mx.lv)
    return loc_payload(mx)


# ----------------------------------------------------------------------
# B3 — a capture on a cable, captured without a name
# ----------------------------------------------------------------------

@title("Plan 26c · a capture on a cable without a name → Pull now → Generate → push")
def test_live_merge_26c_capture_on_cable(mx, user):
    if not mx.capture:
        pytest.skip("Pull now is offered in capture mode")
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    route, a, b, sa, sb_ = pick_route(lv.plugin)
    rid = route.line_name
    near = str(_layer(sa.point_layer_id).getFeature(sa.point_feature_id)["_loc_id"])
    uid = f"QA26C{STAMP}"
    xy = ((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
    _capture_on(mx, loc_payload(mx), uid, near, xy)
    s0 = mx.server()
    assert server_rec(s0, uid), "setup: the capture did not reach LOC"
    act(lv, f"In LOC: {uid} captured on {rid} between {sa.structure_name} and "
            f"{sb_.structure_name}, without a name")
    boxes = push(mx, user, "Push to LOC → Pull now", plan={"Map Is Behind LOC": "Pull now"},
                 push_sent=False)
    added = [t for _k, tt, t in lv.msgbox.calls if tt == "Field Captures Added"]
    mx.step("Pull now adds the capture", f"Field Captures Added lists {uid}",
            "listed" if added and uid in added[-1] else "NOT listed",
            "Map Is Behind LOC" in boxes and bool(added) and uid in added[-1])
    pump(20)
    generate(lv)
    close_review(lv)
    push(mx, user, "Generate → push", titles=[])
    after = mx.server()
    on_routes = {l for v in routes(after).values() for l in v[2]}
    names = [(r.get("unique_asset_id") or "", r.get("actual_asset_name") or "")
             for lid, (_role, r) in records(after).items() if lid in on_routes]
    stop = [n for n in names if f"_{uid}" in n[0] or f"_{uid}" in n[1]]
    none_named = [n for n in names if "_None" in n[0] or "_None" in n[1]]
    mx.step("the cable gains a stop named <Route ID>_<asset ID>", f"{rid}_{uid}; never _None",
            f"{stop[:2] or 'no such stop'}; _None: {none_named[:2] or 'none'}",
            bool(stop) and not none_named)


def _capture_on(mx, payload, uid, near_lid, xy):
    """A field capture at *xy* with no name: the location as it is plus
    one new record, pushed through the API."""
    p = copy.deepcopy(payload)
    tmpl = next(s for s in p["single_locs"] if s["attributes"].get("loc_id") == near_lid)
    new = copy.deepcopy(tmpl)
    at = new["attributes"]
    new["id"] = str(uuid.uuid4())
    at["loc_id"], at["origin_id"] = str(uuid.uuid4()), str(uuid.uuid4())
    for k in ("id", "createdAt", "updatedAt"):
        at.pop(k, None)
    for d in (new, at):
        d["unique_asset_id"] = uid
        d["actual_asset_name"] = ""
    new["name"] = ""
    new["coordinates"] = [xy[0], xy[1]]
    at["origin_longitude"], at["origin_latitude"] = xy
    p["single_locs"].append(new)
    mx.api.push_locs(p)


# ----------------------------------------------------------------------
# B4 — known limits
# ----------------------------------------------------------------------

@title("Plan 27 · limit: a drawn project (not pulled) pushes over LOC's change")
def test_live_merge_27_drawn_project(mx, user):
    from label_on_a_cable.models.mapping import LayerMapping
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    drawn_value = p["old"]
    maps = [LayerMapping.from_dict(m.to_dict()) for m in lv.plugin.layer_mappings]
    copies = []
    for lm in maps:
        src = QgsProject.instance().mapLayer(lm.layer_id)
        if isinstance(src, QgsVectorLayer):
            copies.append((lm, _drawn_copy(src)))
    theirs = f"QA-P27-LOC-{STAMP}"
    s0 = change_in_loc(mx, loc_payload(mx), p, p["lf"], theirs)
    new_project(lv, f"drawn_{STAMP}")
    lv.plugin._pulled_loc_ids = set()
    lv.plugin._pulled_multi_stop_counts = {}
    mine = []
    for lm, mem in copies:
        QgsProject.instance().addMapLayer(mem)
        lm.layer_id, lm.layer_name = mem.id(), mem.name()
        mine.append(lm)
    lv.plugin._on_mapping_accepted(mine, lv.plugin.snap_tolerance)
    act(lv, "Action: draw the location's assets in a new project (same asset IDs) "
            "and map the layers in Category Mapping")
    generate(lv)
    close_review(lv)
    # The limit: with no pulled copy, LOC's change is not seen, so the
    # drawn value would overwrite it.  Checked on the merge log and the
    # prompts; the push itself is not sent (a drawn map does not carry
    # the routes' records, so it also lists them for deletion: answered No).
    boxes = push(mx, user, "push from the drawn project",
                 plan={"LOCs Will Be Deleted": "No", "Map Is Behind LOC": "Push"},
                 push_sent=None)
    ml = user.merge_line()
    stale = boxes.get("Map Is Behind LOC", {}).get("text", "")
    mx.step("LOC's change on S1 is not detected (known limit)",
            "Merge with LOC: 0 kept, 0 conflicts; S1 not in Map Is Behind LOC",
            str(ml), bool(ml) and ml[0] == 0 and ml[3] == 0 and p["uid"] not in stale)
    if "LOCs Will Be Deleted" in boxes:
        mx.step("observed: deletion prompt for records the drawn map does not carry",
                "noted, answered No", "shown", True,
                [boxes["LOCs Will Be Deleted"]["text"][:400]])
        mx.check("LOC unchanged after No", s0, mx.server())
        return
    value_step(mx, f"{p['uid']}.{p['lf']}: the drawn value overwrites LOC's", drawn_value,
               field_of(mx.server(), p["lid"], p["lf"]))


def _drawn_copy(src):
    """*src* drawn by hand: the same features and attributes, none of the
    plugin's own columns."""
    from qgis.core import QgsWkbTypes
    keep = [f for f in src.fields() if not f.name().startswith("_")]
    geom = QgsWkbTypes.displayString(src.wkbType())
    mem = QgsVectorLayer(f"{geom}?crs={src.crs().authid()}",
                         src.name().replace(" (LOC)", " drawn"), "memory")
    mem.dataProvider().addAttributes(keep)
    mem.updateFields()
    feats = []
    for f in src.getFeatures():
        g = QgsFeature(mem.fields())
        g.setGeometry(QgsGeometry(f.geometry()))
        g.setAttributes([f[k.name()] for k in keep])
        feats.append(g)
    mem.dataProvider().addFeatures(feats)
    return mem


@title("Plan 28 · limit: LOC changes S1, QGIS deletes S1 → push → Yes")
def test_live_merge_28_deleted_vs_changed(mx, user):
    lv = mx.lv
    from test_live_videos import pull
    pull(lv)
    p = pick_one(mx)
    if p["stops"]:
        pytest.skip("no structure without route stops on this location")
    s0 = change_in_loc(mx, loc_payload(mx), p, p["lf"], f"QA-P28-LOC-{STAMP}")
    layer, f = find_point(p["lid"])
    user_delete(lv, layer, f.id(), p["uid"])
    boxes = push(mx, user, f"{p['uid']} deleted on the older map → push",
                 plan={"LOCs Will Be Deleted": "Yes", "Map Is Behind LOC": "Push"})
    box = boxes.get("LOCs Will Be Deleted", {})
    mx.step("deletion prompt lists S1", p["uid"], "listed" if p["uid"] in box.get("text", "")
            else "NOT listed", p["uid"] in box.get("text", ""))
    mx.check("LOC: S1 deleted although LOC changed it", s0, mx.server(), removed=[p["lid"]])


# keep the unused-import linters quiet for helpers used only in some modes
_ = (QgsPointXY, _xy, diff, SB)
