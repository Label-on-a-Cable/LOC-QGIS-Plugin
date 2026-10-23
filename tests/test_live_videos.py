"""Read-only recorded scenarios against a real LOC server.

Every scenario signs in, opens the Workspace, picks the configured
Global Identifier → Location, pulls, and then works on the pulled data.
Nothing is ever written to LOC:

* Push is only ever taken as far as the server's dry-run preview
  (``?stats=true``), which is then cancelled.
* As a safety net, a non-preview ``push_locs`` call raises immediately.

Configuration (environment; never stored in the repo):

    LOC_LIVE_EMAIL, LOC_LIVE_PASSWORD   test account
    LOC_LIVE_BASE_URL                   default: the plugin's .env origin
    LOC_LIVE_GID, LOC_LIVE_LOCATION     default: "Test GID", "Test Location"

Run with ``tests/run_live_videos.sh`` (prompts for the password).
``--live-target=fake`` runs the same scenarios against the fake server.
"""

import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY,
    QgsProject, QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import QDialog, QFormLayout, QLabel, QPlainTextEdit, QVBoxLayout

import label_on_a_cable
from helpers import pump, refresh_map, save_project, seed_server, show_map, wait_until
from label_on_a_cable.models.route import StopType
from label_on_a_cable.qt_compat import BB_OK
from label_on_a_cable.services import config as loc_config
from label_on_a_cable.ui.login_dialog import LoginDialog
from label_on_a_cable.ui.pull_dialog import PullDialog
from label_on_a_cable.ui.push_preview_dialog import PushPreviewDialog

pytestmark = pytest.mark.live


# ----------------------------------------------------------------------
# Target + fixture
# ----------------------------------------------------------------------

@pytest.fixture
def target(request, tmp_path, monkeypatch):
    email = os.environ.get("LOC_LIVE_EMAIL")
    if email and request.config.getoption("--live-target") == "real":
        base = (os.environ.get("LOC_LIVE_BASE_URL")
                or loc_config._read_env_file(loc_config._ENV_PATH).get("LOC_BASE_URL")
                or loc_config.DEFAULT_BASE_URL).rstrip("/")
        t = SimpleNamespace(
            base=base, email=email, password=os.environ["LOC_LIVE_PASSWORD"],
            gid=os.environ.get("LOC_LIVE_GID", "Test GID"),
            location=os.environ.get("LOC_LIVE_LOCATION", "Test Location"),
            server=None)
    elif request.config.getoption("--live-target") == "fake":
        from fake_loc_server import FakeLocServer
        srv = FakeLocServer().start()

        may_write = request.node.get_closest_marker("live_write") is not None

        def check_read_only():
            writes = [r for r in srv.requests_to("LOCs/plugin-v3", "POST")
                      if r["query"].get("stats") != "true"]
            srv.stop()
            assert may_write or not writes, "read-only live scenario sent a real push"
        request.addfinalizer(check_read_only)
        seed_server(srv, tmp_path)
        QgsProject.instance().clear()
        t = SimpleNamespace(base=srv.base_url, email="eng@test.io", password="secret",
                            gid="Test GID", location="Testing Location", server=srv)
    else:
        pytest.skip("live run needs LOC_LIVE_EMAIL / LOC_LIVE_PASSWORD "
                    "(use tests/run_live_videos.sh)")
    monkeypatch.setenv("LOC_BASE_URL", t.base)
    return t


# Real pushes only ever go to a test server: a host with one of these
# labels (e.g. stagingapi.loc.store), never production.
TEST_HOST_LABELS = ("staging", "dev", "test", "qa", "sandbox", "preview")


def is_test_host(base_url):
    labels = (urlparse(base_url).hostname or "").lower().split(".")
    return any(label.startswith(t) for label in labels[:-1] for t in TEST_HOST_LABELS)


def _push_allowed(request, target):
    if request.node.get_closest_marker("live_write") is None:
        return False
    if target.server is not None:          # fake server: always fine
        return True
    if os.environ.get("LOC_LIVE_ALLOW_PUSH") != "1":
        pytest.skip("push scenario: run tests/run_live_videos.sh --allow-push")
    if not is_test_host(target.base):
        pytest.skip(f"pushes only run against a staging/test server, not {target.base}")
    return True


