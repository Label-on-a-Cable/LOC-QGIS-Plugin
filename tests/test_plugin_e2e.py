"""End-to-end workflows driven through the plugin's toolbar actions.

Each test loads the plugin with ``classFactory`` into a fake QGIS
interface, talks to the fake LOC server over real HTTP and drives every
dialog the way a user would (modal ``exec()`` calls are replaced by a
driver that operates the real dialog widgets).
"""

import json
from types import SimpleNamespace

import pytest
from qgis.core import QgsGeometry, QgsPointXY, QgsProject, QgsSettings
from qgis.PyQt.QtWidgets import QMessageBox, QPushButton

import label_on_a_cable
from fake_loc_server import LOCATION_ID, OTHER_LOCATION_ID
from helpers import CAT_POLE
from fake_loc_server import CAT_CHAMBER, CAT_STOPS
from helpers import (
    ITM, build_network, location_items, network_mappings, pump, save_project,
    seed_server, wait_task, wait_until,
)
from label_on_a_cable.core import export_builder as eb
from label_on_a_cable.core import route_generator as rg
from label_on_a_cable.models.route import Route
from label_on_a_cable.qt_compat import BB_OK
from label_on_a_cable.services.session import save_session
from label_on_a_cable.models.user import Organization, User
from label_on_a_cable.ui.category_mapping_dialog import CategoryMappingDialog
from label_on_a_cable.ui.login_dialog import LoginDialog
from label_on_a_cable.ui.pull_dialog import PullDialog
from label_on_a_cable.ui.push_preview_dialog import PushPreviewDialog
from label_on_a_cable.ui.user_details_dialog import UserDetailsDialog

YES, NO = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
# "Map Is Behind LOC": Yes = Pull now, Ignore = Push anyway
PUSH_ANYWAY, CANCEL = QMessageBox.StandardButton.Ignore, QMessageBox.StandardButton.Cancel

pytestmark = pytest.mark.e2e


# ----------------------------------------------------------------------
# Fixtures + user-level steps
# ----------------------------------------------------------------------

@pytest.fixture
def plugin(iface, loc_server, msgbox, opened_urls):
    p = label_on_a_cable.classFactory(iface)
    p.initGui()
    yield p
    p.unload()


@pytest.fixture
def ui(plugin, iface, loc_server, drive_modal, msgbox, opened_urls, tmp_path):
    return SimpleNamespace(plugin=plugin, iface=iface, server=loc_server,
                           drive=drive_modal, msgbox=msgbox, urls=opened_urls,
                           tmp=tmp_path, preview_stats=[])


def bar(ui, level=None):
    return ui.iface.messageBar().texts(level)


def enabled(plugin):
    return {k for k, a in plugin.actions.items() if a.isEnabled()}


def login(ui, email="eng@test.io", password="secret"):
    def driver(dlg):
        dlg._email.setText(email)
        dlg._password.setText(password)
        dlg._buttons.button(BB_OK).click()
        wait_until(lambda: dlg._task is None, message="login")
    ui.drive(LoginDialog, driver)
    ui.plugin.actions["login"].trigger()


def select_location(ui, name="Testing Location"):
    ui.plugin.actions["workspace"].trigger()
    sb = ui.plugin._sidebar
    wait_until(lambda: sb._task is None, message="locations")
    item = [it for it in location_items(sb._tree) if it.text(0) == name][0]
    sb._tree.itemClicked.emit(item, 0)


def map_categories(ui, mappings, snap=None):
    def driver(dlg):
        wait_until(lambda: dlg._task is None, message="categories")
        by_id = {m.layer_id: m for m in mappings}
        for row in dlg._layer_rows:
            if row.layer_id in by_id:
                row.restore(by_id[row.layer_id])
            else:
                row.reset()
        if snap is not None:
            dlg._snap_spin.setValue(snap)
        dlg._buttons.button(BB_OK).click()
    ui.drive(CategoryMappingDialog, driver)
    ui.plugin.actions["mapping"].trigger()


def generate(ui):
    ui.plugin.actions["generate"].trigger()
    wait_until(lambda: ui.plugin._gen_task is None, message="generation")
    pump(20)


def push(ui, before_confirm=None, confirm=True):
    """Push through the preview dialog; returns the server dry-run stats."""
    def driver(dlg):
        wait_until(lambda: dlg._task is None, message="preview")
        ui.preview_stats.append(dlg)
        if before_confirm:
            before_confirm(dlg)
        if confirm and dlg._push_btn.isEnabled():
            dlg._push_btn.click()
        else:
            dlg.reject()
    ui.drive(PushPreviewDialog, driver)
    start_push(ui)
    wait_until(lambda: ui.plugin._push_task is None, message="push")
    pump(20)


def start_push(ui):
    """Click Push and wait for the live-state check that precedes the
    prompts and the preview (1.7.0: the location is fetched first)."""
    ui.plugin.actions["push"].trigger()
    wait_until(lambda: ui.plugin._push_state_task is None, message="push state")
    pump(5)


def last_preview_stats(ui):
    """Server dry-run counts as rendered in the last preview dialog."""
    from qgis.PyQt.QtWidgets import QFormLayout
    dlg = ui.preview_stats[-1]
    out = {}
    layout = dlg._server_layout
    for i in range(layout.count()):
        sub = layout.itemAt(i).layout()
        if isinstance(sub, QFormLayout):
            for r in range(sub.rowCount()):
                k = sub.itemAt(r, QFormLayout.ItemRole.LabelRole).widget().text()
                v = sub.itemAt(r, QFormLayout.ItemRole.FieldRole).widget().text()
                out[k.rstrip(":")] = v
    return out


def pull(ui):
    def driver(dlg):
        wait_until(lambda: dlg._task is None, message="pull")
        dlg._import_btn.click()
    ui.drive(PullDialog, driver)
    ui.plugin.actions["pull"].trigger()


def design_project(ui):
    """Saved project with the standard network, logged in, location chosen,
    categories mapped — the state right before Generate."""
    layers = build_network()
    save_project(ui.tmp)
    login(ui)
    select_location(ui)
    map_categories(ui, network_mappings(layers))
    return layers


def server_ids(server):
    return server.all_loc_ids(LOCATION_ID)


# ----------------------------------------------------------------------
# Plugin lifecycle + auth
# ----------------------------------------------------------------------

class TestLifecycle:
    def test_toolbar(self, plugin, iface):
        assert list(plugin.actions) == ["login", "workspace", "mapping",
                                        "generate", "push", "pull"]
        assert enabled(plugin) == {"login"}
        assert len(iface.toolbar_icons) == 6
        for key, action in plugin.actions.items():
            assert action.objectName() == f"loc_{key}"
            assert not action.icon().isNull()
            assert action.toolTip()
        assert plugin.toolbar.objectName() == "LabelOnACableToolbar"

    def test_unload_cleans_up(self, iface, loc_server, msgbox):
        p = label_on_a_cable.classFactory(iface)
        p.initGui()
        p._on_workspace()
        p.cached_routes = [Route(line_name="x", stops=[])]
        p._show_route_review()
        assert iface.docks and p._review_dlg is not None
        p.unload()
        assert iface.toolbar_icons == [] and iface.docks == []
        assert p.actions == {} and p._sidebar is None and p._review_dlg is None

    def test_missing_icon_falls_back(self, iface, loc_server, monkeypatch):
        p = label_on_a_cable.classFactory(iface)
        monkeypatch.setattr(p, "_icon_path", lambda name: "/nonexistent.svg")
        p.initGui()
        assert all(a.icon().isNull() for a in p.actions.values())
        p.unload()

    def test_restores_saved_session(self, iface, loc_server):
        save_session("stored-token", User("u", "Ann", "a@b", "r", False,
                                          Organization("o", "Org")))
        p = label_on_a_cable.classFactory(iface)
        p.initGui()
        assert enabled(p) == set(p.actions)
        assert p.api.session.headers["Authorization"] == "Bearer stored-token"
        p.unload()

    @pytest.mark.parametrize("raw,expected", [("2.5", 2.5), ("abc", 1.0), (None, 1.0)])
    def test_snap_tolerance_from_settings(self, iface, loc_server, raw, expected):
        if raw is not None:
            QgsSettings().setValue("LOC/snap_tolerance", raw)
        p = label_on_a_cable.classFactory(iface)
        assert p.snap_tolerance == expected


class TestAuth:
    def test_login_and_sign_out(self, ui):
        login(ui)
        assert enabled(ui.plugin) == set(ui.plugin.actions)
        assert "Logged in as Test Engineer" in bar(ui, "success")
        assert QgsSettings().value("LOC/user_email") == "eng@test.io"

        def details(dlg):
            assert dlg.windowTitle() == "LOC User Details"
            [b for b in dlg.findChildren(QPushButton) if b.text() == "Sign Out"][0].click()
        ui.drive(UserDetailsDialog, details)
        ui.plugin.active_location = object()
        ui.plugin.layer_mappings = ["x"]
        ui.plugin.actions["login"].trigger()
        assert enabled(ui.plugin) == {"login"}
        assert ui.plugin.active_location is None and ui.plugin.layer_mappings == []
        assert bar(ui)[-1] == "Signed out."
        assert QgsSettings().value("LOC/auth_token") is None

    def test_failed_login_keeps_toolbar_locked(self, ui):
        login(ui, password="wrong")
        assert enabled(ui.plugin) == {"login"}
        assert not ui.plugin.auth.is_logged_in

    def test_two_factor_login(self, ui):
        def driver(dlg):
            dlg._email.setText("2fa@test.io")
            dlg._password.setText("secret")
            dlg._buttons.button(BB_OK).click()
            wait_until(lambda: dlg._task is None)
            assert dlg._otp.isVisible()
            dlg._otp.setText("123456")
            dlg._buttons.button(BB_OK).click()
            wait_until(lambda: dlg._task is None)
        ui.drive(LoginDialog, driver)
        ui.plugin.actions["login"].trigger()
        assert ui.plugin.auth.current_user.enabled_2fa

    def test_expired_token_logs_out(self, ui):
        login(ui)
        ui.server.revoke_all_tokens()
        ui.plugin.actions["workspace"].trigger()
        wait_until(lambda: ui.plugin._sidebar._task is None)
        pump(20)
        assert enabled(ui.plugin) == {"login"}
        assert "Session expired — please sign in again." in bar(ui, "warning")

    def test_sign_out_detaches_running_tasks(self, ui, tmp_path):
        layers = design_project(ui)
        ui.plugin._run_generation()
        ui.plugin._run_generation()            # already running → ignored
        ui.plugin._execute_push({"location_id": LOCATION_ID})
        ui.plugin._execute_push({})            # already running → ignored
        ui.plugin._on_workspace()              # hide sidebar
        ui.plugin.cached_routes = rg.generate_routes(ui.plugin.layer_mappings)
        ui.plugin._show_route_review()
        ui.plugin._on_sign_out()
        assert ui.plugin._gen_task is None and ui.plugin._push_task is None
        assert ui.plugin._review_dlg is None
        pump(300)
        assert ui.plugin.cached_routes == []   # late completion did not land


