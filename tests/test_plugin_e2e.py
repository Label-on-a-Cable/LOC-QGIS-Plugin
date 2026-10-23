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
from helpers import (
    ITM, build_network, network_mappings, pump, save_project, seed_server,
    wait_task, wait_until,
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
    gid = sb._tree.topLevelItem(0)
    item = [gid.child(i) for i in range(gid.childCount())
            if gid.child(i).text(0) == name][0]
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
    ui.plugin.actions["push"].trigger()
    wait_until(lambda: ui.plugin._push_task is None, message="push")
    pump(20)


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

    def test_cancel_preview_pushes_nothing(self, ui):
        design_project(ui)
        generate(ui)
        push(ui, confirm=False)
        assert ui.server.push_count == 0 and ui.urls == []


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
        ui.plugin.actions["push"].trigger()
        title, text = ui.msgbox.calls[-1][1:]
        assert title == "Disabled Layers Will Be Removed from LOC"
        assert "  - Chambers" in text
        assert ui.server.push_count == 0
        ui.msgbox.answers = [YES]
        push(ui)
        assert len(ui.server.loc_state()["single"]) == 5

    def test_duplicate_asset_ids_warn(self, ui):
        layers = design_project(ui)
        poles = layers["poles"]
        fid = [f.id() for f in poles.getFeatures() if f["PoleID"] == "P5"][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("PoleID"): "P1"}})
        generate(ui)
        ui.msgbox.answers = [NO]
        ui.plugin.actions["push"].trigger()
        kind, title, text = ui.msgbox.calls[-1]
        assert (kind, title) == ("warning", "Push Warning")
        assert "'P1' is used by 2 features" in text
        assert ui.server.push_count == 0

    def test_structural_error_blocks(self, ui, monkeypatch):
        design_project(ui)
        generate(ui)
        monkeypatch.setattr(eb, "validate_payload",
                            lambda *a: [("error", "Duplicate stop loc_id x")])
        ui.plugin.actions["push"].trigger()
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