@pytest.fixture
def lv(request, target, iface, drive_modal, msgbox, opened_urls, recorder, tmp_path,
       monkeypatch):
    allow_push = _push_allowed(request, target)
    host = urlparse(target.base).netloc
    recorder.title = f"{recorder.title}   ·   {host} · {target.gid} › {target.location}"
    p = label_on_a_cable.classFactory(iface)
    p.initGui()

    real_push = p.api.push_locs

    def guarded_push(payload, preview=False):
        if not preview:
            if not allow_push:
                raise RuntimeError("live scenarios are read-only: real push blocked")
            active = p.active_location.name if p.active_location else ""
            if target.server is None and (not is_test_host(p.api.base_url)
                                          or active != target.location):
                raise RuntimeError(f"refusing to push to {p.api.base_url} / {active!r}")
        return real_push(payload, preview=preview)
    monkeypatch.setattr(p.api, "push_locs", guarded_push)

    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    save_project(tmp_path, "live_test.qgz")
    yield SimpleNamespace(plugin=p, iface=iface, t=target, drive=drive_modal,
                          msgbox=msgbox, rec=recorder, tmp=tmp_path)
    p.unload()


def title(text):
    def deco(fn):
        fn.video_title = text
        return fn
    return deco


# ----------------------------------------------------------------------
# Captioned user actions
# ----------------------------------------------------------------------

def act(lv, text, **kw):
    lv.rec.step(text, **kw)


_last_real_sign_in = [0.0]
SIGN_IN_GAP_S = 20   # real servers rate-limit logins (HTTP 429)


def sign_in(lv):
    if lv.t.server is None:
        import time
        wait = _last_real_sign_in[0] + SIGN_IN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_real_sign_in[0] = time.monotonic()

    def driver(dlg):
        act(lv, "Action: click the Login button on the LOC toolbar → the LOC Login dialog opens")
        dlg._email.setText(lv.t.email)
        act(lv, f"Action: type the email “{lv.t.email}”")
        dlg._password.setText(lv.t.password)
        act(lv, "Action: type the password (masked)")
        dlg._buttons.button(BB_OK).click()
        wait_until(lambda: dlg._task is None, timeout_ms=60000, message="login")
        if dlg._error_label.isVisible():
            act(lv, f"Login failed: {dlg._error_label.text()}", kind="bug")
    lv.drive(LoginDialog, driver)
    lv.plugin.actions["login"].trigger()
    assert lv.plugin.auth.is_logged_in, "login failed — check LOC_LIVE_EMAIL / password"
    act(lv, f"Result: signed in as {lv.plugin.auth.current_user.full_name or lv.t.email}; "
            "the other LOC toolbar buttons are enabled")


def open_location(lv):
    act(lv, "Action: click Workspace on the LOC toolbar")
    lv.plugin.actions["workspace"].trigger()
    sb = lv.plugin._sidebar
    wait_until(lambda: sb._task is None, timeout_ms=60000, message="locations")
    tree = sb._tree
    gids = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    gid = next((g for g in gids if g.text(0).startswith(f"{lv.t.gid} (")), None)
    assert gid is not None, f"Global Identifier {lv.t.gid!r} not in Workspace"
    tree.scrollToItem(gid)
    act(lv, f"Result: Workspace lists locations; expand “{lv.t.gid}”")
    loc = next((gid.child(i) for i in range(gid.childCount())
                if gid.child(i).text(0) == lv.t.location), None)
    assert loc is not None, f"Location {lv.t.location!r} not under {lv.t.gid!r}"
    tree.setCurrentItem(loc)
    tree.scrollToItem(loc)
    tree.itemClicked.emit(loc, 0)
    project = lv.plugin.active_location.project.name
    act(lv, f"Action: click “{lv.t.location}” (project “{project}”) → it becomes the active location")


def pull(lv):
    def driver(dlg):
        act(lv, "Action: click Pull from LOC → the plugin fetches the location's LOCs")
        wait_until(lambda: dlg._task is None, timeout_ms=120000, message="pull fetch")
        act(lv, "Result: Pull summary shows what LOC holds for this location")
        act(lv, "Action: click Import")
        dlg._import_btn.click()
    lv.drive(PullDialog, driver)
    lv.plugin.actions["pull"].trigger()
    show_map(lv.iface)
    n = len(lv.plugin.cached_routes)
    act(lv, f"Result: pulled layers added to the map; {n} route(s) rebuilt for review")