# ----------------------------------------------------------------------
# Workspace / location selection
# ----------------------------------------------------------------------

class TestWorkspace:
    def test_select_location(self, ui):
        login(ui)
        select_location(ui)
        assert ui.plugin.active_location.location_id == LOCATION_ID
        assert eb._active_location_id == LOCATION_ID
        assert "Active location: Testing Location" in bar(ui, "success")
        assert ui.plugin._sidebar in ui.iface.docks

    def test_toggle_sidebar(self, ui):
        login(ui)
        select_location(ui)
        sb = ui.plugin._sidebar
        n = len(ui.server.requests_to("/locations"))
        ui.plugin.actions["workspace"].trigger()       # hide
        assert not sb.isVisible()
        ui.plugin.actions["workspace"].trigger()       # show + refetch
        wait_until(lambda: sb._task is None)
        assert sb.isVisible()
        assert len(ui.server.requests_to("/locations")) == n + 1

    def test_switching_location_resets_dependent_state(self, ui):
        layers = design_project(ui)
        generate(ui)
        assert ui.plugin.layer_mappings and ui.plugin.cached_routes
        select_location(ui, "Testing Location")        # same → kept
        assert ui.plugin.layer_mappings
        select_location(ui, "Other Location")
        assert ui.plugin.active_location.location_id == OTHER_LOCATION_ID
        assert ui.plugin.layer_mappings == [] and ui.plugin.cached_routes == []
        assert ui.plugin._last_fingerprint == "" and ui.plugin._categories_cache == {}


# ----------------------------------------------------------------------
# Mapping + generation
# ----------------------------------------------------------------------

class TestMappingAndGenerate:
    def test_mapping_requires_location(self, ui):
        login(ui)
        ui.plugin.actions["mapping"].trigger()
        assert "Select a Location in the Workspace sidebar first." in bar(ui, "warning")

    def test_mapping_saved_with_tolerance(self, ui):
        layers = build_network()
        login(ui)
        select_location(ui)
        map_categories(ui, network_mappings(layers), snap=2.5)
        assert len(ui.plugin.layer_mappings) == 3
        assert ui.plugin.snap_tolerance == 2.5
        assert float(QgsSettings().value("LOC/snap_tolerance")) == 2.5
        assert set(ui.plugin._categories_cache) == {c["id"] for c in ui.server.categories}
        assert "Category mapping saved (3 layers mapped)." in bar(ui, "success")
        map_categories(ui, network_mappings(layers)[:1], snap=2.5)
        assert "Category mapping saved (1 layer mapped)." in bar(ui, "success")

    def test_generate_requires_mapping(self, ui):
        login(ui)
        ui.plugin.actions["generate"].trigger()
        assert "Configure Category Mapping first." in bar(ui, "warning")

    def test_generate_opens_review_and_caches(self, ui):
        design_project(ui)
        generate(ui)
        p = ui.plugin
        assert len(p.cached_routes) == 2
        assert p._routes_snap_tolerance == 1.0 and p._last_fingerprint
        assert ("Generated 2 route(s) with 4 stop(s). 1 route(s) matched no structures "
                "within 1 m and will use line-endpoint coordinates.") in bar(ui, "success")
        dlg = p._review_dlg
        assert dlg is not None and dlg.isVisible()
        assert dlg._route_table.rowCount() == 2

        generate(ui)  # nothing changed → cached routes re-shown, no new task
        assert "No layer changes detected — showing 2 cached route(s)." in bar(ui, "info")
        assert p._review_dlg is not dlg and not dlg.isVisible()

        p._review_dlg.close()
        pump()
        assert p._review_dlg is None

    def test_generate_reruns_after_layer_change(self, ui):
        layers = design_project(ui)
        generate(ui)
        fp = ui.plugin._last_fingerprint
        layers["poles"].dataProvider().deleteFeatures(
            [f.id() for f in layers["poles"].getFeatures() if f["PoleID"] == "P2"])
        generate(ui)
        assert ui.plugin._last_fingerprint != fp
        r1 = [r for r in ui.plugin.cached_routes if r.line_name == "CBL-001"][0]
        assert r1.active_stop_count == 3

    def test_snap_change_invalidates_cache(self, ui):
        layers = design_project(ui)
        generate(ui)
        map_categories(ui, network_mappings(layers), snap=0.4)
        assert ui.plugin._last_fingerprint == ""
        generate(ui)
        r1 = [r for r in ui.plugin.cached_routes if r.line_name == "CBL-001"][0]
        assert "Pole 2" not in [s.structure_name for s in r1.stops]
        assert "within 0.4 m" in bar(ui, "success")[-1]

    def test_manual_edits_confirmed_before_regenerating(self, ui):
        layers = design_project(ui)
        generate(ui)
        routes = ui.plugin.cached_routes
        routes[0].has_edits = True
        ui.msgbox.answers = [NO]
        generate(ui)
        assert ui.msgbox.titles("question")[-1] == "Regenerate Routes"
        assert ui.plugin.cached_routes is routes            # kept
        ui.msgbox.answers = [YES]
        generate(ui)
        assert ui.plugin.cached_routes is not routes
        assert not any(r.has_edits for r in ui.plugin.cached_routes)

    def test_generation_error_reported(self, ui, monkeypatch):
        design_project(ui)
        monkeypatch.setattr(rg, "generate_routes",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad CRS")))
        generate(ui)
        assert "Generation failed: bad CRS" in bar(ui, "critical")
        assert ui.plugin.cached_routes == []

    def test_review_needs_routes(self, ui):
        ui.plugin._show_route_review()
        assert ui.plugin._review_dlg is None
        ui.plugin._on_generation_done()   # no task → no-op
        ui.plugin._on_push_done()


# ----------------------------------------------------------------------
# Push: happy path, re-push, edits, deletes
# ----------------------------------------------------------------------

class TestPush:
    def test_design_to_push(self, ui):
        design_project(ui)
        generate(ui)
        push(ui)
        stats = last_preview_stats(ui)
        assert stats["Created LOCs"] == "12"
        assert stats["Updated LOCs"] == "0" and stats["Deleted LOCs"] == "0"
        st = ui.server.loc_state()
        assert (len(st["single"]), len(st["dual"]), len(st["multi"])) == (6, 1, 1)
        (multi,) = st["multi"].values()
        assert [s["singleLoc"]["unique_asset_id"] for s in multi["Stops"]] == [
            "CBL-001_Pole 2", "CBL-001_Chamber 1_IN",
            "CBL-001_Chamber 1_OUT", "CBL-001_Pole 3"]
        assert "Push complete: 12 created, 0 updated, 0 deleted." in bar(ui, "success")
        assert ui.urls == [f"{ui.server.base_url}/locations/{LOCATION_ID}/locs"]
        sidecar = ui.tmp / "network_loc_stamps.json"
        assert len(json.loads(sidecar.read_text())[LOCATION_ID]) == 8
        body = ui.server.requests_to("LOCs/plugin-v3", "POST")[-1]["body"]
        assert body["organization_id"] == "org-0001"
        assert all(not k.startswith("_") for k in body)
        for sl in body["single_locs"]:   # never overwrite crew-set status
            assert "origin_status" not in sl["attributes"]

    def test_repush_is_a_no_op(self, ui):
        design_project(ui)
        generate(ui)
        push(ui)
        ids = server_ids(ui.server)
        generate(ui)
        push(ui)
        assert last_preview_stats(ui) == {"Created LOCs": "0", "Updated LOCs": "0",
                                          "Deleted LOCs": "0", "Total Processed": "12"}
        assert server_ids(ui.server) == ids
        assert ui.server.push_count == 2

    def test_renamed_standalone_structure_keeps_its_record(self, ui):
        """loc-next v1.29.1 (B2, on production 9 Nov 2026) finds a
        standalone record by loc_id first, so a corrected asset id is an
        update of the same record.  The plugin used to predict the old
        rule's delete + create and showed a false "LOCs Will Be Deleted"
        (follow-up F5)."""
        layers = design_project(ui)
        generate(ui)
        push(ui)
        p5_lid = next(lid for lid, s in ui.server.loc_state()["single"].items()
                      if s["unique_asset_id"] == "P5")
        poles = layers["poles"]
        fid = [f.id() for f in poles.getFeatures() if f["PoleID"] == "P5"][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("PoleID"): "P5A"}})
        generate(ui)
        push(ui)
        assert "LOCs Will Be Deleted" not in ui.msgbox.titles()
        stats = last_preview_stats(ui)
        assert (stats["Created LOCs"], stats["Deleted LOCs"]) == ("0", "0")
        single = ui.server.loc_state()["single"]
        assert single[p5_lid]["unique_asset_id"] == "P5A"

    def test_attribute_edit_pushes_one_update(self, ui):
        layers = design_project(ui)
        generate(ui)
        push(ui)
        ids = server_ids(ui.server)
        poles = layers["poles"]
        fid = [f.id() for f in poles.getFeatures() if f["PoleID"] == "P5"][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("Height"): "12"}})
        generate(ui)
        push(ui)
        stats = last_preview_stats(ui)
        assert (stats["Created LOCs"], stats["Updated LOCs"], stats["Deleted LOCs"]) == \
            ("0", "1", "0")
        assert server_ids(ui.server) == ids
        p5 = [s for s in ui.server.loc_state()["single"].values()
              if s["unique_asset_id"] == "P5"][0]
        assert p5["fields"]["Height"] == "12"

    def test_deleting_features_deletes_on_server(self, ui):
        layers = design_project(ui)
        generate(ui)
        push(ui)
        poles = layers["poles"]
        poles.dataProvider().deleteFeatures(
            [f.id() for f in poles.getFeatures() if f["PoleID"] in ("P5", "P3")])
        generate(ui)
        push(ui)
        stats = last_preview_stats(ui)
        assert stats["Deleted LOCs"] == "3"   # P5 + P3 single + P3 route stop
        uids = {s["unique_asset_id"] for s in ui.server.loc_state()["single"].values()}
        assert uids == {"P1", "P2", "P4", "CH1"}
        # A normal project: the server's sweep did it; the check agrees.
        assert ui.server.requests_to("LOCs/delete-many", "DELETE") == []
        assert "Some LOCs Were Not Deleted" not in ui.msgbox.titles()

    def test_cancel_preview_pushes_nothing(self, ui):
        design_project(ui)
        generate(ui)
        push(ui, confirm=False)
        assert ui.server.push_count == 0 and ui.urls == []


