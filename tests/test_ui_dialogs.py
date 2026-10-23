"""Login, workspace sidebar, user details, push preview and pull dialogs."""

from types import SimpleNamespace

import pytest
from qgis.PyQt.QtWidgets import QLabel, QPushButton

from fake_loc_server import LOCATION_ID
from helpers import pump, seed_server, wait_until
from label_on_a_cable.qt_compat import BB_OK, BB_CANCEL
from label_on_a_cable.services.auth_service import AuthService
from label_on_a_cable.models.location import Location
from label_on_a_cable.models.user import Organization, User
from label_on_a_cable.ui.location_sidebar import LocationSidebar
from label_on_a_cable.ui.login_dialog import LoginDialog, _LoginTask
from label_on_a_cable.ui.pull_dialog import PullDialog
from label_on_a_cable.ui.push_preview_dialog import PushPreviewDialog, _pretty_key
from label_on_a_cable.ui.user_details_dialog import UserDetailsDialog


def labels(widget):
    return [l.text() for l in widget.findChildren(QLabel)]


def button(widget, text):
    return [b for b in widget.findChildren(QPushButton) if b.text() == text][0]


def collect(signal):
    got = []
    signal.connect(lambda *args: got.append(args))
    return got


# ----------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------

class TestLoginDialog:
    @pytest.fixture
    def dlg(self, api, qtbot):
        d = LoginDialog(AuthService(api))
        qtbot.addWidget(d)
        d.show()
        return d

    def _submit(self, dlg, email, password):
        dlg._email.setText(email)
        dlg._password.setText(password)
        dlg._buttons.button(BB_OK).click()
        assert not dlg._buttons.button(BB_OK).isEnabled()   # busy while running
        wait_until(lambda: dlg._task is None, message="login task")

    def test_requires_fields(self, dlg):
        dlg._buttons.button(BB_OK).click()
        assert dlg._error_label.isVisible()
        assert dlg._error_label.text() == "Email and password are required."
        assert dlg._task is None

    def test_success(self, dlg):
        ok = collect(dlg.login_successful)
        self._submit(dlg, " eng@test.io ", "secret")
        assert ok == [()]
        assert dlg.result() == dlg.DialogCode.Accepted
        assert dlg.auth.is_logged_in

    def test_bad_password(self, dlg):
        self._submit(dlg, "eng@test.io", "nope")
        assert dlg._error_label.text() == "Invalid email or password"
        assert dlg._buttons.button(BB_OK).isEnabled()
        assert not dlg.auth.is_logged_in

    def test_two_factor_flow(self, dlg):
        ok = collect(dlg.login_successful)
        self._submit(dlg, "2fa@test.io", "secret")
        assert dlg._otp.isVisible() and dlg._otp_label.isVisible()
        assert "verification code" in dlg._error_label.text()
        assert ok == []
        dlg._otp.setText(" 123456 ")
        dlg._buttons.button(BB_OK).click()
        wait_until(lambda: dlg._task is None)
        assert ok == [()]

    def test_wrong_otp(self, dlg):
        self._submit(dlg, "2fa@test.io", "secret")
        dlg._otp.setText("999999")
        dlg._buttons.button(BB_OK).click()
        wait_until(lambda: dlg._task is None)
        assert dlg._error_label.text() == "Invalid verification code"

    def test_unexpected_error(self, dlg, monkeypatch):
        monkeypatch.setattr(dlg.auth, "login",
                            lambda *a: (_ for _ in ()).throw(KeyError("token")))
        self._submit(dlg, "eng@test.io", "secret")
        assert dlg._error_label.text().startswith("Unexpected error")

    def test_otp_prompt_without_message(self, dlg, monkeypatch):
        from label_on_a_cable.services.exceptions import OTPRequiredException

        def raise_otp(*a):
            raise OTPRequiredException("")
        monkeypatch.setattr(dlg.auth, "login", raise_otp)
        self._submit(dlg, "eng@test.io", "secret")
        assert dlg._error_label.text() == "Enter the 2FA code sent to your email."

    def test_cancel_while_running(self, dlg, loc_server):
        loc_server.delay["auth/app/login/enabled2fa"] = 0.5
        ok = collect(dlg.login_successful)
        dlg._email.setText("eng@test.io")
        dlg._password.setText("secret")
        dlg._buttons.button(BB_OK).click()
        dlg._buttons.button(BB_CANCEL).click()
        assert dlg._task is None and dlg.result() == dlg.DialogCode.Rejected
        pump(800)
        assert ok == []

    def test_cancel_idle_and_stale_callback(self, dlg):
        dlg._on_task_finished()  # no task → no-op
        dlg._buttons.button(BB_CANCEL).click()
        assert dlg.result() == dlg.DialogCode.Rejected