def generate(lv):
    act(lv, "Action: click Generate Route Labels")
    lv.plugin.actions["generate"].trigger()
    wait_until(lambda: lv.plugin._gen_task is None, timeout_ms=120000)
    pump(30)
    refresh_map(lv.iface)
    msg = lv.iface.messageBar().last()
    act(lv, f"Result: {msg[2] if msg else 'Route Review opens'}")


def close_review(lv):
    if lv.plugin._review_dlg is not None:
        act(lv, "Action: close Route Review (OK)")
        lv.plugin._review_dlg.close()
        pump()


def review_route(lv, route, stop=None):
    dlg = lv.plugin._review_dlg
    row = dlg._routes.index(route)
    dlg._route_table.selectRow(row)
    pump()
    if stop is not None:
        dlg._stop_table.selectRow(route.stops.index(stop))
        pump()
    refresh_map(lv.iface)
    return dlg


def preview_then_cancel(lv, inspect=None):
    """Open Push → server dry run → cancel. Never confirms."""
    seen = {}

    def driver(dlg):
        act(lv, "Result: Push Preview opens and asks the server for a dry run")
        wait_until(lambda: dlg._task is None, timeout_ms=200000, message="preview")
        seen["dialog"], seen["payload"] = dlg, dlg._payload
        seen["stats"] = _stats(dlg)
        act(lv, "Result: server dry-run answer (nothing has been written)")
        if inspect:
            inspect(dlg)
        act(lv, "Action: click Cancel (read-only test, nothing is pushed)")
        dlg.reject()
    lv.drive(PushPreviewDialog, driver)
    act(lv, "Action: click Push to LOC")
    lv.plugin.actions["push"].trigger()
    pump(30)
    return seen


def _stats(dlg):
    out = {}
    lay = dlg._server_layout
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if isinstance(sub, QFormLayout):
            for r in range(sub.rowCount()):
                k = sub.itemAt(r, QFormLayout.ItemRole.LabelRole).widget().text()
                v = sub.itemAt(r, QFormLayout.ItemRole.FieldRole).widget().text()
                out[k.rstrip(":")] = v
    return out


def inspector(lv, heading, lines, caption, ok=None):
    dlg = QDialog(lv.iface.mainWindow())
    dlg.setWindowTitle(f"Test inspector — {heading}")
    dlg.resize(940, 380)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(f"<b>Test inspector (not plugin UI) — {heading}</b>"))
    te = QPlainTextEdit()
    te.setReadOnly(True)
    te.setFont(QFont("DejaVu Sans Mono", 10))
    te.setPlainText("\n".join(lines))
    lay.addWidget(te)
    dlg.show()
    pump()
    kind = "step" if ok is None else ("ok" if ok else "bug")
    lv.rec.step(caption, kind=kind, widget=dlg, seconds=3.5)
    dlg.close()


# ----------------------------------------------------------------------
# Data-driven picks on whatever the location contains
# ----------------------------------------------------------------------

def _layer(lid):
    return QgsProject.instance().mapLayer(lid)


def _vertices(route):
    feat = _layer(route.line_layer_id).getFeature(route.line_feature_id)
    g = feat.geometry()
    if g.isMultipart():
        return [p for part in g.asMultiPolyline() for p in part]
    return g.asPolyline()


def _stop_xy(stop):
    lyr = _layer(stop.point_layer_id)
    if not isinstance(lyr, QgsVectorLayer):
        return None
    f = lyr.getFeature(stop.point_feature_id)
    return f.geometry().asPoint() if f.isValid() else None


def _same(a, b):
    return round(a.x(), 8) == round(b.x(), 8) and round(a.y(), 8) == round(b.y(), 8)


def pick_route(plugin):
    """A pulled route with ≥1 passthrough stop and two consecutive
    intermediate stops at distinct points (smallest such route)."""
    best = None
    for r in plugin.cached_routes:
        inter = [s for s in r.intermediate_stops if s.point_feature_id >= 0]
        if len(inter) < 3 or not any(s.stop_type == StopType.PASSTHROUGH for s in inter):
            continue
        verts = _vertices(r)
        for i in range(1, len(verts) - 2):
            a, b = verts[i], verts[i + 1]
            if _same(a, b):
                continue
            at_a = [s for s in inter if _stop_xy(s) and _same(_stop_xy(s), a)]
            at_b = [s for s in inter if _stop_xy(s) and _same(_stop_xy(s), b)]
            if at_a and at_b:
                if best is None or len(r.stops) < len(best[0].stops):
                    best = (r, a, b, at_a[-1], at_b[0])
                break
    assert best, "no pulled route with two consecutive intermediate stops"
    return best