class TestCaptureModeDeletes:
    """A project in LOC capture mode (every production project, 9 Nov
    2026) merges pushes: the server deletes nothing a push leaves out
    (loc-next M0.5).  Reported the same day: "I deleted some LOCs on my
    QGIS map and the plugin flags that they will get deleted. Yet when I
    push, they don't get deleted in LOC."  The plugin now bins what the
    user removed itself and checks afterwards; field captures it never
    held stay."""

    def _capture(self, ui):
        ui.server.capture_projects.add("p-1")

    def _uids(self, ui):
        return {s["unique_asset_id"] for s in ui.server.loc_state()["single"].values()}

    def _delete_poles(self, layers, *ids):
        poles = layers["poles"]
        poles.dataProvider().deleteFeatures(
            [f.id() for f in poles.getFeatures() if f["PoleID"] in ids])

    def _prompt(self, ui):
        return [c for c in ui.msgbox.calls if c[1] == "LOCs Will Be Deleted"]

    def test_server_alone_keeps_what_the_push_leaves_out(self, ui):
        """The bug, at the server: a merge push deletes nothing."""
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        payload = json.loads(json.dumps(ui.server.requests_to(
            "LOCs/plugin-v3", "POST")[-1]["body"]))
        payload["single_locs"] = [s for s in payload["single_locs"]
                                  if s["unique_asset_id"] != "P5"]
        ui.server._apply_push(LOCATION_ID, payload)
        assert "P5" in self._uids(ui)

    def test_deleted_structures_leave_loc(self, ui):
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._delete_poles(layers, "P5", "P3")
        generate(ui)
        push(ui)
        (prompt,) = self._prompt(ui)
        assert "You removed 3 LOC(s) in QGIS" in prompt[2]
        assert "  - P5  (single)" in prompt[2]
        assert self._uids(ui) == {"P1", "P2", "P4", "CH1"}   # P3's stop LOC too
        assert len(ui.server.bin) == 3
        assert any("3 LOC(s) moved to the recycle bin" in t for t in bar(ui))
        assert "Some LOCs Were Not Deleted" not in ui.msgbox.titles()

    def test_bulk_delete_is_filed_under_the_location_as_plugin_activity(self, ui):
        """LOC's history showed none of these deletes (12 Nov 2026): the
        request named no location and looked like the mobile app.  The
        plugin now says who it is and which location the delete is in."""
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._delete_poles(layers, "P5")
        generate(ui)
        push(ui)
        (req,) = ui.server.requests_to("LOCs/delete-many", "DELETE")
        assert req["body"]["location_id"] == ui.plugin.active_location.location_id
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers.get("x-loc-client") == "qgis-plugin"

    def test_pulled_then_deleted_leaves_loc(self, ui):
        """The reported flow: pull, delete on the map, push."""
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        from qgis.core import QgsCoordinateReferenceSystem
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, "pulled.qgz")
        pull(ui)
        pulled = [l for l in QgsProject.instance().mapLayers().values()
                  if l.fields().indexOf("_loc_id") >= 0
                  and any(f["_loc_id"] for f in l.getFeatures())]
        p5_lid = next(lid for lid, rec in ui.server.loc_state()["single"].items()
                      if rec["unique_asset_id"] == "P5")
        layer = next(l for l in pulled
                     if any(f["_loc_id"] == p5_lid for f in l.getFeatures()))
        layer.dataProvider().deleteFeatures(
            [f.id() for f in layer.getFeatures() if f["_loc_id"] == p5_lid])
        push(ui)
        assert self._prompt(ui)
        assert p5_lid not in server_ids(ui.server)
        assert "P5" not in self._uids(ui)
        assert any("moved to the recycle bin" in t for t in bar(ui))

    def test_whole_route_deleted_leaves_loc(self, ui):
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        (multi,) = ui.server.loc_state()["multi"].values()
        route_ids = {multi["dualLoc"]["loc_id"]} | {
            s["singleLoc"]["loc_id"] for s in multi["Stops"]}
        cables = layers["cables"]
        cables.dataProvider().deleteFeatures(
            [f.id() for f in cables.getFeatures() if f["AssetID"] == "CBL-001"])
        generate(ui)
        push(ui)
        assert self._prompt(ui)
        assert ui.server.loc_state()["multi"] == {}
        assert not route_ids & server_ids(ui.server)

    def test_field_captures_since_the_pull_are_kept(self, ui):
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._field_capture(ui)
        self._delete_poles(layers, "P5")
        generate(ui)
        ui.msgbox.answers = [PUSH_ANYWAY]
        push(ui)
        (prompt,) = self._prompt(ui)
        assert "  - P5  (single)" in prompt[2] and "FIELD-1" not in prompt[2]
        assert "1 other LOC(s) were captured in the field" in prompt[2]
        assert "FIELD-1" in self._uids(ui) and "P5" not in self._uids(ui)

    def test_nothing_deleted_in_qgis_deletes_nothing(self, ui):
        self._capture(ui)
        design_project(ui)
        generate(ui)
        push(ui)
        self._field_capture(ui)
        ids = server_ids(ui.server)
        ui.msgbox.answers = [PUSH_ANYWAY]
        push(ui)
        assert not self._prompt(ui)
        assert server_ids(ui.server) == ids and ui.server.bin == []

    def test_project_pulled_with_an_older_plugin(self, ui):
        """No record of what the project held: list everything left out,
        say why, delete only on Yes."""
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        cache = eb._load_stamp_cache()
        cache.pop(eb._KNOWN_KEY, None)
        eb._save_stamp_cache(cache)
        ui.plugin._pulled_loc_ids = set()
        self._delete_poles(layers, "P5")
        generate(ui)
        ui.msgbox.answers = [NO]
        start_push(ui)
        (prompt,) = self._prompt(ui)
        assert "older plugin version" in prompt[2]
        assert "P5" in self._uids(ui)                   # declined: nothing sent
        push(ui)
        assert "P5" not in self._uids(ui)

    def test_unreadable_project_switch_still_deletes(self, ui):
        """The capture switch cannot be read (no project access): the
        prompt lists what a full replacement would remove, and the task
        makes sure it is gone whatever the server did."""
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._delete_poles(layers, "P5")
        generate(ui)
        ui.server.fail_next["projects/p-1"] = (403, {"message": "Forbidden"}, {})
        push(ui)
        (prompt,) = self._prompt(ui)
        assert "A push replaces the whole location" in prompt[2]
        assert "P5" not in self._uids(ui)

    def test_failed_delete_is_reported_and_the_push_stands(self, ui):
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._delete_poles(layers, "P5")
        self._set_height(layers, "P4", "9")
        generate(ui)
        ui.server.fail_next["LOCs/delete-many"] = (500, {"message": "boom"}, {})
        push(ui)
        assert "Some LOCs Were Not Deleted" in ui.msgbox.titles()
        text = [c for c in ui.msgbox.calls if c[1] == "Some LOCs Were Not Deleted"][0][2]
        assert "1 of the 1 LOC(s) you deleted are still in LOC" in text
        assert "P5" in self._uids(ui)
        p4 = [s for s in ui.server.loc_state()["single"].values()
              if s["unique_asset_id"] == "P4"][0]
        assert p4["fields"]["Height"] == "9"            # the push itself landed

    def test_bulk_delete_refused_falls_back_to_one_by_one(self, ui):
        """delete-many is all or nothing: one record removed meanwhile
        (404) refuses the batch, so the task deletes the rest singly."""
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._delete_poles(layers, "P5", "P3")
        generate(ui)
        ui.server.fail_next["LOCs/delete-many"] = (
            404, {"message": "One or more LOCs not found"}, {})
        push(ui)
        assert len(ui.server.requests_to("LOCs/delete-many", "DELETE")) == 1
        assert len([r for r in ui.server.requests if r["method"] == "DELETE"
                    and not r["path"].endswith("delete-many")]) == 3
        assert self._uids(ui) == {"P1", "P2", "P4", "CH1"}
        assert "Some LOCs Were Not Deleted" not in ui.msgbox.titles()

    def _field_capture(self, ui, n=1, category=CAT_POLE):
        """LOC gains poles captured in the field, unknown to QGIS."""
        for i in range(1, n + 1):
            ui.server.loc_state()["single"][f"field-{i}"] = {
                "loc_id": f"field-{i}", "unique_asset_id": f"FIELD-{i}",
                "category": category, "fields": {},
                "origin_longitude": -6.2 + i / 1000, "origin_latitude": 53.3}

    def test_pull_with_an_uncategorised_record_completes(self, ui):
        """A Default-category record (category null) made the end of the
        pull crash in the change fingerprint (found 9 Nov 2026 testing
        Pull now)."""
        from qgis.core import QgsCoordinateReferenceSystem
        design_project(ui)
        generate(ui)
        push(ui)
        self._field_capture(ui, category=None)
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, "pulled.qgz")
        pull(ui)
        assert any(t.startswith("Imported") for t in bar(ui))
        assert ui.plugin._last_fingerprint

    # -- stale map: LOC holds field captures this project has never seen --

    STALE = "Map Is Behind LOC"

    def _stale(self, ui):
        return [c for c in ui.msgbox.calls if c[1] == self.STALE]

    def _pushed_and_captured(self, ui, n=1):
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._field_capture(ui, n)
        return layers

    def test_stale_map_warns_before_the_push(self, ui):
        self._pushed_and_captured(ui)
        posts = len(ui.server.requests_to("LOCs/plugin-v3", "POST"))
        previews = len(ui.preview_stats)
        ui.msgbox.answers = [CANCEL]
        start_push(ui)
        (warn,) = self._stale(ui)
        assert warn[0] == "warning"
        assert warn[2].startswith(
            "LOC has 1 asset(s) captured in the field since your last pull. "
            "Pull now adds them to your layers (your mapping and routes stay as "
            "they are), or push anyway; LOC keeps them either way.")
        assert "  - FIELD-1  (single)" in warn[2]
        assert len(ui.preview_stats) == previews            # no preview opened
        assert len(ui.server.requests_to("LOCs/plugin-v3", "POST")) == posts
        assert "FIELD-1" in self._uids(ui)

    def test_stale_map_buttons(self, ui):
        """Pull now (default), Push anyway, Cancel (Escape)."""
        from label_on_a_cable.ui import reconcile_dialog as rd
        seen = []
        ui.msgbox.on_call = None
        orig = QMessageBox.exec

        def spy(box):
            sb = QMessageBox.StandardButton
            seen.append(([box.button(b).text() if box.button(b) else None
                           for b in (sb.Yes, sb.Ignore, sb.Cancel)],
                         box.defaultButton() is box.button(sb.Yes),
                         box.escapeButton() is box.button(sb.Cancel)))
            return orig(box)
        QMessageBox.exec = spy
        try:
            ui.msgbox.answers = [YES, PUSH_ANYWAY, CANCEL]
            got = [rd.confirm_stale_map(None, [("F", "single")]) for _ in range(3)]
        finally:
            QMessageBox.exec = orig
        assert got == [rd.STALE_PULL, rd.STALE_PUSH, rd.STALE_CANCEL]
        assert seen[0] == (["Pull now", "Push anyway", "Cancel"], True, True)
        assert rd.confirm_stale_map(None, []) == rd.STALE_PUSH   # nothing new: no box
        assert len(seen) == 3
        # Only LOC's own changes: nothing to add, so no Pull now.
        ui.msgbox.answers = [PUSH_ANYWAY]
        QMessageBox.exec = spy
        try:
            assert rd.confirm_stale_map(None, [], changed_in_loc=[("P1", ["Height"])]) \
                == rd.STALE_PUSH
        finally:
            QMessageBox.exec = orig
        assert seen[3][0] == [None, "Push", "Cancel"]

    def test_stale_map_push_anyway_pushes_and_keeps_field_captures(self, ui):
        layers = self._pushed_and_captured(ui)
        self._set_height(layers, "P4", "9")
        generate(ui)
        ui.msgbox.answers = [PUSH_ANYWAY]
        push(ui)
        assert len(self._stale(ui)) == 1
        assert not self._prompt(ui)                         # nothing deleted in QGIS
        p4 = [s for s in ui.server.loc_state()["single"].values()
              if s["unique_asset_id"] == "P4"][0]
        assert p4["fields"]["Height"] == "9"
        assert "FIELD-1" in self._uids(ui) and ui.server.bin == []

    def _layer_ids(self):
        return sorted(QgsProject.instance().mapLayers())

    def _mapping_dump(self, ui):
        return [m.to_dict() for m in ui.plugin.layer_mappings]

    def _field_capture_rich(self, ui, n=3):
        """Field captures as the app stores them: named fields, exact
        coordinates."""
        self._field_capture(ui, n)
        for i in range(1, n + 1):
            rec = ui.server.loc_state()["single"][f"field-{i}"]
            rec["actual_asset_name"] = f"Field pole {i}"
            rec["fields"] = {"Height": str(10 + i), "Material": f"Wood {i}"}
            rec["origin_longitude"] = -6.4301 - i / 10000
            rec["origin_latitude"] = 53.5702

    def test_pull_now_adds_field_captures_to_the_design_layers(self, ui):
        """Brandon, 15 Nov 2026: Pull now pulled the whole location into new
        "(LOC)" layers and moved the mapping onto them; remapped, every stop
        was a duplicate and the push was refused, and the full pull had
        marked the field captures as held here, so a push from the design
        layers offered to delete them.  Pull now brings in only the three
        captures, onto the layers mapped to their category."""
        layers = self._pushed_and_captured(ui, n=0)
        self._capture(ui)
        self._field_capture_rich(ui, 3)
        before_layers, before_map = self._layer_ids(), self._mapping_dump(ui)
        posts = len(ui.server.requests_to("LOCs/plugin-v3", "POST"))
        ui.msgbox.answers = [YES]                           # Pull now
        start_push(ui)
        (warn,) = self._stale(ui)
        assert "Pull now adds them to your layers" in warn[2]
        assert len(ui.server.requests_to("LOCs/plugin-v3", "POST")) == posts
        assert self._layer_ids() == before_layers           # no "(LOC)" layers
        assert self._mapping_dump(ui) == before_map         # mapping kept
        (added,) = [c for c in ui.msgbox.calls if c[1] == "Field Captures Added"]
        assert "Added 3 asset(s)" in added[2] and "FIELD-2  (Poles)" in added[2]
        poles = {f["PoleID"]: f for f in layers["poles"].getFeatures()}
        f1 = poles["FIELD-1"]
        assert (f1["PoleName"], f1["Height"]) == ("Field pole 1", "11")
        pt = f1.geometry().asPoint()                        # ITM, not degrees
        assert 600000 < pt.x() < 800000 and 700000 < pt.y() < 800000

        state = json.loads(json.dumps(ui.server.loc_state()["single"]))
        generate(ui)
        push(ui)
        assert len(self._stale(ui)) == 1                    # no second warning
        assert not self._prompt(ui)                         # nothing to delete
        singles = ui.server.loc_state()["single"]
        assert sorted(r["unique_asset_id"] for r in singles.values()).count("FIELD-1") == 1
        assert len(singles) == len(state)                   # no duplicates
        for i in (1, 2, 3):
            got, was = singles[f"field-{i}"], state[f"field-{i}"]
            assert got["fields"] == was["fields"]
            # Photos: loc-next keeps them apart from the pushed record, so
            # a push cannot touch them; the fake server does not model that.
            assert abs(float(got["origin_longitude"]) - was["origin_longitude"]) < 1e-7
            assert abs(float(got["origin_latitude"]) - was["origin_latitude"]) < 1e-7

    def test_pull_now_then_delete_a_capture_removes_it(self, ui):
        """An added capture is held by the project like any pulled record:
        delete it on the map and the push takes it out of LOC."""
        layers = self._pushed_and_captured(ui, n=0)
        self._capture(ui)
        self._field_capture_rich(ui, 2)
        ui.msgbox.answers = [YES]
        start_push(ui)
        self._delete_poles(layers, "FIELD-1")
        generate(ui)
        push(ui)
        (prompt,) = self._prompt(ui)
        assert "  - FIELD-1  (single)" in prompt[2]
        assert "FIELD-1" not in self._uids(ui) and "FIELD-2" in self._uids(ui)

    def test_pull_now_lists_captures_it_cannot_place(self, ui):
        """A capture in a category no point layer is mapped to stays in LOC,
        is named in the report, and is still not the project's to delete."""
        self._pushed_and_captured(ui, n=0)
        self._capture(ui)
        self._field_capture(ui, 1, category=CAT_CHAMBER)
        ui.server.loc_state()["single"]["field-2"] = {
            "loc_id": "field-2", "unique_asset_id": "FIELD-2", "category": CAT_STOPS,
            "fields": {}, "origin_longitude": -6.43, "origin_latitude": 53.57}
        ui.msgbox.answers = [YES]
        start_push(ui)
        (added,) = [c for c in ui.msgbox.calls if c[1] == "Field Captures Added"]
        assert "FIELD-1  (Chambers)" in added[2]
        assert "FIELD-2: no point layer is mapped to Stops" in added[2]
        generate(ui)
        ui.msgbox.answers = [PUSH_ANYWAY]
        push(ui)
        assert not self._prompt(ui)
        assert {"FIELD-1", "FIELD-2"} <= self._uids(ui)

    def test_pull_now_adds_field_captures_to_pulled_layers(self, ui):
        """A project made by a pull: the capture joins the pulled layer with
        its pulled copy, and no second set of layers appears."""
        from qgis.core import QgsCoordinateReferenceSystem
        self._capture(ui)
        design_project(ui)
        generate(ui)
        push(ui)
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, "pulled.qgz")
        pull(ui)
        self._field_capture(ui)
        before_layers, before_map = self._layer_ids(), self._mapping_dump(ui)
        posts = len(ui.server.requests_to("LOCs/plugin-v3", "POST"))
        ui.msgbox.answers = [YES]                           # Pull now
        start_push(ui)
        assert len(self._stale(ui)) == 1
        assert len(ui.server.requests_to("LOCs/plugin-v3", "POST")) == posts
        assert self._layer_ids() == before_layers
        assert self._mapping_dump(ui) == before_map
        assert any(f["_loc_id"] == "field-1" and f["_sloc_json"]
                   for l in QgsProject.instance().mapLayers().values()
                   if l.fields().indexOf("_sloc_json") >= 0
                   for f in l.getFeatures())
        n = len(ui.server.loc_state()["single"])
        push(ui)
        assert len(self._stale(ui)) == 1                    # no second warning
        assert not self._prompt(ui)
        assert len(ui.server.loc_state()["single"]) == n
        assert "FIELD-1" in self._uids(ui)

    def test_pull_now_keeps_unpushed_edits(self, ui):
        """Pull now only adds the captures, so it is offered over edits not
        pushed yet (it used to replace the map and was withheld, Bug 33);
        the edit is still sent by the next push."""
        from qgis.core import QgsCoordinateReferenceSystem
        self._capture(ui)
        design_project(ui)
        generate(ui)
        push(ui)
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, "pulled.qgz")
        pull(ui)
        p4 = next(lid for lid, r in ui.server.loc_state()["single"].items()
                  if r["unique_asset_id"] == "P4")
        layer = next(l for l in QgsProject.instance().mapLayers().values()
                     if l.fields().indexOf("_loc_id") >= 0
                     and any(f["_loc_id"] == p4 for f in l.getFeatures()))
        h = layer.fields().indexOf("Height")
        fid = next(f.id() for f in layer.getFeatures() if f["_loc_id"] == p4)
        layer.dataProvider().changeAttributeValues({fid: {h: "77"}})   # not pushed
        self._field_capture(ui)
        ui.msgbox.answers = [YES]                           # Pull now
        start_push(ui)
        assert layer.getFeature(fid)["Height"] == "77"
        push(ui)
        assert ui.server.loc_state()["single"][p4]["fields"]["Height"] == "77"
        assert "FIELD-1" in self._uids(ui)

    def test_stale_map_cancel_stops_the_push(self, ui):
        layers = self._pushed_and_captured(ui)
        self._delete_poles(layers, "P5")
        generate(ui)
        posts = len(ui.server.requests_to("LOCs/plugin-v3", "POST"))
        ui.msgbox.answers = [CANCEL]
        start_push(ui)
        assert len(self._stale(ui)) == 1 and not self._prompt(ui)
        assert len(ui.server.requests_to("LOCs/plugin-v3", "POST")) == posts
        assert {"P5", "FIELD-1"} <= self._uids(ui)

    def test_no_stale_warning_when_the_map_is_current(self, ui):
        self._capture(ui)
        layers = design_project(ui)
        generate(ui)
        push(ui)
        self._delete_poles(layers, "P5")
        generate(ui)
        push(ui)
        assert not self._stale(ui) and self._prompt(ui)

    def test_no_stale_warning_outside_capture_mode(self, ui):
        """A normal project replaces the location: the unseen record is
        listed for deletion instead (the existing prompt)."""
        design_project(ui)
        generate(ui)
        push(ui)
        self._field_capture(ui)
        ui.msgbox.answers = [NO]
        start_push(ui)
        assert not self._stale(ui)
        (prompt,) = self._prompt(ui)
        assert "FIELD-1" in prompt[2]

    def test_no_stale_warning_without_a_record_of_the_project(self, ui):
        """Pulled with an older plugin: it cannot tell field captures from
        deletions; the older-plugin deletion prompt covers it."""
        self._pushed_and_captured(ui)
        cache = eb._load_stamp_cache()
        cache.pop(eb._KNOWN_KEY, None)
        eb._save_stamp_cache(cache)
        ui.plugin._pulled_loc_ids = set()
        ui.msgbox.answers = [NO]
        start_push(ui)
        assert not self._stale(ui)
        assert "older plugin version" in self._prompt(ui)[0][2]

    def test_stale_warning_lists_many_captures_in_details(self, ui):
        self._pushed_and_captured(ui, n=20)
        ui.msgbox.answers = [CANCEL]
        start_push(ui)
        (warn,) = self._stale(ui)
        assert "LOC has 20 asset(s) captured in the field" in warn[2]
        assert "... and 5 more (see Show Details)." in warn[2]

    def _set_height(self, layers, pole, value):
        poles = layers["poles"]
        fid = [f.id() for f in poles.getFeatures() if f["PoleID"] == pole][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("Height"): value}})


