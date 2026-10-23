"""Recorded bug scenarios (Cypress-style videos).

Each scenario reproduces one known defect through the plugin's own
toolbar buttons and dialogs, recording a captioned frame per step.  The
last frame is a green OK frame when the plugin behaves correctly and a
red BUG frame (with a failing test) if the defect ever comes back.

Run:  tests/run_videos.sh        → tests/reports/videos/*.mp4
"""

import json
from types import SimpleNamespace

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsFeatureRequest, QgsGeometry,
    QgsPointXY, QgsProject,
)
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import QDialog, QFormLayout, QLabel, QPlainTextEdit, QVBoxLayout

import label_on_a_cable
from fake_loc_server import LOCATION_ID
from helpers import (
    ITM, build_network, feature_where, network_mappings, pump, refresh_map,
    save_project, seed_server, show_map, wait_until,
)
from label_on_a_cable.qt_compat import BB_OK
from label_on_a_cable.ui.category_mapping_dialog import CategoryMappingDialog
from label_on_a_cable.ui.login_dialog import LoginDialog
from label_on_a_cable.ui.pull_dialog import PullDialog
from label_on_a_cable.ui.push_preview_dialog import PushPreviewDialog

pytestmark = pytest.mark.visual


def title(text):
    def deco(fn):
        fn.video_title = text
        return fn
    return deco


@pytest.fixture
def v(iface, loc_server, drive_modal, msgbox, opened_urls, recorder, tmp_path):
    p = label_on_a_cable.classFactory(iface)
    p.initGui()
    yield SimpleNamespace(plugin=p, iface=iface, server=loc_server,
                          drive=drive_modal, msgbox=msgbox, urls=opened_urls,
                          tmp=tmp_path, rec=recorder)
    p.unload()


# ----------------------------------------------------------------------
# User-level steps, each recorded
# ----------------------------------------------------------------------

def login(v):
    def driver(dlg):
        dlg._email.setText("eng@test.io")
        dlg._password.setText("secret")
        v.rec.step("Click Login on the LOC toolbar and sign in")
        dlg._buttons.button(BB_OK).click()
        wait_until(lambda: dlg._task is None)
    v.drive(LoginDialog, driver)
    v.plugin.actions["login"].trigger()
    v.rec.step("Signed in: the rest of the LOC toolbar is now enabled")


def choose_location(v):
    v.plugin.actions["workspace"].trigger()
    sb = v.plugin._sidebar
    wait_until(lambda: sb._task is None)
    gid = sb._tree.topLevelItem(0)
    item = [gid.child(i) for i in range(gid.childCount())
            if gid.child(i).text(0) == "Testing Location"][0]
    sb._tree.setCurrentItem(item)
    sb._tree.itemClicked.emit(item, 0)
    v.rec.step("Workspace: select “Testing Location”")


def map_categories(v, mappings):
    def driver(dlg):
        wait_until(lambda: dlg._task is None)
        by_id = {m.layer_id: m for m in mappings}
        for row in dlg._layer_rows:
            if row.layer_id in by_id:
                row.restore(by_id[row.layer_id])
            else:
                row.reset()
        v.rec.step("Category Mapping: map each layer and its fields to a LOC category")
        dlg._buttons.button(BB_OK).click()
    v.drive(CategoryMappingDialog, driver)
    v.plugin.actions["mapping"].trigger()


def generate(v, caption):
    v.plugin.actions["generate"].trigger()
    wait_until(lambda: v.plugin._gen_task is None)
    pump(30)
    refresh_map(v.iface)
    v.rec.step(caption)


def close_review(v):
    if v.plugin._review_dlg is not None:
        v.plugin._review_dlg.close()
        pump()


def review_select(v, route_name, stop_name=None):
    dlg = v.plugin._review_dlg
    names = [dlg._route_table.item(r, 0).text() for r in range(dlg._route_table.rowCount())]
    dlg._route_table.selectRow(names.index(route_name))
    pump()
    if stop_name is not None:
        route = dlg._selected_route()
        idx = [s.structure_name for s in route.stops].index(stop_name)
        dlg._stop_table.selectRow(idx)
        pump()
    refresh_map(v.iface)
    return dlg