def zoom_to(lv, points, pad=0.0006):
    xs, ys = [p.x() for p in points], [p.y() for p in points]
    lv.iface.mapCanvas().setExtent(QgsRectangle(
        min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))
    refresh_map(lv.iface)


def add_structure_between(lv, route, a, b, near_stop):
    """Add a point (same layer as *near_stop*) midway between vertices a, b."""
    lyr = _layer(near_stop.point_layer_id)
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY((a.x() + b.x()) / 2,
                                                     (a.y() + b.y()) / 2)))
    names = lyr.fields().names()
    if "Unique Asset Identifier" in names:
        f.setAttribute("Unique Asset Identifier", "QA-TEST-NEW")
    if "Actual Asset Name" in names:
        f.setAttribute("Actual Asset Name", "QA New Structure")
    assert lyr.dataProvider().addFeatures([f])[0]
    refresh_map(lv.iface)
    return lyr


def find_route(plugin, line_layer_id, line_feature_id):
    return next(r for r in plugin.cached_routes
                if (r.line_layer_id, r.line_feature_id) == (line_layer_id, line_feature_id))


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------

@title("Live walkthrough · sign in → pull → generate → review → preview")
def test_live_walkthrough(lv):
    act(lv, "Start: an empty, saved QGIS project with the LOC plugin loaded")
    sign_in(lv)
    open_location(lv)
    pull(lv)
    generate(lv)
    dlg = lv.plugin._review_dlg
    if dlg is not None and dlg._routes:
        review_route(lv, dlg._routes[0])
        act(lv, "Action: select the first route → its stops are listed and highlighted on the map")
    close_review(lv)
    seen = preview_then_cancel(lv)
    act(lv, f"Done: dry run returned {seen.get('stats') or 'no stats'}; nothing was written",
        kind="ok")


@title("Live · Bug 10 · pulled routes show ‘Modified’ straight after a pull")
def test_live_fresh_pull_status(lv):
    sign_in(lv)
    open_location(lv)
    pull(lv)
    generate(lv)
    dlg = lv.plugin._review_dlg
    states = [dlg._route_change_state(r) for r in dlg._routes]
    with_stops = [s for r, s in zip(dlg._routes, states) if r.intermediate_stops]
    modified = sum(1 for s in with_stops if s == "Modified")
    ok = modified == 0
    act(lv, f"Nothing was edited, yet {modified} of {len(with_stops)} routes with stops "
            "show ‘Modified’" if not ok else "Unedited pulled routes show ‘No changes’",
        kind="ok" if ok else "bug")
    assert ok


@title("Live · Bug 4 · new structure lands at the end of the stop list")
def test_live_snapped_stop_order(lv):
    sign_in(lv)
    open_location(lv)
    pull(lv)
    route, a, b, sa, sb = pick_route(lv.plugin)
    zoom_to(lv, [a, b] + [p for p in (_stop_xy(s) for s in route.stops[:4]) if p])
    act(lv, f"Chosen route {route.line_name}: zoomed to {sa.structure_name} → {sb.structure_name}")
    add_structure_between(lv, route, a, b, sa)
    act(lv, f"Action: add a new structure “QA New Structure” on the cable between "
            f"{sa.structure_name} and {sb.structure_name} (in memory, never pushed)")
    key = (route.line_layer_id, route.line_feature_id)
    generate(lv)
    r2 = find_route(lv.plugin, *key)
    new = next(s for s in r2.stops if s.structure_name == "QA New Structure")
    dlg = review_route(lv, r2, new)
    names = [s.structure_name for s in r2.stops]
    i = names.index("QA New Structure")
    ok = names[i - 1] == sa.structure_name
    act(lv, f"Route Review lists the new structure at position {i} of {len(names) - 2}, "
            f"after {names[i - 1]}. It was drawn right after {sa.structure_name}."
        if not ok else "New structure sits where it was drawn",
        kind="ok" if ok else "bug")
    assert ok