class TestMergeWithLoc:
    """A push keeps what changed in LOC since the pull (by the LOC app on
    site, or the web) and sends what changed on the map; a value changed
    on both sides is the user's call.  Brandon, 10 Nov 2026: compare the
    map and LOC before pushing, without overwriting local changes."""

    STALE, CONFLICT = "Map Is Behind LOC", "Changed in QGIS and in LOC"

    def _pulled(self, ui, capture=True):
        from qgis.core import QgsCoordinateReferenceSystem
        if capture:
            ui.server.capture_projects.add("p-1")
        design_project(ui)
        generate(ui)
        push(ui)
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, "pulled.qgz")
        pull(ui)
        ui.msgbox.calls.clear()

    def _rec(self, ui, uid):
        return next(r for r in ui.server.loc_state()["single"].values()
                    if r["unique_asset_id"] == uid)

    def _in_loc(self, ui, uid, field, value):
        """Someone changes a field in LOC (the stored record)."""
        self._rec(ui, uid)["fields"][field] = value

    def _on_map(self, ui, uid, field, value):
        lid = self._rec(ui, uid)["loc_id"]
        for l in QgsProject.instance().mapLayers().values():
            if l.fields().indexOf("_loc_id") < 0 or l.fields().indexOf(field) < 0:
                continue
            for f in l.getFeatures():
                if f["_loc_id"] == lid:
                    l.dataProvider().changeAttributeValues(
                        {f.id(): {l.fields().indexOf(field): value}})
                    return
        raise AssertionError(f"{uid} not on the map")

    def _titles(self, ui):
        return ui.msgbox.titles()

    def _pushes(self, ui):
        return len(ui.server.requests_to("LOCs/plugin-v3", "POST"))

    @pytest.mark.parametrize("capture", [True, False], ids=["capture on", "capture off"])
    def test_change_in_loc_is_kept_and_map_edit_lands(self, ui, capture):
        """Scenario (a): a field changed in LOC after the pull is not put
        back by a push from the older map, and the map's own edit lands."""
        self._pulled(ui, capture)
        self._in_loc(ui, "P4", "Height", "99")
        self._on_map(ui, "P3", "Height", "7")
        push(ui)                                    # default: Push
        warn = [c for c in ui.msgbox.calls if c[1] == self.STALE]
        assert warn and "1 asset(s) were changed in LOC" in warn[0][2]
        assert "P4  (Height)" in warn[0][2]
        assert self.CONFLICT not in self._titles(ui)
        assert self._rec(ui, "P4")["fields"]["Height"] == "99"
        assert self._rec(ui, "P3")["fields"]["Height"] == "7"
        push(ui)                                    # second push: still kept
        assert self._rec(ui, "P4")["fields"]["Height"] == "99"
        assert self._rec(ui, "P3")["fields"]["Height"] == "7"

    def test_unedited_map_keeps_loc_change(self, ui):
        """Only LOC's own edits: the push keeps them, so there is nothing
        for Pull now to add and the box offers Push and Cancel."""
        self._pulled(ui)
        self._in_loc(ui, "P4", "Height", "99")
        ui.msgbox.answers = [PUSH_ANYWAY]
        push(ui)
        (warn,) = [c for c in ui.msgbox.calls if c[1] == self.STALE]
        assert warn[2].startswith("1 asset(s) were changed in LOC since your last "
                                  "pull. The push keeps LOC's changes.")
        assert last_preview_stats(ui).get("Updated LOCs") in ("0", None)
        assert self._rec(ui, "P4")["fields"]["Height"] == "99"

    def test_different_fields_of_one_record_both_land(self, ui):
        self._pulled(ui)
        self._in_loc(ui, "P4", "Height", "99")
        self._on_map(ui, "P4", "Material", "Concrete")
        push(ui)
        assert self.CONFLICT not in self._titles(ui)
        f = self._rec(ui, "P4")["fields"]
        assert (f["Height"], f["Material"]) == ("99", "Concrete")

    def test_same_value_changed_on_both_sides_keep_mine(self, ui):
        self._pulled(ui)
        self._in_loc(ui, "P4", "Height", "99")
        self._on_map(ui, "P4", "Height", "7")
        ui.msgbox.answers = [YES]                   # Keep my QGIS values
        push(ui)
        (c,) = [c for c in ui.msgbox.calls if c[1] == self.CONFLICT]
        assert "P4 · Height: QGIS '7', LOC '99' (was '10')" in c[2]
        assert self._rec(ui, "P4")["fields"]["Height"] == "7"

    def test_same_value_changed_on_both_sides_keep_loc(self, ui):
        """Keep LOC's: LOC keeps 99, and the next push neither asks again
        nor puts the map's 7 back."""
        self._pulled(ui)
        self._in_loc(ui, "P4", "Height", "99")
        self._on_map(ui, "P4", "Height", "7")
        ui.msgbox.answers = [NO]                    # Keep LOC's values
        push(ui)
        assert self._rec(ui, "P4")["fields"]["Height"] == "99"
        ui.msgbox.calls.clear()
        push(ui)
        assert self.CONFLICT not in self._titles(ui)
        assert self._rec(ui, "P4")["fields"]["Height"] == "99"

    def test_conflict_cancel_sends_nothing(self, ui):
        self._pulled(ui)
        self._in_loc(ui, "P4", "Height", "99")
        self._on_map(ui, "P4", "Height", "7")
        n, previews = self._pushes(ui), len(ui.preview_stats)
        ui.msgbox.answers = [CANCEL]
        start_push(ui)
        assert self._pushes(ui) == n and len(ui.preview_stats) == previews
        assert self._rec(ui, "P4")["fields"]["Height"] == "99"

    def test_revert_after_a_push_is_sent(self, ui):
        """The pulled copy moves forward with each push, so putting a value
        back to what was pulled is still a change."""
        self._pulled(ui)
        self._on_map(ui, "P3", "Height", "7")
        push(ui)
        self._on_map(ui, "P3", "Height", "10")
        push(ui)
        assert self._rec(ui, "P3")["fields"]["Height"] == "10"
        assert self.CONFLICT not in self._titles(ui)

    def _route(self, ui):
        (m,) = ui.server.loc_state()["multi"].values()
        return m

    def _add_stop_in_loc(self, ui):
        """Scenario (b): someone adds a stop between the route's first two."""
        import copy as _copy
        m = self._route(ui)
        st = sorted(m["Stops"], key=lambda s: s["stopNumber"])
        new = _copy.deepcopy(st[0])
        new["stop_id"] = "stop-field-1"
        new["singleLoc"].update(loc_id="field-stop-1", origin_id="o-field-stop-1",
                                unique_asset_id="CBL-001_Field Joint",
                                actual_asset_name="CBL-001_Field Joint")
        a, b = st[0]["singleLoc"], st[1]["singleLoc"]
        new["singleLoc"]["origin_longitude"] = (a["origin_longitude"] + b["origin_longitude"]) / 2
        new["singleLoc"]["origin_latitude"] = (a["origin_latitude"] + b["origin_latitude"]) / 2
        st.insert(1, new)
        for i, s_ in enumerate(st, start=1):
            s_["stopNumber"] = i
        m["Stops"] = st
        return [s_["singleLoc"]["loc_id"] for s_ in st]

    @pytest.mark.parametrize("capture", [True, False], ids=["capture on", "capture off"])
    def test_stop_added_in_loc_stays_on_its_route(self, ui, capture):
        self._pulled(ui, capture)
        want = self._add_stop_in_loc(ui)
        self._on_map(ui, "P3", "Height", "7")
        push(ui)
        (warn,) = [c for c in ui.msgbox.calls if c[1] == self.STALE]
        assert "1 stop(s) were added to routes in LOC." in warn[2]
        assert "LOCs Will Be Deleted" not in self._titles(ui)
        order = [s_["singleLoc"]["loc_id"] for s_ in
                 sorted(self._route(ui)["Stops"], key=lambda s: s["stopNumber"])]
        assert order == want
        push(ui)                                    # and on the next push
        order = [s_["singleLoc"]["loc_id"] for s_ in
                 sorted(self._route(ui)["Stops"], key=lambda s: s["stopNumber"])]
        assert order == want
        assert "LOCs Will Be Deleted" not in self._titles(ui)

    def test_stop_removed_in_loc_is_not_put_back(self, ui):
        self._pulled(ui)
        m = self._route(ui)
        st = sorted(m["Stops"], key=lambda s: s["stopNumber"])
        gone = st[1]["singleLoc"]["loc_id"]
        m["Stops"] = [s_ for s_ in st if s_["singleLoc"]["loc_id"] != gone]
        self._on_map(ui, "P3", "Height", "7")
        push(ui)
        (warn,) = [c for c in ui.msgbox.calls if c[1] == self.STALE]
        assert "1 stop(s) were removed from routes in LOC." in warn[2]
        assert gone not in [s_["singleLoc"]["loc_id"] for s_ in self._route(ui)["Stops"]]

    def test_route_renamed_in_loc_is_kept(self, ui):
        self._pulled(ui)
        self._route(ui)["multiName"] = "CBL-001 renamed on site"
        self._on_map(ui, "P3", "Height", "7")
        push(ui)
        assert self._route(ui)["multiName"] == "CBL-001 renamed on site"

    def test_values_only_loc_changes_are_kept_and_named(self, ui):
        """Photos, flags and the like are changed in the LOC app, never in
        QGIS: a push keeps LOC's and the warning names them."""
        self._pulled(ui)
        rec = self._rec(ui, "P4")
        assert "is_flagged" in rec
        rec["is_flagged"] = True                    # flagged on site
        self._on_map(ui, "P3", "Height", "7")
        push(ui)
        (warn,) = [c for c in ui.msgbox.calls if c[1] == self.STALE]
        assert "P4  (Flag)" in warn[2]
        assert self._rec(ui, "P4")["is_flagged"] is True

    def test_kept_stop_survives_generate_and_another_push(self, ui):
        """The kept stop is now in the project's record; Generate must not
        make it read as deleted on the map."""
        self._pulled(ui)
        want = self._add_stop_in_loc(ui)
        self._on_map(ui, "P3", "Height", "7")
        push(ui)
        generate(ui)
        ui.msgbox.calls.clear()
        push(ui)
        assert "LOCs Will Be Deleted" not in self._titles(ui)
        order = [s_["singleLoc"]["loc_id"] for s_ in
                 sorted(self._route(ui)["Stops"], key=lambda s: s["stopNumber"])]
        assert order == want

    def test_push_anyway_over_edits_on_a_drawn_map(self, ui):
        """A map drawn in QGIS has no pulled copy to compare with; Push
        anyway sends its edit and keeps the field capture."""
        ui.server.capture_projects.add("p-1")
        layers = design_project(ui)
        generate(ui)
        push(ui)
        poles = layers["poles"]
        fid = next(f.id() for f in poles.getFeatures() if f["PoleID"] == "P3")
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("Height"): "7"}})
        ui.server.loc_state()["single"]["field-1"] = {
            "loc_id": "field-1", "unique_asset_id": "FIELD-1", "category": CAT_POLE,
            "fields": {}, "origin_longitude": -6.2, "origin_latitude": 53.3}
        ui.msgbox.answers = [PUSH_ANYWAY]
        push(ui)
        (warn,) = [c for c in ui.msgbox.calls if c[1] == self.STALE]
        assert "Pull now adds them to your layers" in warn[2]
        assert self._rec(ui, "P3")["fields"]["Height"] == "7"

    def test_map_without_a_pulled_copy_pushes_as_before(self, ui):
        """Drawn in QGIS, never pulled: nothing to compare with, so the map
        is sent as it is (the limit given to Brandon)."""
        ui.server.capture_projects.add("p-1")
        design_project(ui)
        generate(ui)
        push(ui)
        self._in_loc(ui, "P4", "Height", "99")
        push(ui)
        assert self._rec(ui, "P4")["fields"]["Height"] == "10"