def push(v, preview_caption="Push to LOC: the server dry-run preview",
         during_preview=None, confirm=True):
    seen = {}

    def driver(dlg):
        wait_until(lambda: dlg._task is None)
        seen["stats"] = preview_stats(dlg)
        seen["dialog"] = dlg
        v.rec.step(preview_caption)
        if during_preview:
            during_preview(dlg)
        if confirm and dlg._push_btn.isEnabled():
            dlg._push_btn.click()
        else:
            dlg.reject()
    v.drive(PushPreviewDialog, driver)
    v.plugin.actions["push"].trigger()
    wait_until(lambda: v.plugin._push_task is None)
    pump(30)
    return seen


def preview_stats(dlg):
    out = {}
    lay = dlg._server_layout
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if isinstance(sub, QFormLayout):
            for r in range(sub.rowCount()):
                k = sub.itemAt(r, QFormLayout.ItemRole.LabelRole).widget().text()
                val = sub.itemAt(r, QFormLayout.ItemRole.FieldRole).widget().text()
                out[k.rstrip(":")] = val
    return out


def pull(v):
    def driver(dlg):
        wait_until(lambda: dlg._task is None)
        v.rec.step("Pull from LOC: summary of what the server holds")
        dlg._import_btn.click()
    v.drive(PullDialog, driver)
    v.plugin.actions["pull"].trigger()
    close_review(v)
    show_map(v.iface)
    v.rec.step("Pulled layers added to the project")


def evidence(v, heading, lines, caption, ok):
    """Show an inspection window (test tooling, not plugin UI)."""
    dlg = QDialog(v.iface.mainWindow())
    dlg.setWindowTitle(f"Test inspector — {heading}")
    dlg.resize(940, 380)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(f"<b>{heading}</b>"))
    te = QPlainTextEdit()
    te.setReadOnly(True)
    te.setFont(QFont("DejaVu Sans Mono", 10))
    te.setPlainText("\n".join(lines))
    lay.addWidget(te)
    dlg.show()
    pump()
    v.rec.step(caption, kind="ok" if ok else "bug", widget=dlg)
    dlg.close()


def verdict(v, ok, bug_caption, ok_caption):
    v.rec.step(ok_caption if ok else bug_caption, kind="ok" if ok else "bug")


# ----------------------------------------------------------------------
# Project set-ups
# ----------------------------------------------------------------------

def design_project(v):
    layers = build_network()
    save_project(v.tmp)
    show_map(v.iface)
    v.rec.step("A saved QGIS project with a small FTTH design (cables, poles, chamber)")
    login(v)
    choose_location(v)
    map_categories(v, network_mappings(layers))
    return layers


def pulled_project(v):
    seed_server(v.server, v.tmp)
    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    save_project(v.tmp, "fieldwork.qgz")
    v.rec.step("New, saved QGIS project. The design already exists in LOC.")
    login(v)
    choose_location(v)
    pull(v)


def layer(name):
    return [l for l in QgsProject.instance().mapLayers().values() if l.name() == name][0]


def add_pole_on_cbl001(v, between, name="New Pole"):
    """Add a pole on CBL-001, midway between two of its vertices."""
    poles, cables = layer("Pole (LOC)"), layer("Fiber Cable (LOC)")
    verts = feature_where(cables, "Route ID", "CBL-001").geometry().asPolyline()
    a, b = verts[between[0]], verts[between[1]]
    nf = QgsFeature(poles.fields())
    nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)))
    nf.setAttribute("Unique Asset Identifier", "P-NEW")
    nf.setAttribute("Actual Asset Name", name)
    poles.dataProvider().addFeatures([nf])
    refresh_map(v.iface)


def remove_stop_in_review(v, route, stop):
    dlg = review_select(v, route, stop)
    dlg._btn_remove.click()
    pump()
    refresh_map(v.iface)


# ----------------------------------------------------------------------
# 1–2. Stop identity after editing a pulled route
# ----------------------------------------------------------------------