@title("Live · Bug 1 · remove one stop + add one stop on a pulled route")
def test_live_remove_one_add_one(lv):
    sign_in(lv)
    open_location(lv)
    pull(lv)
    route, a, b, sa, sb = pick_route(lv.plugin)
    before = route.active_stop_count
    zoom_to(lv, [a, b])
    add_structure_between(lv, route, a, b, sa)
    act(lv, f"Action: add “QA New Structure” on {route.line_name} between "
            f"{sa.structure_name} and {sb.structure_name}")
    key = (route.line_layer_id, route.line_feature_id)
    generate(lv)
    r2 = find_route(lv.plugin, *key)
    victim = next(s for s in r2.intermediate_stops if s.stop_type == StopType.PASSTHROUGH
                  and s.structure_name not in ("QA New Structure",))
    dlg = review_route(lv, r2, victim)
    act(lv, f"Result: {r2.line_name} now has {r2.active_stop_count} stops (was {before})")
    dlg._btn_remove.click()
    pump()
    act(lv, f"Action: select {victim.structure_name} and click Remove → "
            f"{r2.active_stop_count} stops, same as at pull time")
    close_review(lv)
    preview_then_cancel(lv)
    blocked = [c for c in lv.msgbox.calls if c[1] == "Push Validation Failed"]
    if blocked:
        lv.rec.show_message_box(
            *blocked[-1], frame_kind="bug",
            caption="The new structure was given an existing stop's LOC record "
                    "(duplicate loc_id), so the push is blocked.")
    else:
        act(lv, "Push was not blocked: the new structure got its own record", kind="ok")
    assert not blocked


@title("Live · Bug 3 · moving a pulled point is not pushed")
def test_live_moved_point(lv):
    sign_in(lv)
    open_location(lv)
    pull(lv)
    lyr = next(l for l in QgsProject.instance().mapLayers().values()
               if isinstance(l, QgsVectorLayer) and "_sloc_json" in l.fields().names()
               and l.featureCount())
    feat = next(lyr.getFeatures())
    old = feat.geometry().asPoint()
    name = feat["Actual Asset Name"] or feat["Unique Asset Identifier"]
    zoom_to(lv, [old, QgsPointXY(old.x() + 0.0005, old.y())])
    act(lv, f"Chosen point: {name} in layer “{lyr.name()}”")
    new_x = old.x() + 0.0005
    lyr.dataProvider().changeGeometryValues(
        {feat.id(): QgsGeometry.fromPointXY(QgsPointXY(new_x, old.y()))})
    refresh_map(lv.iface)
    act(lv, f"Action: move {name} about 33 m east (in memory, never pushed)")
    loc_id = feat["_loc_id"]
    result = {}

    def inspect(dlg):
        sent = next(s for s in dlg._payload["single_locs"]
                    if s["attributes"].get("loc_id") == loc_id)
        lon = sent["coordinates"][0]
        result["ok"] = ok = abs(lon - new_x) < 1e-9
        inspector(lv, f"what the plugin would send for {name}", [
            f"QGIS geometry after the move:  lon {new_x:.9f}",
            f"Longitude in the push payload: lon {lon:.9f}",
            f"Longitude at pull time:        lon {old.x():.9f}",
        ], f"The push would send the old position: the move of {name} is silently dropped"
           if not ok else "The push carries the moved position", ok)
    preview_then_cancel(lv, inspect)
    assert result["ok"]


# Connect generously, but give the server only 0.3 s to answer: a real
# *read* timeout, i.e. the server is reachable but slow.  (A plain 0.05 s
# timeout expires while connecting instead, whose error text happens to
# contain "timeout" and so hides the bug.)
SLOW_SERVER_TIMEOUT = (15, 0.3)


def _simulate_slow_server(lv, monkeypatch):
    import label_on_a_cable.services.api_client as api_mod
    monkeypatch.setattr(api_mod, "PUSH_TIMEOUT", SLOW_SERVER_TIMEOUT)
    # The plugin logs "timeout=%ds"; a (connect, read) pair can't format as
    # %d, and pytest's log capture raises on that. Log it as %s instead.
    real_error = api_mod._log.error
    monkeypatch.setattr(api_mod._log, "error",
                        lambda msg, *a, **k: real_error(msg.replace("%ds", "%ss"), *a, **k))
    if lv.t.server is not None:
        lv.t.server.delay["LOCs/plugin-v3"] = 1.5