# ----------------------------------------------------------------------
# Workspace sidebar
# ----------------------------------------------------------------------

class TestLocationSidebar:
    @pytest.fixture
    def sidebar(self, authed_api, qtbot):
        s = LocationSidebar(authed_api)
        qtbot.addWidget(s)
        s.show()
        return s

    def _fetch(self, s):
        s.fetch_locations()
        assert not s._btn_refresh.isEnabled()
        wait_until(lambda: s._task is None, message="locations")

    def test_tree_grouped_by_gid(self, sidebar):
        self._fetch(sidebar)
        tree = sidebar._tree
        assert tree.topLevelItemCount() == 1
        gid = tree.topLevelItem(0)
        assert gid.text(0) == "Test GID (2 locations)" and gid.isExpanded()
        assert [gid.child(i).text(0) for i in range(gid.childCount())] == \
            ["Other Location", "Testing Location"]
        assert sidebar._btn_refresh.isEnabled() and not sidebar._status.isVisible()

    def test_select_location(self, sidebar):
        self._fetch(sidebar)
        got = collect(sidebar.location_selected)
        gid = sidebar._tree.topLevelItem(0)
        sidebar._tree.itemClicked.emit(gid, 0)          # group row: ignored
        assert got == []
        sidebar._tree.itemClicked.emit(gid.child(1), 0)
        (loc,), = got
        assert isinstance(loc, Location) and loc.location_id == LOCATION_ID
        assert sidebar._selection_label.text() == "Selected: Testing Location."

    def test_single_location_and_missing_gid(self, sidebar, loc_server):
        loc_server.locations = [dict(loc_server.locations[0], gid_name="")]
        self._fetch(sidebar)
        assert sidebar._tree.topLevelItem(0).text(0) == "(No GID) (1 location)"

    def test_no_locations(self, sidebar, loc_server):
        loc_server.locations = []
        self._fetch(sidebar)
        assert sidebar._status.text().startswith("No locations found")

    def test_auth_failure(self, api, qtbot):
        s = LocationSidebar(api)   # no token
        qtbot.addWidget(s)
        failed = collect(s.auth_failed)
        self._fetch(s)
        assert failed == [()]
        assert s._status.text() == "Session expired — please sign in again."

    def test_server_error(self, sidebar, loc_server):
        loc_server.fail_next["locations"] = (500, "boom", None)
        self._fetch(sidebar)
        assert "HTTP 500" in sidebar._status.text()
        assert "red" in sidebar._status.styleSheet()

    def test_refresh_while_fetching_is_noop(self, sidebar, loc_server):
        loc_server.delay["locations"] = 0.3
        sidebar.fetch_locations()
        first = sidebar._task
        sidebar._btn_refresh.setEnabled(True)
        sidebar._btn_refresh.click()
        assert sidebar._task is first
        wait_until(lambda: sidebar._task is None)
        assert len(loc_server.requests_to("/locations")) == 1

    def test_close_during_fetch(self, sidebar, loc_server):
        loc_server.delay["locations"] = 0.3
        sidebar.fetch_locations()
        sidebar.close()
        assert sidebar._task is None
        pump(500)
        assert sidebar._tree.topLevelItemCount() == 0
        sidebar._on_fetch_done()  # stale callback → no-op