class TestEditCycles:
    """One continuous session: push, edit, push, edit again, pull into a
    new project, push, edit, pull again, delete, push — state carries
    over from step to step, as in real use."""

    def _layer(self, name):
        return [l for l in QgsProject.instance().mapLayers().values()
                if l.name() == name][0]

    def _set(self, layer, key_field, key, field, value):
        fid = [f.id() for f in layer.getFeatures() if f[key_field] == key][0]
        layer.dataProvider().changeAttributeValues(
            {fid: {layer.fields().indexOf(field): value}})

    def _single(self, ui, uid):
        return [s for s in ui.server.loc_state()["single"].values()
                if s["unique_asset_id"] == uid][0]

    def _new_project_pull(self, ui, name):
        from qgis.core import QgsCoordinateReferenceSystem
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, name)
        pull(ui)

    def _counts(self, ui):
        s = last_preview_stats(ui)
        return s["Created LOCs"], s["Updated LOCs"], s["Deleted LOCs"]

    def test_push_edit_push_pull_edit_pull_delete_chain(self, ui):
        layers = design_project(ui)
        generate(ui)
        push(ui)                                            # 1. first push
        assert self._counts(ui) == ("12", "0", "0")
        ids = server_ids(ui.server)

        self._set(layers["poles"], "PoleID", "P5", "Height", "12")
        generate(ui)
        push(ui)                                            # 2. edit → push
        assert self._counts(ui) == ("0", "1", "0")
        assert self._single(ui, "P5")["fields"]["Height"] == "12"

        self._set(layers["cables"], "AssetID", "CBL-001", "Colour", "Green")
        generate(ui)
        push(ui)                                            # 3. edit again → push
        c, u, d = self._counts(ui)
        assert (c, d) == ("0", "0") and int(u) >= 1
        (multi,) = ui.server.loc_state()["multi"].values()
        assert multi["dualLoc"]["fields"]["Colour"] == "Green"
        assert server_ids(ui.server) == ids                 # nothing re-created

        self._new_project_pull(ui, "second.qgz")            # 4. pull into a new project
        push(ui)
        assert self._counts(ui) == ("0", "0", "0")
        assert server_ids(ui.server) == ids

        self._set(self._layer("Pole (LOC)"), "Unique Asset Identifier", "P5",
                  "Material", "Composite")
        push(ui)                                            # 5. edit after pull → push
        assert self._counts(ui) == ("0", "1", "0")
        p5 = self._single(ui, "P5")
        assert (p5["fields"]["Height"], p5["fields"]["Material"]) == ("12", "Composite")

        self._new_project_pull(ui, "third.qgz")             # 6. pull again
        push(ui)
        assert self._counts(ui) == ("0", "0", "0")
        assert server_ids(ui.server) == ids
        cables = self._layer("Fiber Cable (LOC)")
        assert [f["Colour"] for f in cables.getFeatures() if f["Route ID"] == "CBL-001"] == ["Green"]

        poles = self._layer("Pole (LOC)")
        poles.dataProvider().deleteFeatures(
            [f.id() for f in poles.getFeatures() if f["Unique Asset Identifier"] == "P5"])
        push(ui)                                            # 7. delete → push
        assert self._counts(ui) == ("0", "0", "1")
        assert server_ids(ui.server) == ids - {p5["loc_id"]}

        self._new_project_pull(ui, "fourth.qgz")            # 8. pull once more
        push(ui)
        assert self._counts(ui) == ("0", "0", "0")
        assert last_preview_stats(ui)["Total Processed"] == "11"
        assert ui.server.push_count == 8


