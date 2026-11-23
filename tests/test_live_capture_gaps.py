"""Brandon's two scenarios (10 Nov 2026), fixed by the merge with LOC
(``core/rebase.py``), run against a real server with the project's
capture switch on and off:

(a) someone changes a LOC in LOC (the app on site, or the web) after the
    QGIS user pulled; the QGIS user then pushes from that older map.
(b) someone adds a stop to a route in LOC after the QGIS user pulled; the
    QGIS user then pushes (Push anyway where the stale-map warning shows).

Each passes when LOC keeps the other side's change and the QGIS user's own
edit lands.  Results go to ``matrix/probes.jsonl``, apart from the matrix.

    LOC_LIVE_BASE_URL=https://stagingapi.loc.store LOC_LIVE_GID=Jameesh \\
        LOC_LIVE_LOCATION=Howth LOC_LIVE_CAPTURE=on|off \\
        tests/run_live_videos.sh --allow-push -k capture_gaps
"""

import copy
import uuid

import pytest
from qgis.PyQt.QtWidgets import QMessageBox

from test_live_matrix import (  # noqa: F401
    STAMP, lv, mx, pull, preview_then_cancel, push_for_real, records, routes,
    server_rec, standalone_points, target, title, act, _location_id,
)

pytestmark = [pytest.mark.live, pytest.mark.live_write]
RESULTS_FILE = "probes.jsonl"


def _payload(lv):
    from label_on_a_cable.core.tasks import _sent_payload
    lv.msgbox.answers = [QMessageBox.StandardButton.Ignore]   # if the map is behind
    return copy.deepcopy(_sent_payload(preview_then_cancel(lv)["payload"]))


@title("Probe a · LOC changed elsewhere after the pull → push from the older map")
def test_live_capture_gaps_a_changed_in_loc(mx):
    lv = mx.lv
    pull(lv)
    (p,) = standalone_points(lv, mx.baseline, 1)
    # Someone else changes that structure's field in LOC.
    elsewhere = f"QA-PROBE-A-{STAMP}"
    pl = _payload(lv)
    rec = next(s for s in pl["single_locs"] if s["attributes"].get("loc_id") == p["lid"])
    for d in (rec.get("fields"), rec["attributes"].get("fields")):
        if isinstance(d, dict):
            d[p["lf"]] = elsewhere
    mx.api.push_locs(pl)
    s0 = mx.server()
    got0 = (records(s0)[p["lid"]][1].get("fields") or {}).get(p["lf"])
    mx.step("someone else changed it in LOC", f"{p['lf']} = {elsewhere}", repr(got0),
            got0 == elsewhere)
    act(lv, f"Elsewhere: {p['uid']}.{p['lf']} changed to {elsewhere!r} in LOC")
    # The QGIS user, on the older map, edits another structure and pushes.
    q = next(c for c in standalone_points(lv, mx.baseline, 3) if c["lid"] != p["lid"])
    from test_live_matrix import edit
    edit(lv, q, f"QA-PROBE-A-QGIS-{STAMP}")
    seen = push_for_real(lv)
    dialogs = seen.get("dialogs", [])
    titles = [c[1] for c in dialogs]
    after = mx.server()
    got1 = (records(after)[p["lid"]][1].get("fields") or {}).get(p["lf"])
    got_q = (records(after)[q["lid"]][1].get("fields") or {}).get(q["lf"])
    warned = any(p["uid"] in t for _k, tt, t in dialogs if tt == "Map Is Behind LOC")
    mx.step("QGIS user edits another structure and pushes from the older map",
            f"LOC keeps {p['uid']}.{p['lf']} = {elsewhere!r}; the QGIS edit lands; "
            "the warning names the change",
            f"{p['uid']}: {got1!r}; {q['uid']}: {got_q!r}; warned: {warned}; "
            f"prompts: {', '.join(titles) or 'none'}",
            got1 == elsewhere and got_q == f"QA-PROBE-A-QGIS-{STAMP}" and warned
            and "Changed in QGIS and in LOC" not in titles,
            [t[:300] for _k, _t, t in dialogs])
    seen = push_for_real(lv)
    got2 = (records(mx.server())[p["lid"]][1].get("fields") or {}).get(p["lf"])
    mx.step("second push from the same map", f"still {elsewhere!r}", repr(got2),
            got2 == elsewhere)


@title("Probe b · a stop added to a pulled route in LOC → push from the older map")
def test_live_capture_gaps_b_stop_added_in_loc(mx):
    lv = mx.lv
    pull(lv)
    pl = _payload(lv)
    m = next(m for m in pl["multiLocs"] if len(m.get("Stops", [])) >= 2)
    mid, name = m["id"], m.get("multiName")
    a, b = m["Stops"][0], m["Stops"][1]
    st = copy.deepcopy(a)
    sl = st["singleLoc"]
    uid = f"{name}_QA-PROBE-STOP-{STAMP}"
    st["stop_id"] = str(uuid.uuid4())
    sl["loc_id"] = str(uuid.uuid4())
    sl["origin_id"] = str(uuid.uuid4())
    for k in ("id", "createdAt", "updatedAt"):
        sl.pop(k, None)
    sl["unique_asset_id"] = uid
    sl["actual_asset_name"] = f"QA probe stop"
    for k in ("origin_longitude", "origin_latitude"):
        sl[k] = (float(a["singleLoc"][k]) + float(b["singleLoc"][k])) / 2
    m["Stops"].insert(1, st)
    for i, s_ in enumerate(m["Stops"]):
        s_["stopNumber"] = i + 1
    mx.api.push_locs(pl)
    s0 = mx.server()
    new = server_rec(s0, uid)
    on_route = bool(new) and new["loc_id"] in routes(s0)[mid][2]
    mx.step(f"someone else added a stop to {name} in LOC", "stop on the route",
            "on the route" if on_route else "NOT on the route", on_route)
    act(lv, f"Elsewhere: a stop is added to {name} in LOC")
    # The QGIS user pushes from the older map (default answers).
    lv.msgbox.answers = []
    seen = push_for_real(lv)
    dialogs = seen.get("dialogs", [])
    titles = [c[1] for c in dialogs]
    s1 = mx.server()
    still = server_rec(s1, uid)
    want_order = routes(s0)[mid][2]
    got_order = routes(s1)[mid][2]
    warned = any("1 stop(s) were added to routes in LOC" in t for _k, tt, t in dialogs)
    mx.step("QGIS user pushes from the older map",
            "the stop stays on the route in LOC's order; warning says so; no deletion prompt",
            f"stop {'kept' if still else 'GONE'}; route order "
            f"{'same as LOC' if got_order == want_order else 'DIFFERENT'}; warned: {warned}; "
            f"prompts: {', '.join(titles) or 'none'}",
            bool(still) and got_order == want_order and warned
            and "LOCs Will Be Deleted" not in titles, [t[:300] for _k, _t, t in dialogs])
    seen = push_for_real(lv)
    s2 = mx.server()
    mx.step("second push from the same map", "route unchanged", 
            "unchanged" if routes(s2)[mid][2] == want_order else "CHANGED",
            routes(s2)[mid][2] == want_order)