# ----------------------------------------------------------------------
# User details
# ----------------------------------------------------------------------

class TestUserDetailsDialog:
    @pytest.fixture
    def dlg(self, qtbot):
        user = User("u", "Ann Engineer", "ann@test.io", "admin", False,
                    Organization("o", "KelTech IoE"))
        d = UserDetailsDialog(user)
        qtbot.addWidget(d)
        return d

    def test_profile_shown(self, dlg):
        for text in ("Ann Engineer", "ann@test.io", "admin", "KelTech IoE"):
            assert text in labels(dlg)

    def test_sync(self, dlg):
        got = collect(dlg.sync_requested)
        button(dlg, "Sync with LOC").click()
        assert got == [()] and dlg.result() == dlg.DialogCode.Accepted

    def test_sign_out(self, dlg):
        got = collect(dlg.signed_out)
        button(dlg, "Sign Out").click()
        assert got == [()]

    def test_dashboard(self, dlg, opened_urls, monkeypatch):
        monkeypatch.setenv("LOC_BASE_URL", "https://dash.example.test/")
        button(dlg, "Open Dashboard").click()
        assert opened_urls == ["https://dash.example.test"]


# ----------------------------------------------------------------------
# Push preview
# ----------------------------------------------------------------------

PAYLOAD = {"location_id": LOCATION_ID, "organization_id": "o", "multiLocs": [],
           "dual_locs": [], "single_locs": [], "_feature_map": []}
SUMMARY = {"location": "Testing Location", "routes_with_stops": 1,
           "routes_without_stops": 2, "total_routes": 3, "total_stops": 4,
           "standalone_assets": 5, "total_locs": 13}


class TestPushPreviewDialog:
    def _open(self, qtbot, api, summary=SUMMARY):
        d = PushPreviewDialog(api, PAYLOAD, summary)
        qtbot.addWidget(d)
        d.show()
        return d

    def _wait(self, d):
        wait_until(lambda: d._task is None, message="preview")

    def test_local_summary(self, qtbot, authed_api):
        d = self._open(qtbot, authed_api, dict(SUMMARY, routes_unmatched=2,
                                               will_be_deleted=7))
        texts = labels(d)
        assert "<b>Location:</b> Testing Location" in texts
        assert "<b>13</b>" in texts and "2" in texts and "7" in texts
        assert not d._push_btn.isEnabled()          # until the server answers
        self._wait(d)

    def test_server_stats_then_push(self, qtbot, authed_api, loc_server):
        d = self._open(qtbot, authed_api)
        self._wait(d)
        texts = labels(d)
        for key in ("Created LOCs:", "Updated LOCs:", "Deleted LOCs:", "Total Processed:"):
            assert key in texts
        assert not d._server_status.isVisible()
        assert d._push_btn.isEnabled()
        assert loc_server.push_count == 0           # dry run only
        got = collect(d.push_confirmed)
        d._push_btn.click()
        assert got == [(PAYLOAD,)] and d.result() == d.DialogCode.Accepted

    def test_no_stats(self, qtbot):
        api = SimpleNamespace(push_locs=lambda p, preview: {"stats": {}})
        d = self._open(qtbot, api)
        self._wait(d)
        assert "Server returned no stats." in labels(d)

    @pytest.mark.parametrize("status,blocked", [(500, True), (503, True), (400, False)])
    def test_server_errors(self, qtbot, authed_api, loc_server, status, blocked):
        loc_server.fail_next["LOCs/plugin-v3"] = (status, {"error": "nope"}, None)
        d = self._open(qtbot, authed_api)
        self._wait(d)
        text = d._server_status.text()
        assert "Check server connectivity" in text
        assert d._push_btn.isEnabled() is (not blocked)
        if blocked:
            assert "Push blocked" in d._push_btn.toolTip()

    def test_server_side_timeout_blocks_push(self, qtbot, authed_api, loc_server):
        loc_server.fail_next["LOCs/plugin-v3"] = (500, "Operation timeout", None)
        d = self._open(qtbot, authed_api)
        self._wait(d)
        assert not d._push_btn.isEnabled()

    def test_client_timeout_blocks_push(self, qtbot, authed_api, loc_server, monkeypatch):
        import label_on_a_cable.services.api_client as api_mod
        monkeypatch.setattr(api_mod, "PUSH_TIMEOUT", 0.3)
        loc_server.delay["LOCs/plugin-v3"] = 1.0
        d = self._open(qtbot, authed_api)
        self._wait(d)
        assert "timed out" in d._server_status.text()
        assert not d._push_btn.isEnabled()

    def test_cancel_while_loading(self, qtbot, authed_api, loc_server):
        loc_server.delay["LOCs/plugin-v3"] = 0.3
        d = self._open(qtbot, authed_api)
        d._buttons.button(BB_CANCEL).click()
        assert d._task is None and d.result() == d.DialogCode.Rejected
        pump(500)
        d._on_preview_loaded()  # stale → no-op

    @pytest.mark.parametrize("key,label", [
        ("createdLocs", "Created LOCs"), ("deleted_locs", "Deleted LOCs"),
        ("totalProcessed", "Total Processed"), ("someCamelKey", "Some Camel Key"),
        ("loc_count", "LOC Count"), ("api_url_id", "API URL ID"),
        ("stop-uuid", "Stop UUID"), ("my_locs_value", "My LOCs Value"),
    ])
    def test_pretty_key(self, key, label):
        assert _pretty_key(key) == label

    @pytest.mark.parametrize("key,label", [
        ("locationCount", "Location Count"), ("identifier", "Identifier"),
    ])
    def test_pretty_key_word_boundaries(self, key, label):
        assert _pretty_key(key) == label