@title("Bug 1 · Remove one stop + add one stop on a pulled route")
def test_video_remove_one_add_one(v):
    pulled_project(v)
    add_pole_on_cbl001(v, between=(4, 5))
    v.rec.step("Add a new pole on cable CBL-001, between Pole 3 and Pole 4")
    generate(v, "Generate Route Labels: routes rebuilt from the pulled layers")
    review_select(v, "CBL-001")
    v.rec.step("Route Review: CBL-001 now has 5 stops (4 pulled + the new pole)")
    remove_stop_in_review(v, "CBL-001", "Pole 2")
    v.rec.step("Remove Pole 2 → 4 stops again, the same count as at pull time")
    close_review(v)
    push(v)
    blocked = [c for c in v.msgbox.calls if c[1] == "Push Validation Failed"]
    if blocked:
        v.rec.show_message_box(
            *blocked[-1], frame_kind="bug",
            caption="The new pole was given Pole 3's existing LOC record (same "
                    "loc_id), so the push is blocked as a duplicate stop. Without "
                    "the check, LOC would overwrite Pole 3's stop.")
    else:
        verdict(v, True, "", "Push accepted: the new pole got its own LOC record")
    assert not blocked


@title("Bug 2 · New stop reuses another stop's stop_id")
def test_video_new_stop_duplicate_stop_id(v):
    pulled_project(v)
    add_pole_on_cbl001(v, between=(4, 5))
    v.rec.step("Add a new pole on cable CBL-001")
    generate(v, "Generate Route Labels: routes rebuilt from the pulled layers")
    remove_stop_in_review(v, "CBL-001", "Pole 2")
    remove_stop_in_review(v, "CBL-001", "Pole 3")
    v.rec.step("Remove Pole 2 and Pole 3 in Route Review (3 stops left)")
    close_review(v)
    push(v)
    (multi,) = [m for m in v.server.loc_state()["multi"].values()
                if m["multiName"] == "CBL-001"]
    ids = [s["stop_id"] for s in multi["Stops"]]
    lines = ["CBL-001 stops as stored in LOC after the push:", ""]
    for s in multi["Stops"]:
        dup = "   <── DUPLICATE stop_id" if ids.count(s["stop_id"]) > 1 else ""
        lines.append(f"  #{s['stopNumber']}  {s['singleLoc']['unique_asset_id']:<28} "
                     f"stop_id={s['stop_id']}{dup}")
    ok = len(set(ids)) == len(ids)
    evidence(v, "LOC server record for CBL-001", lines,
             "Two stops in one route share a stop_id, so LOC treats them as the same stop"
             if not ok else "Every stop has its own stop_id", ok)
    assert ok


# ----------------------------------------------------------------------
# 3. Moved pulled point
# ----------------------------------------------------------------------

@title("Bug 3 · Moving a pulled point in QGIS is never pushed")
def test_video_moved_point_not_pushed(v):
    pulled_project(v)
    poles = layer("Pole (LOC)")
    p5 = feature_where(poles, "Unique Asset Identifier", "P5")
    old = p5.geometry().asPoint()
    show_map(v.iface, extent_layers=[poles])
    v.rec.step("Pole 5 at its pulled position")
    new_x = old.x() + 0.001
    poles.dataProvider().changeGeometryValues(
        {p5.id(): QgsGeometry.fromPointXY(QgsPointXY(new_x, old.y()))})
    refresh_map(v.iface)
    v.rec.step("Move Pole 5 about 66 m east and save the layer")
    seen = push(v, "Push preview: the server is told nothing changed")
    stored = [s for s in v.server.loc_state()["single"].values()
              if s["unique_asset_id"] == "P5"][0]
    ok = abs(stored["origin_longitude"] - new_x) < 1e-9
    evidence(v, "Pole 5 position", [
        f"QGIS geometry (after move):   lon {new_x:.9f}",
        f"LOC server after push:        lon {stored['origin_longitude']:.9f}",
        f"Value captured at pull time:  lon {old.x():.9f}",
        "",
        f"Preview said: {seen.get('stats')}",
    ], "LOC still has the old position: the move was silently dropped"
       if not ok else "LOC now has the moved position", ok)
    assert ok