class TestPushGuards:
    def test_unsaved_project_blocks_push_and_pull(self, ui):
        login(ui)
        ui.plugin.actions["push"].trigger()
        ui.plugin.actions["pull"].trigger()
        assert ui.msgbox.titles("critical") == ["Save the QGIS Project First"] * 2
        assert "MyProject_loc_stamps.json" in ui.msgbox.calls[0][2]

    def test_requires_location_and_mapping(self, ui):
        save_project(ui.tmp)
        login(ui)
        ui.plugin.actions["push"].trigger()
        ui.plugin.actions["pull"].trigger()
        assert bar(ui, "warning").count(
            "Select a Location in the Workspace sidebar first.") == 2
        select_location(ui)
        ui.plugin.actions["push"].trigger()
        assert "Configure Category Mapping first." in bar(ui, "warning")

    def test_missing_mapped_layer(self, ui):
        layers = design_project(ui)
        QgsProject.instance().removeMapLayer(layers["chambers"].id())
        ui.plugin.actions["push"].trigger()
        assert ("1 mapped layer(s) no longer exist in the project. "
                "Re-open Category Mapping to fix.") in bar(ui, "critical")

    def test_no_routes_prompt(self, ui):
        design_project(ui)
        ui.msgbox.answers = [NO]
        ui.plugin.actions["push"].trigger()
        assert ui.msgbox.titles("question") == ["No Routes Generated"]
        assert ui.server.push_count == 0
        ui.msgbox.answers = [YES]
        push(ui)                                  # standalone points only
        assert last_preview_stats(ui)["Created LOCs"] == "6"
        assert len(ui.server.loc_state()["single"]) == 6

    def test_not_logged_in(self, ui):
        design_project(ui)
        generate(ui)
        ui.plugin.auth.current_user = None
        ui.plugin.actions["push"].trigger()
        assert "Not logged in." in bar(ui, "warning")

    def test_disabled_layer_warning(self, ui):
        layers = build_network()
        save_project(ui.tmp)
        login(ui)
        select_location(ui)
        maps = network_mappings(layers)
        maps[2].enabled_for_export = False
        map_categories(ui, maps)
        generate(ui)
        ui.msgbox.answers = [NO]
        start_push(ui)
        title, text = ui.msgbox.calls[-1][1:]
        assert title == "Features Excluded From Push"
        assert "  - Chambers (1 feature(s)): 'Create in LOC' is switched off" in text
        assert ui.server.push_count == 0
        ui.msgbox.answers = [YES]
        push(ui)
        assert len(ui.server.loc_state()["single"]) == 5

    def test_unpushable_features_named_before_the_push(self, ui):
        """Clanton 24 Nov: cables added in the attribute table (no line on
        the map) and a cable drawn after Generate were left out of the push
        without a word; the preview just showed nothing created."""
        from helpers import X0, Y0, add_lines
        from qgis.core import QgsFeature
        layers = design_project(ui)
        generate(ui)
        cables = layers["cables"]
        row = QgsFeature(cables.fields())
        row.setAttribute("AssetID", "CBL-026")
        cables.dataProvider().addFeatures([row])
        add_lines(cables, [([(X0 + 800, Y0), (X0 + 900, Y0)], {"AssetID": "CBL-003"})])
        ui.msgbox.answers = [NO]
        start_push(ui)
        title, text = ui.msgbox.calls[-1][1:]
        assert title == "Features Excluded From Push"
        assert "Fiber Cable (1 feature(s)): no geometry: CBL-026" in text
        assert "Fiber Cable (1 feature(s)): not in the generated routes: CBL-003" in text
        assert ui.server.push_count == 0

        # Generate names the row with no geometry too, then picks up CBL-003
        generate(ui)
        assert any("1 feature(s) have no geometry" in t and "CBL-026" in t
                   for t in bar(ui, "warning"))
        ui.msgbox.answers = [NO]
        start_push(ui)
        title, text = ui.msgbox.calls[-1][1:]
        assert "no geometry: CBL-026" in text and "CBL-003" not in text

    def test_duplicate_asset_ids_warn(self, ui):
        layers = design_project(ui)
        poles = layers["poles"]
        fid = [f.id() for f in poles.getFeatures() if f["PoleID"] == "P5"][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("PoleID"): "P1"}})
        generate(ui)
        ui.msgbox.answers = [NO]
        start_push(ui)
        kind, title, text = ui.msgbox.calls[-1]
        assert (kind, title) == ("warning", "Push Warning")
        assert "'P1' is used by 2 features" in text
        assert ui.server.push_count == 0

    def test_structural_error_blocks(self, ui, monkeypatch):
        design_project(ui)
        generate(ui)
        monkeypatch.setattr(eb, "validate_payload",
                            lambda *a: [("error", "Duplicate stop loc_id x")])
        start_push(ui)
        kind, title, text = ui.msgbox.calls[-1]
        assert (kind, title) == ("critical", "Push Validation Failed")
        assert "• Duplicate stop loc_id x" in text