@title("Live · Bug 6 · preview that times out still allows Push")
def test_live_preview_timeout(lv, monkeypatch):
    sign_in(lv)
    open_location(lv)
    pull(lv)
    _simulate_slow_server(lv, monkeypatch)
    act(lv, "Setup: the test allows 15 s to connect but only 0.3 s for the server "
            "to answer (a reachable but slow server)")
    result = {}

    def inspect(dlg):
        text = dlg._server_status.text().lower()
        result["timed_out"] = "timed out" in text or "timeout" in text
        result["enabled"] = dlg._push_btn.isEnabled()
        if not result["timed_out"]:
            act(lv, "Not reproduced: the server answered the dry run within 0.3 s")
            return
        act(lv, "The dry run timed out, so nothing was validated, but Push is enabled"
            if result["enabled"] else "Push stays disabled after the preview timed out",
            kind="bug" if result["enabled"] else "ok")
    preview_then_cancel(lv, inspect)
    if not result.get("timed_out"):
        pytest.skip("server answered within the read timeout; nothing to judge")
    assert not result["enabled"]


# ======================================================================
# Scenarios that really push — staging/test servers only (--allow-push)
# ======================================================================

def _location_id(lv):
    return lv.plugin.active_location.location_id


def backup(lv, label):
    """Save the location's current LOC data before anything is pushed."""
    import datetime
    import json as _json
    from conftest import VIDEO_DIR
    data = lv.plugin.api.get_locs_for_location(_location_id(lv))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = VIDEO_DIR / "live" / "backups" / f"{label}-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(data), encoding="utf-8")
    act(lv, f"Safety: backed up {lv.t.location} "
            f"({len(data.get('singleLOCs', []))} single, {len(data.get('dualLOCs', []))} "
            f"dual, {len(data.get('multiLoc', []))} multi LOCs) → {path.name}")
    return data


def push_for_real(lv, during_preview=None):
    """Push → preview → click Push for real. Returns what was seen."""
    seen = {}

    def driver(dlg):
        act(lv, "Result: Push Preview opens and asks the server for a dry run")
        wait_until(lambda: dlg._task is None, timeout_ms=200000, message="preview")
        seen["stats"] = _stats(dlg)
        seen["payload"] = dlg._payload
        act(lv, "Result: server dry-run answer")
        if during_preview:
            during_preview(dlg)
        if dlg._push_btn.isEnabled():
            act(lv, f"Action: click Push → real push to {lv.t.location} on staging")
            dlg._push_btn.click()
            seen["pushed"] = True
        else:
            act(lv, "Push is disabled; click Cancel")
            dlg.reject()
    lv.drive(PushPreviewDialog, driver)
    calls_before = len(lv.msgbox.calls)
    act(lv, "Action: click Push to LOC")
    lv.plugin.actions["push"].trigger()
    wait_until(lambda: lv.plugin._push_task is None, timeout_ms=300000, message="push")
    pump(50)
    seen["dialogs"] = lv.msgbox.calls[calls_before:]
    msg = lv.iface.messageBar().last()
    if seen.get("pushed") and msg:
        act(lv, f"Result: {msg[2]}")
    return seen


def new_project(lv, name):
    close_review(lv)
    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    d = lv.tmp / name
    d.mkdir(exist_ok=True)
    save_project(d, f"{name}.qgz")
    refresh_map(lv.iface)
    act(lv, f"Action: start a new, saved QGIS project “{name}.qgz”")
    return d


def restore(lv, data):
    """Push the backup back so the location ends as it started."""
    from label_on_a_cable.core import import_builder as ib
    from label_on_a_cable.models.category import Category
    new_project(lv, "restore")
    cats = Category.list_from_api(
        lv.plugin.api.get_categories_by_location(_location_id(lv)))
    layers, maps, ids = ib.build_layers(data, cats)
    lv.plugin._on_pull_complete(layers, maps, ids, ib.extract_multi_stop_counts(data))
    lv.plugin._categories_cache = {c.category_id: c for c in cats}
    show_map(lv.iface)
    act(lv, "Cleanup: load the backup taken at the start into this project")
    push_for_real(lv)
    act(lv, f"Cleanup done: {lv.t.location} restored from the backup")


def _updated_total(stats):
    total = 0
    for k, v in (stats or {}).items():
        if k.lower().startswith("updated"):
            try:
                total += int(v)
            except ValueError:
                pass
    return total