# ----------------------------------------------------------------------
# 4. Stop order for snapped additions
# ----------------------------------------------------------------------

@title("Bug 4 · New structure on a pulled route goes to the end of the stop list")
def test_video_snapped_stop_order(v):
    pulled_project(v)
    add_pole_on_cbl001(v, between=(1, 2))
    v.rec.step("Add a new pole between Pole 2 and Chamber 1 on CBL-001")
    generate(v, "Generate Route Labels: routes rebuilt from the pulled layers")
    dlg = review_select(v, "CBL-001", "New Pole")
    names = [s.structure_name for s in dlg._selected_route().stops]
    ok = names.index("New Pole") == 2
    verdict(v, ok,
            f"Route Review lists the new pole as stop {names.index('New Pole')} of "
            f"{len(names) - 2}, just before the destination. It should be third, "
            "right after Pole 2.",
            "New pole sits between Pole 2 and Chamber 1, as drawn")
    assert ok


# ----------------------------------------------------------------------
# 5–6. Client-side timeouts
# ----------------------------------------------------------------------

@title("Bug 5 · Push that times out is reported as a server warning")
def test_video_push_timeout(v, monkeypatch):
    import label_on_a_cable.services.api_client as api_mod
    design_project(v)
    generate(v, "Generate Route Labels: 2 routes, 4 stops")
    close_review(v)

    def slow(dlg):
        monkeypatch.setattr(api_mod, "PUSH_TIMEOUT", 0.3)
        v.server.delay["LOCs/plugin-v3"] = 1.5
        v.rec.step("Click Push. The server is slow and the request times out.")
    push(v, during_preview=slow)
    kind, ttl, text = v.msgbox.calls[-1]
    ok = (kind, ttl) == ("critical", "Push Failed")
    stamped = (v.tmp / "network_loc_stamps.json").exists()
    if ok:
        verdict(v, True, "", "Timeout reported as Push Failed")
    else:
        v.rec.show_message_box(
            kind, ttl, text, frame_kind="bug",
            caption="A timeout should be a hard failure. Instead the plugin says "
                    "the data 'may have been written'"
                    f"{', saves the LOC IDs as if it was' if stamped else ''} "
                    "and opens the LOC page.")
    assert ok


@title("Bug 6 · Preview that times out still lets you push")
def test_video_preview_timeout(v, monkeypatch):
    import label_on_a_cable.services.api_client as api_mod
    design_project(v)
    generate(v, "Generate Route Labels: 2 routes, 4 stops")
    close_review(v)
    monkeypatch.setattr(api_mod, "PUSH_TIMEOUT", 0.3)
    v.server.delay["LOCs/plugin-v3"] = 1.5
    state = {}

    def check(dlg):
        state["enabled"] = dlg._push_btn.isEnabled()
        verdict(v, not state["enabled"],
                "The dry run timed out, so nothing was validated, but the Push "
                "button is enabled.",
                "Push stays disabled after the preview timed out")
    push(v, "Push preview: the server never answers the dry run",
         during_preview=check, confirm=False)
    assert not state["enabled"]


# ----------------------------------------------------------------------
# 7. Standalone dual route loses its identity
# ----------------------------------------------------------------------