# ----------------------------------------------------------------------
# Pull
# ----------------------------------------------------------------------

class TestPullDialog:
    def _open(self, qtbot, api, name="Testing Location"):
        d = PullDialog(api, LOCATION_ID, name)
        qtbot.addWidget(d)
        d.show()
        return d

    def _wait(self, d):
        wait_until(lambda: d._task is None, message="pull fetch")

    def test_summary_and_import(self, qtbot, authed_api, loc_server, tmp_path):
        seed_server(loc_server, tmp_path)
        d = self._open(qtbot, authed_api)
        assert not d._import_btn.isEnabled()
        self._wait(d)
        texts = labels(d)
        for row in ("Single LOCs (Pole):", "Single LOCs (Chamber):",
                    "Dual LOCs (Fiber Cable):", "Multi LOCs:", "Stop single LOCs:",
                    "<b>12</b>"):
            assert row in texts
        assert len(d._categories) == 4
        got = collect(d.import_complete)
        d._import_btn.click()
        (layers, mappings, ids, counts), = got
        assert len(layers) == 3 and len(mappings) == 3 and len(ids) == 12
        assert list(counts.values()) == [4]
        assert d.result() == d.DialogCode.Accepted

    def test_empty_location(self, qtbot, authed_api):
        d = self._open(qtbot, authed_api, name="")
        assert f"<b>Location:</b> {LOCATION_ID}" in labels(d)
        self._wait(d)
        assert "No LOC data found for this location." in labels(d)
        assert "Single LOCs:" in labels(d) and "Dual LOCs:" in labels(d)
        assert not d._import_btn.isEnabled()

    def test_fetch_error(self, qtbot, api):
        d = self._open(qtbot, api)  # unauthenticated
        self._wait(d)
        assert "Invalid or expired token" in d._status_label.text()
        assert not d._import_btn.isEnabled()

    def test_cancel_and_noop_import(self, qtbot, authed_api, loc_server):
        loc_server.delay["LOCs/all-locs-for-location"] = 0.3
        d = self._open(qtbot, authed_api)
        got = collect(d.import_complete)
        d._on_import()            # nothing fetched yet → ignored
        d._buttons.button(BB_CANCEL).click()
        assert d._task is None and d.result() == d.DialogCode.Rejected
        pump(500)
        d._on_fetch_done()
        assert got == []