def diff_report(payload, pulled):
    """Which keys the payload sends differently from the pulled records."""
    from collections import Counter
    changed, added = Counter(), Counter()

    def compare(kind, sent, raw):
        for k, v in sent.items():
            if k.startswith("_") or k == "Stops":
                continue
            if k not in raw:
                added[f"{kind}.{k}"] += 1
            elif raw[k] != v:
                changed[f"{kind}.{k}"] += 1

    singles = {s.get("loc_id"): s for s in pulled.get("singleLOCs", [])}
    for s in payload.get("single_locs", []):
        raw = singles.get(s["attributes"].get("loc_id"))
        if raw:
            compare("single", s["attributes"], raw)
    duals = {d.get("loc_id"): d for d in pulled.get("dualLOCs", [])}
    for d in payload.get("dual_locs", []):
        if d.get("loc_id") in duals:
            compare("dual", d, duals[d["loc_id"]])
    multis = {m.get("id"): m for m in pulled.get("multiLoc", [])}
    for m in payload.get("multiLocs", []):
        raw = multis.get(m.get("id"))
        if not raw:
            continue
        compare("multi.dualLoc", m["dualLoc"], raw.get("dualLoc") or raw.get("dualLOC") or {})
        raw_stops = {s.get("stop_id"): s for s in raw.get("Stops", raw.get("stops", []))}
        for s in m.get("Stops", []):
            rs = raw_stops.get(s.get("stop_id"))
            if rs:
                compare("stop.singleLoc", s["singleLoc"],
                        rs.get("singleLoc") or rs.get("singleLOC") or {})
    lines = ["Keys whose value differs from the pulled record (records affected):"]
    lines += [f"  changed  {k:<40} {n}" for k, n in changed.most_common(14)] or ["  (none)"]
    lines += ["", "Keys the plugin adds that the pulled record did not have:"]
    lines += [f"  added    {k:<40} {n}" for k, n in added.most_common(10)] or ["  (none)"]
    return lines


@pytest.mark.live_write
@title("Staging push · unedited pull → push → pull: does LOC still report updates?")
def test_live_push_round_trip(lv):
    sign_in(lv)
    open_location(lv)
    data = backup(lv, "round_trip")
    pull(lv)
    first = {}

    def capture(dlg):
        first["updated"] = _updated_total(_stats(dlg))
        inspector(lv, "what the plugin would send vs what it pulled",
                  diff_report(dlg._payload, data),
                  f"Nothing edited, yet the dry run reports {first['updated']} updates. "
                  "These fields differ from the pulled records.")
    push_for_real(lv, during_preview=capture)
    new_project(lv, "round_trip_second_pull")
    pull(lv)
    second = {}

    def capture2(dlg):
        second["updated"] = _updated_total(_stats(dlg))
    preview_then_cancel(lv, inspect=capture2)
    ok = second["updated"] == 0
    inspector(lv, "updates reported for an unedited round trip", [
        f"1st dry run (pulled, nothing edited):     {first['updated']} updates",
        "→ real push of that unchanged data",
        f"2nd dry run (fresh pull after the push):  {second['updated']} updates",
    ], "Even after pushing, an unedited pull → push still rewrites records"
       if not ok else "After one push the round trip is clean (0 updates)", ok)
    assert ok


@pytest.mark.live_write
@title("Staging push · Bug 5 · push that times out is reported as a server warning")
def test_live_push_timeout(lv, monkeypatch):
    sign_in(lv)
    open_location(lv)
    backup(lv, "push_timeout")
    pull(lv)

    def slow(dlg):
        _simulate_slow_server(lv, monkeypatch)
        act(lv, "Setup: from now on the server gets only 0.3 s to answer "
                "(the push itself still reaches it)")
    seen = push_for_real(lv, during_preview=slow)
    if not seen["dialogs"]:
        act(lv, "Not reproduced: the server finished the push within 0.3 s")
        pytest.skip("push completed within the read timeout")
    kind, ttl, text = seen["dialogs"][-1]
    assert "timed out" in text.lower(), f"push failed for another reason: {text[:200]}"
    ok = (kind, ttl) == ("critical", "Push Failed")
    lv.rec.show_message_box(
        kind, ttl, text, frame_kind="ok" if ok else "bug",
        caption="Timeout reported as Push Failed" if ok else
        "The client gave up waiting, yet the plugin says the data 'may have been "
        "written', saves the LOC IDs as if it was, and opens the LOC page.")
    pump(5000)  # let the server finish the push it is still processing
    assert ok