@title("Bug 7 · Route with no stops is deleted and re-created after its layer is re-created")
def test_video_dual_route_recreated(v):
    layers = design_project(v)
    generate(v, "Generate Route Labels: CBL-002 has no structures (a route with no stops)")
    close_review(v)
    push(v, "First push: everything is created in LOC")
    v.rec.step("Pushed. LOC IDs are saved next to the project.")
    clones = {}
    for key, lyr in layers.items():
        clone = lyr.materialize(QgsFeatureRequest())
        clone.setName(lyr.name())
        QgsProject.instance().addMapLayer(clone)
        QgsProject.instance().removeMapLayer(lyr.id())
        clones[key] = clone
    show_map(v.iface)
    v.rec.step("The layers are re-created (e.g. re-imported): same features, new layer source")
    map_categories(v, network_mappings(clones))
    generate(v, "Generate Route Labels again")
    close_review(v)
    result = {}

    def judge(dlg):
        stats = preview_stats(dlg)
        result["ok"] = ok = (stats.get("Created LOCs") == "0"
                             and stats.get("Deleted LOCs") == "0")
        verdict(v, ok,
                f"Preview: {stats.get('Created LOCs')} created, "
                f"{stats.get('Deleted LOCs')} deleted. CBL-002 lost its ID, so LOC "
                "would delete it and create a new copy, losing its history.",
                "Nothing created or deleted: CBL-002 kept its ID")
    push(v, "Push preview after re-creating the layers", during_preview=judge,
         confirm=False)
    assert result["ok"]


# ----------------------------------------------------------------------
# 8. Stale stamp cache across projects
# ----------------------------------------------------------------------

@title("Bug 8 · Saved LOC IDs from the previous project leak into the next one")
def test_video_stale_stamps_across_projects(v):
    design_project(v)
    generate(v, "Project A: generate routes")
    close_review(v)
    push(v, "Project A: push")
    a_sources = {l.source() for l in QgsProject.instance().mapLayers().values()}
    v.rec.step("Project A pushed. Its LOC IDs are saved to network_loc_stamps.json.")

    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    b_dir = v.tmp / "project_b"
    b_dir.mkdir()
    layers = build_network()
    save_project(b_dir, "project_b.qgz")
    show_map(v.iface)
    v.rec.step("Open a different project (project_b.qgz) in the same QGIS session")
    map_categories(v, network_mappings(layers))
    generate(v, "Project B: generate routes")
    close_review(v)
    push(v, "Project B: push")
    sidecar = json.loads((b_dir / "project_b_loc_stamps.json").read_text())
    keys = list(sidecar.get(LOCATION_ID, {}))
    foreign = [k for k in keys if k.rsplit(":", 1)[0] in a_sources]
    lines = [f"project_b_loc_stamps.json → {len(keys)} entries", ""]
    for k in keys:
        tag = "   <── from project A" if k in foreign else ""
        lines.append(f"  {k[:70]}{tag}")
    evidence(v, "Project B's saved-ID file", lines,
             f"{len(foreign)} of {len(keys)} entries in project B's file belong to "
             "project A's layers" if foreign else "Project B's file only has its own entries",
             not foreign)
    assert not foreign


# ----------------------------------------------------------------------
# 9. Preview label formatting (covers both _pretty_key cases)
# ----------------------------------------------------------------------

@title("Bug 9 · Push preview mislabels some server statistics")
def test_video_preview_labels(v):
    design_project(v)
    generate(v, "Generate Route Labels")
    close_review(v)
    v.server.extra_stats = {"locationCount": 1, "identifier": "rg-7"}
    result = {}

    def judge(dlg):
        result["ok"] = ok = {"Location Count", "Identifier"} <= set(preview_stats(dlg))
        verdict(v, ok,
                "The server sent 'locationCount' and 'identifier'. The preview "
                "shows them as 'LOCation Count' and 'IDentifier'.",
                "Labels read 'Location Count' and 'Identifier'")
    push(v, "Push preview with two extra statistics from the server",
         during_preview=judge, confirm=False)
    assert result["ok"]


# ----------------------------------------------------------------------
# 10. Status straight after pull
# ----------------------------------------------------------------------

@title("Bug 10 · Every pulled route shows ‘Modified’ straight after a pull")
def test_video_fresh_pull_status(v):
    pulled_project(v)
    generate(v, "Generate Route Labels without editing anything")
    dlg = review_select(v, "CBL-001")
    row = dlg._route_table.selectionModel().selectedRows()[0].row()
    status = dlg._route_table.item(row, 5).text()
    ok = status == "No changes"
    verdict(v, ok,
            f"Nothing was edited, but CBL-001 shows '{status}'. Only routes with "
            "no stops show 'No changes'.",
            "Unedited pulled routes show 'No changes'")
    assert ok