class TestPushFailures:
    def _fail_on_confirm(self, ui, status, body="error"):
        def inject(dlg):
            ui.server.fail_next["LOCs/plugin-v3"] = (status, body, {"X-Request-Id": "r-7"})
        design_project(ui)
        generate(ui)
        push(ui, before_confirm=inject)

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_errors_are_hard_failures(self, ui, status):
        self._fail_on_confirm(ui, status)
        kind, title, text = ui.msgbox.calls[-1]
        assert (kind, title) == ("critical", "Push Failed")
        assert f"HTTP status: {status}" in text and "Request-ID: r-7" in text
        assert not (ui.tmp / "network_loc_stamps.json").exists()
        assert ui.urls == []

    def test_server_500_may_have_committed(self, ui):
        self._fail_on_confirm(ui, 500, "validation failed but written")
        kind, title, text = ui.msgbox.calls[-1]
        assert (kind, title) == ("warning", "Push — Server Warning")
        assert "the data may have been written" in text
        assert (ui.tmp / "network_loc_stamps.json").exists()   # stamped to be safe
        assert ui.urls

    def test_client_timeout_is_hard_failure(self, ui, monkeypatch):
        import label_on_a_cable.services.api_client as api_mod

        def slow_server(dlg):
            monkeypatch.setattr(api_mod, "PUSH_TIMEOUT", 0.3)
            ui.server.delay["LOCs/plugin-v3"] = 1.5
        design_project(ui)
        generate(ui)
        push(ui, before_confirm=slow_server)
        assert ui.msgbox.calls[-1][:2] == ("critical", "Push Failed")

    def test_connection_failure(self, ui, monkeypatch):
        from label_on_a_cable.services.exceptions import NetworkException
        real = ui.plugin.api.push_locs

        def flaky(payload, preview=False):
            if preview:
                return real(payload, preview=True)
            raise NetworkException("Connection failed: refused")
        monkeypatch.setattr(ui.plugin.api, "push_locs", flaky)
        design_project(ui)
        generate(ui)
        push(ui)
        assert ui.msgbox.calls[-1][:2] == ("critical", "Push Failed")


# ----------------------------------------------------------------------
# Pull round trips
# ----------------------------------------------------------------------