@pytest.mark.live_write
@title("Staging push · Bug 8 · saved LOC IDs leak from one project into the next")
def test_live_stale_stamps(lv):
    import json as _json
    sign_in(lv)
    open_location(lv)
    backup(lv, "stale_stamps")
    pull(lv)
    push_for_real(lv)
    a_sources = {l.source() for l in QgsProject.instance().mapLayers().values()}
    act(lv, "Project A pushed: its LOC IDs were saved next to project A")
    b_dir = new_project(lv, "project_b")
    pull(lv)
    push_for_real(lv)
    sidecar = _json.loads((b_dir / "project_b_loc_stamps.json").read_text())
    keys = list(sidecar.get(_location_id(lv), {}))
    foreign = [k for k in keys if k.rsplit(":", 1)[0] in a_sources]
    ok = not foreign
    inspector(lv, "project B's saved-ID file", [
        f"project_b_loc_stamps.json: {len(keys)} entries",
        f"entries that belong to project A's layers: {len(foreign)}",
        "",
        *[f"  {k[:90]}" for k in foreign[:12]],
    ], f"{len(foreign)} of {len(keys)} entries in project B's file came from project A"
       if not ok else "Project B's file only has its own entries", ok)
    assert ok


@pytest.mark.live_write
@title("Staging push · Bug 2 · a new stop reuses another stop's stop_id")
def test_live_new_stop_stop_id(lv):
    sign_in(lv)
    open_location(lv)
    data = backup(lv, "new_stop_stop_id")
    pull(lv)
    pushed = False
    try:
        route, a, b, sa, sb = pick_route(lv.plugin)
        line = _layer(route.line_layer_id).getFeature(route.line_feature_id)
        multi_id = line["_multi_id"]
        raw = next(m for m in data["multiLoc"] if m.get("id") == multi_id)
        raw_n = len(raw.get("Stops", raw.get("stops", [])))
        zoom_to(lv, [a, b])
        add_structure_between(lv, route, a, b, sa)
        act(lv, f"Action: add “QA New Structure” on {route.line_name} between "
                f"{sa.structure_name} and {sb.structure_name}")
        key = (route.line_layer_id, route.line_feature_id)
        generate(lv)
        r2 = find_route(lv.plugin, *key)
        target_n = raw_n - 1   # differ from the pulled count: no sequential fallback
        for s in list(r2.intermediate_stops):
            if r2.active_stop_count <= target_n:
                break
            if s.stop_type == StopType.PASSTHROUGH and s.structure_name != "QA New Structure":
                review_route(lv, r2, s)
                lv.plugin._review_dlg._btn_remove.click()
                pump()
                act(lv, f"Action: select {s.structure_name} and click Remove")
        act(lv, f"Result: {r2.line_name} has {r2.active_stop_count} stops "
                f"({raw_n} in LOC before)")
        close_review(lv)
        seen = push_for_real(lv)
        pushed = seen.get("pushed", False)
        blocked = [c for c in seen["dialogs"] if c[1] == "Push Validation Failed"]
        if blocked:
            lv.rec.show_message_box(*blocked[-1], frame_kind="bug",
                                    caption="Push blocked by the plugin's own duplicate check")
            pytest.fail("push blocked before it reached LOC")
        server = lv.plugin.api.get_locs_for_location(_location_id(lv))
        multi = next(m for m in server["multiLoc"] if m.get("id") == multi_id)
        held = len(multi.get("Stops", multi.get("stops", [])))
        sent = next(m for m in seen["payload"]["multiLocs"] if m.get("id") == multi_id)
        sent_ids = [s["stop_id"] for s in sent["Stops"]]
        lines = [f"{route.line_name}: stops in the push payload"]
        for s in sent["Stops"]:
            dup = "   <── DUPLICATE stop_id" if sent_ids.count(s["stop_id"]) > 1 else ""
            lines.append(f"  #{s['stopNumber']} {s['singleLoc']['unique_asset_id'][:46]:<46} "
                         f"{s['stop_id']}{dup}")
        lines += ["", f"Stops LOC holds for the route after the push: {held} "
                      f"(payload had {len(sent_ids)})"]
        ok = len(set(sent_ids)) == len(sent_ids)
        inspector(lv, f"{route.line_name} after the real push", lines,
                  ("Two stops in the payload share a stop_id"
                   + ("" if held == len(sent_ids)
                      else f"; LOC kept only {held} of {len(sent_ids)} stops"))
                  if not ok else "Every stop kept its own stop_id", ok)
        assert ok
    finally:
        if pushed:
            restore(lv, data)