class TestPull:
    def _pull_project(self, ui):
        seed_server(ui.server, ui.tmp)
        # Field crews have since updated status on the server.
        for rec in ui.server.loc_state()["single"].values():
            rec["origin_status"] = "installed"
        QgsProject.instance().clear()
        from qgis.core import QgsCoordinateReferenceSystem
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
        save_project(ui.tmp, "fieldwork.qgz")
        login(ui)
        select_location(ui)
        pull(ui)

    def test_pull_imports_layers_and_routes(self, ui):
        self._pull_project(ui)
        p = ui.plugin
        names = sorted(l.name() for l in QgsProject.instance().mapLayers().values())
        assert names == ["Chamber (LOC)", "Fiber Cable (LOC)", "Pole (LOC)"]
        assert len(p.layer_mappings) == 3 and len(p._pulled_loc_ids) == 12
        assert list(p._pulled_multi_stop_counts.values()) == [4]
        assert len(p.cached_routes) == 2 and p._has_pulled_layers()
        assert ("Imported 12 LOC(s) into 3 layer(s) — 2 route(s) ready for review."
                in bar(ui, "success"))
        assert len(p._categories_cache) == 4

    def test_pull_keeps_stop_field_mapping(self, ui):
        """Regression (ee7a374): the mapping the plugin holds after a pull
        had the stop category but no stop fields."""
        self._pull_project(ui)
        lm = next(m for m in ui.plugin.layer_mappings if m.stop_category_id)
        assert lm.stop_category_name == "Stops"
        assert lm.stop_qgis_field_for("Colour") == "Colour"
        assert lm.stop_qgis_field_for("Actual Asset Name") == ""

    def test_pull_then_push_changes_nothing(self, ui):
        self._pull_project(ui)
        before = json.dumps(ui.server.fetch_shape(LOCATION_ID), sort_keys=True)
        push(ui)
        assert last_preview_stats(ui) == {"Created LOCs": "0", "Updated LOCs": "0",
                                          "Deleted LOCs": "0", "Total Processed": "12"}
        after = ui.server.fetch_shape(LOCATION_ID)
        for s in after["singleLOCs"]:
            assert s["origin_status"] == "installed"    # server-owned, preserved
        assert {s["loc_id"] for s in after["singleLOCs"]} == \
            {s["loc_id"] for s in json.loads(before)["singleLOCs"]}

    def _layer_named(self, name):
        return [l for l in QgsProject.instance().mapLayers().values() if l.name() == name][0]

    def test_review_edits_on_a_pulled_route_push(self, ui):
        """Pull → Generate (no layer change: the pulled routes are shown)
        → Toggle Type → push was refused: "stop count changed: expected 4
        (from pull)".  Review edits change a route's stops on purpose."""
        self._pull_project(ui)
        generate(ui)
        dlg = ui.plugin._review_dlg
        route = next(r for r in dlg._routes if r.line_name == "CBL-001")
        dlg._route_table.selectRow(dlg._routes.index(route))
        ing = next(s for s in route.stops if s.stop_type.name == "INGRESS")
        dlg._stop_table.selectRow(route.stops.index(ing))
        dlg._on_toggle_type()
        push(ui)
        assert "Push Validation Failed" not in ui.msgbox.titles()
        stops = next(m for m in ui.server.loc_state()["multi"].values()
                     if m["multiName"] == "CBL-001")["Stops"]
        uids = [x["singleLoc"]["unique_asset_id"] for x in stops]
        assert "CBL-001_Chamber 1" in uids
        assert not any(u.endswith(("_IN", "_OUT")) for u in uids)

    @pytest.mark.parametrize("ends", ["on structures", "off structures"])
    def test_cable_drawn_after_pull_pushes_once(self, ui, ends):
        """A cable drawn on a pulled map, pushed, then pushed again from the
        same map: the second push must change nothing."""
        from qgis.core import QgsFeature, QgsGeometry, QgsPointXY
        self._pull_project(ui)
        cables, poles = self._layer_named("Fiber Cable (LOC)"), self._layer_named("Pole (LOC)")
        pts = {f["Unique Asset Identifier"]: f.geometry().asPoint() for f in poles.getFeatures()}
        a, b = pts["P4"], pts["P5"]
        if ends == "off structures":
            a, b = QgsPointXY(a.x() + 0.001, a.y()), QgsPointXY(b.x() + 0.001, b.y())
        f = QgsFeature(cables.fields())
        f.setAttribute("Route ID", "CBL-NEW")
        f.setGeometry(QgsGeometry.fromPolylineXY([a, b]))
        assert cables.dataProvider().addFeatures([f])[0]
        generate(ui)
        push(ui)
        assert last_preview_stats(ui)["Created LOCs"] == "1"
        push(ui)
        stats = last_preview_stats(ui)
        assert (stats["Created LOCs"], stats["Updated LOCs"]) == ("0", "0")

    def test_cable_field_edit_pushes_once(self, ui):
        """Edit a pulled cable's field, push, push again from the same map:
        the second push sent the cable's destination with its pulled
        updatedAt, and LOC counted an update on every push."""
        self._pull_project(ui)
        cables = self._layer_named("Fiber Cable (LOC)")
        fid = next(f.id() for f in cables.getFeatures() if f["Route ID"] == "CBL-001")
        cables.dataProvider().changeAttributeValues(
            {fid: {cables.fields().indexOf("Fiber Count"): "96"}})
        push(ui)
        assert last_preview_stats(ui)["Updated LOCs"] == "1"
        push(ui)
        stats = last_preview_stats(ui)
        assert (stats["Created LOCs"], stats["Updated LOCs"]) == ("0", "0")

    def _toggle(self, ui, name, stop_type):
        dlg = ui.plugin._review_dlg
        route = next(r for r in dlg._routes if r.line_name == name)
        dlg._route_table.selectRow(dlg._routes.index(route))
        stop = next(s for s in route.stops if s.stop_type.name == stop_type)
        dlg._stop_table.selectRow(route.stops.index(stop))
        dlg._on_toggle_type()

    def test_stop_added_in_review_pushes_once(self, ui):
        """Pull → Generate → Toggle Type on a passthrough (adds an OUT stop)
        → push → push again from the same map.  The second push called the
        plugin's own new stop "added in LOC" (Map Is Behind LOC) and pushed a
        second copy of it (scenario run, 24 Nov 2026)."""
        self._pull_project(ui)
        generate(ui)
        self._toggle(ui, "CBL-001", "PASSTHROUGH")
        push(ui)
        assert last_preview_stats(ui)["Created LOCs"] == "1"
        n = len(ui.msgbox.calls)
        push(ui)
        assert [c[1] for c in ui.msgbox.calls[n:]] == []
        stats = last_preview_stats(ui)
        assert (stats["Created LOCs"], stats["Updated LOCs"]) == ("0", "0")
        stops = next(m for m in ui.server.loc_state()["multi"].values()
                     if m["multiName"] == "CBL-001")["Stops"]
        uids = [x["singleLoc"]["unique_asset_id"] for x in stops]
        assert len(uids) == len(set(uids)) == 5, uids

    def test_stop_removed_in_review_stays_removed_after_generate(self, ui):
        """Pull → Generate → Remove a stop → push.  Later in the same
        session a layer edit and Generate rebuild the route from the line,
        which still bends through the removed stop's structure."""
        from qgis.PyQt.QtWidgets import QMessageBox
        self._pull_project(ui)
        generate(ui)
        dlg = ui.plugin._review_dlg
        route = next(r for r in dlg._routes if r.line_name == "CBL-001")
        dlg._route_table.selectRow(dlg._routes.index(route))
        p2 = next(s for s in route.stops if "Pole 2" in s.display_name)
        dlg._stop_table.selectRow(route.stops.index(p2))
        dlg._on_remove()
        ui.msgbox.default = QMessageBox.StandardButton.Yes
        push(ui)
        poles = self._layer_named("Pole (LOC)")
        fid = next(f.id() for f in poles.getFeatures() if f["Unique Asset Identifier"] == "P5")
        poles.dataProvider().changeAttributeValues({fid: {poles.fields().indexOf("Material"): "Steel"}})
        generate(ui)
        n = len(ui.msgbox.calls)
        push(ui)
        stats = last_preview_stats(ui)
        # The removal was this map's own: not "removed in LOC".
        assert "Map Is Behind LOC" not in [c[1] for c in ui.msgbox.calls[n:]]
        stops = next(m for m in ui.server.loc_state()["multi"].values()
                     if m["multiName"] == "CBL-001")["Stops"]
        assert not any("Pole 2" in x["singleLoc"]["unique_asset_id"] for x in stops)
        assert stats["Created LOCs"] == "0"

    def test_field_edit_after_pull(self, ui):
        self._pull_project(ui)
        poles = [l for l in QgsProject.instance().mapLayers().values()
                 if l.name() == "Pole (LOC)"][0]
        fid = [f.id() for f in poles.getFeatures()
               if f["Unique Asset Identifier"] == "P5"][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("Material"): "Composite"}})
        push(ui)
        stats = last_preview_stats(ui)
        assert (stats["Created LOCs"], stats["Updated LOCs"]) == ("0", "1")
        p5 = [s for s in ui.server.loc_state()["single"].values()
              if s["unique_asset_id"] == "P5"][0]
        assert p5["fields"]["Material"] == "Composite"

    def _move_pulled_p5(self, ui, clear_hidden_coords):
        poles = [l for l in QgsProject.instance().mapLayers().values()
                 if l.name() == "Pole (LOC)"][0]
        p5 = [f for f in poles.getFeatures() if f["Unique Asset Identifier"] == "P5"][0]
        pt = p5.geometry().asPoint()
        poles.dataProvider().changeGeometryValues(
            {p5.id(): QgsGeometry.fromPointXY(QgsPointXY(pt.x() + 0.001, pt.y()))})
        if clear_hidden_coords:
            poles.dataProvider().changeAttributeValues(
                {p5.id(): {poles.fields().indexOf("_origin_lon"): None,
                           poles.fields().indexOf("_origin_lat"): None}})
        return pt.x() + 0.001

    def test_moved_point_pushed_when_hidden_coords_cleared(self, ui):
        self._pull_project(ui)
        new_lon = self._move_pulled_p5(ui, clear_hidden_coords=True)
        push(ui)
        assert last_preview_stats(ui)["Updated LOCs"] == "1"
        moved = [s for s in ui.server.loc_state()["single"].values()
                 if s["unique_asset_id"] == "P5"][0]
        assert moved["origin_longitude"] == pytest.approx(new_lon)

    def test_moved_point_after_pull_is_pushed(self, ui):
        self._pull_project(ui)
        new_lon = self._move_pulled_p5(ui, clear_hidden_coords=False)
        push(ui)
        moved = [s for s in ui.server.loc_state()["single"].values()
                 if s["unique_asset_id"] == "P5"][0]
        assert moved["origin_longitude"] == pytest.approx(new_lon)

    def test_delete_after_pull_warns_and_deletes(self, ui):
        self._pull_project(ui)
        poles = [l for l in QgsProject.instance().mapLayers().values()
                 if l.name() == "Pole (LOC)"][0]
        poles.dataProvider().deleteFeatures(
            [f.id() for f in poles.getFeatures() if f["Unique Asset Identifier"] == "P5"])
        rendered = []
        push(ui, before_confirm=lambda dlg: rendered.extend(
            l.text() for l in dlg.findChildren(type(dlg._server_status))))
        assert "Will be deleted on server:" in rendered
        kind, title, text = ui.msgbox.calls[-1]
        assert (kind, title) == ("warning", "LOCs Will Be Deleted")
        assert "  - P5  (single)" in text
        assert last_preview_stats(ui)["Deleted LOCs"] == "1"
        assert len(ui.server.loc_state()["single"]) == 5

    def test_generate_on_pulled_layers_reconstructs(self, ui):
        self._pull_project(ui)
        generate(ui)                             # unchanged → cached
        assert "No layer changes detected — showing 2 cached route(s)." in bar(ui, "info")
        cables = [l for l in QgsProject.instance().mapLayers().values()
                  if l.name() == "Fiber Cable (LOC)"][0]
        f = next(cables.getFeatures())
        cables.dataProvider().changeAttributeValues(
            {f.id(): {cables.fields().indexOf("Colour"): "Green"}})
        ui.plugin._pulled_loc_ids = set()          # detected from layer schema instead
        generate(ui)
        assert "Reconstructed 2 route(s) with 4 stop(s)." in bar(ui, "success")
        assert ui.plugin._pulled_multi_stop_counts == {}
        push(ui)
        stats = last_preview_stats(ui)
        assert stats["Created LOCs"] == "0" and stats["Deleted LOCs"] == "0"

    def test_has_pulled_layers_ignores_truncated_ids(self, ui):
        layers = design_project(ui)
        assert not ui.plugin._has_pulled_layers()
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QMetaType
        poles = layers["poles"]
        poles.dataProvider().addAttributes([QgsField("_loc_id", QMetaType.Type.QString)])
        poles.updateFields()
        fid = next(poles.getFeatures()).id()
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("_loc_id"): "1234567890"}})  # shapefile-truncated
        assert not ui.plugin._has_pulled_layers()


# ----------------------------------------------------------------------
# Sync stamps (match an existing design to server records)
# ----------------------------------------------------------------------

class TestSync:
    def _sync(self, ui):
        def details(dlg):
            [b for b in dlg.findChildren(QPushButton) if b.text() == "Sync with LOC"][0].click()
        ui.drive(UserDetailsDialog, details)
        ui.plugin.actions["login"].trigger()
        task = getattr(ui.plugin, "_sync_task", None)
        if task is not None:
            wait_task(task)

    def test_sync_then_push_creates_nothing(self, ui):
        seed_server(ui.server, ui.tmp)           # pushed from another machine
        QgsProject.instance().clear()
        design_project(ui)                       # same design, no stamps here
        self._sync(ui)
        assert "Sync complete: matched 8 feature(s) to 8 server LOC(s)." in bar(ui, "success")
        generate(ui)
        push(ui)
        stats = last_preview_stats(ui)
        assert stats == {"Created LOCs": "0", "Updated LOCs": "0",
                         "Deleted LOCs": "0", "Total Processed": "12"}

    def test_synced_project_deletes_in_capture_mode(self, ui):
        """A project matched to LOC by Sync (not pulled) holds the matched
        records, so a capture-mode push deletes one removed here."""
        seed_server(ui.server, ui.tmp)
        ui.server.capture_projects.add("p-1")
        QgsProject.instance().clear()
        layers = design_project(ui)
        self._sync(ui)
        poles = layers["poles"]
        poles.dataProvider().deleteFeatures(
            [f.id() for f in poles.getFeatures() if f["PoleID"] == "P5"])
        generate(ui)
        push(ui)
        prompt = [c for c in ui.msgbox.calls if c[1] == "LOCs Will Be Deleted"]
        assert prompt and "older plugin version" not in prompt[-1][2]
        uids = {r["unique_asset_id"] for r in ui.server.loc_state()["single"].values()}
        assert "P5" not in uids

    def test_sync_guards(self, ui):
        login(ui)
        ui.plugin._on_sync_stamps()
        select_location(ui)
        ui.plugin._on_sync_stamps()
        warnings = bar(ui, "warning")
        assert "Select a Location in the Workspace sidebar first." in warnings
        assert "Set up category mappings first (Map Categories button)." in warnings

    def test_sync_failure(self, ui):
        design_project(ui)
        ui.server.fail_next["LOCs/all-locs-for-location"] = (500, "down", None)
        self._sync(ui)
        assert any(m.startswith("Sync failed: Server error (HTTP 500)")
                   for m in bar(ui, "critical"))

    def test_sync_without_data(self, ui):
        design_project(ui)
        ui.plugin._sync_task = SimpleNamespace(locs_data=None, error=None)
        ui.plugin._on_sync_fetched()
        ui.plugin._on_sync_failed()
        assert "Sync failed: no data returned from server." in bar(ui, "warning")
        assert "Sync failed: Unknown error" in bar(ui, "critical")


def test_open_loc_page_without_location(ui):
    ui.plugin._open_loc_page()
    assert ui.urls == [ui.server.base_url]
