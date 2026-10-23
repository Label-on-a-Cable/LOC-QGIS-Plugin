"""Route review dialog: tables, map highlights, stop edits, change states."""

import pytest
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject
from qgis.PyQt.QtWidgets import QCheckBox, QInputDialog

from helpers import (
    build_network, categories_by_id, location, network_mappings, pump,
    save_project,
)
from label_on_a_cable.core import export_builder as eb
from label_on_a_cable.core import import_builder as ib
from label_on_a_cable.core.route_generator import generate_routes
from label_on_a_cable.models.route import Route, Stop, StopType
from label_on_a_cable.ui import route_review_dialog as rrd
from label_on_a_cable.ui.route_review_dialog import RouteReviewDialog


@pytest.fixture
def routes():
    layers = build_network()
    return generate_routes(network_mappings(layers))


@pytest.fixture
def dlg(qtbot, iface, routes):
    d = RouteReviewDialog(routes, canvas=iface.mapCanvas(), snap_tolerance=1.0,
                          parent=iface.mainWindow())
    qtbot.addWidget(d)
    d.show()
    return d


def cell(table, row, col):
    item = table.item(row, col)
    return item.text() if item else None


def col(table, c):
    return [cell(table, r, c) for r in range(table.rowCount())]


def select_stop(d, row):
    d._stop_table.selectRow(row)
    pump()


def select_route(d, row):
    d._route_table.selectRow(row)
    pump()


class TestTablesAndSummary:
    def test_routes_table(self, dlg):
        t = dlg._route_table
        assert t.rowCount() == 2
        assert col(t, rrd._R_COL_NAME) == ["CBL-001", "CBL-002"]
        assert col(t, rrd._R_COL_CAT) == ["Fiber Cable", "Fiber Cable"]
        assert col(t, rrd._R_COL_ORIGIN) == ["Pole 1", "(line endpoint)"]
        assert col(t, rrd._R_COL_DEST) == ["Pole 4", "(line endpoint)"]
        assert col(t, rrd._R_COL_STOPS) == ["4", "0"]
        assert col(t, rrd._R_COL_STATUS) == ["New", "New"]
        assert col(t, rrd._R_COL_DELTA) == ["", ""]

    def test_summary_banner(self, dlg):
        assert dlg._summary_counts.text() == \
            "2 routes  ·  4 intermediate stops  ·  snap 1 m"
        changes = dlg._summary_changes.text()
        assert changes.startswith("2 new  ·  0 modified  ·  0 with removed stops")
        assert "1 of 2 matched structures — 1 will push with line-endpoint" in changes
        assert "Ready to push" in dlg._summary_state.text()

    def test_first_route_selected_with_stops_and_highlight(self, dlg):
        assert dlg._stops_header.text() == "Stops for CBL-001 (Pole 1 → Pole 4)"
        s = dlg._stop_table
        assert col(s, rrd._S_COL_NO) == ["", "1", "2", "2", "3", ""]
        assert col(s, rrd._S_COL_TYPE) == ["Origin", "Passthrough", "Ingress (_IN)",
                                           "Egress (_OUT)", "Passthrough", "Destination"]
        assert col(s, rrd._S_COL_STRUCT)[2] == "Chamber 1"
        assert s.cellWidget(1, rrd._S_COL_REMOVED) is not None   # intermediate
        assert s.cellWidget(0, rrd._S_COL_REMOVED) is None       # endpoint
        assert len(dlg._route_highlights) == 1

    def test_select_unmatched_route_and_clear(self, dlg):
        select_route(dlg, 1)
        assert dlg._stop_table.rowCount() == 0
        dlg._route_table.clearSelection()
        pump()
        assert dlg._stops_header.text() == "Stops"
        assert dlg._route_highlights == []

    def test_empty_dialog(self, qtbot, iface):
        d = RouteReviewDialog([], canvas=iface.mapCanvas())
        qtbot.addWidget(d)
        assert d._summary_state.text() == "No changes detected"
        assert d._summary_counts.text() == "0 routes  ·  0 intermediate stops"
        d.refresh()
        d._refresh_current()  # nothing selected → no-op
        d._on_rename(); d._on_remove(); d._on_toggle_type()

    def test_single_route_and_stop_wording(self, qtbot, iface):
        r = Route(line_name="", stops=[Stop(original_name="A", stop_type=StopType.ORIGIN),
                                       Stop(original_name="B", stop_type=StopType.PASSTHROUGH,
                                            stop_number=1)])
        d = RouteReviewDialog([r], canvas=iface.mapCanvas())
        qtbot.addWidget(d)
        assert d._summary_counts.text() == "1 route  ·  1 intermediate stop"
        assert cell(d._route_table, 0, rrd._R_COL_NAME) == r.route_id[:8]
        assert d._route_highlights == []  # no line layer to highlight


class TestStopEdits:
    def test_button_state_follows_selection(self, dlg):
        assert not dlg._btn_rename.isEnabled()
        select_stop(dlg, 0)        # origin: rename only
        assert dlg._btn_rename.isEnabled()
        assert not dlg._btn_remove.isEnabled() and not dlg._btn_toggle_type.isEnabled()
        assert len(dlg._stop_highlights) == 1
        select_stop(dlg, 1)
        assert dlg._btn_remove.isEnabled() and dlg._btn_toggle_type.isEnabled()

    def test_rename(self, dlg, routes, monkeypatch):
        answers = iter([("  Renamed Stop ", True), ("   ", True), ("x", False)])
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: next(answers)))
        select_stop(dlg, 1)
        dlg._btn_rename.click()
        assert routes[0].stops[1].display_name == "Renamed Stop"
        assert routes[0].has_edits
        assert cell(dlg._stop_table, 1, rrd._S_COL_LABEL) == "Renamed Stop"
        assert dlg._stop_table.item(1, 1).background().color() == rrd._BG_MODIFIED
        assert cell(dlg._route_table, 0, rrd._R_COL_STATUS) == "Modified"
        assert "1 modified" in dlg._summary_changes.text()
        dlg._btn_rename.click(); dlg._btn_rename.click()   # blank / cancelled
        assert routes[0].stops[1].display_name == "Renamed Stop"

    def test_remove_ingress_removes_pair_then_restore(self, dlg, routes):
        r = routes[0]
        select_stop(dlg, 2)                     # Chamber 1 _IN
        dlg._btn_remove.click()
        assert r.stops[2].removed and r.stops[3].removed
        assert dlg._btn_remove.text() == "Restore"
        assert cell(dlg._route_table, 0, rrd._R_COL_DELTA) == "−2"
        assert cell(dlg._route_table, 0, rrd._R_COL_STATUS) == "Contains removed stops"
        assert cell(dlg._route_table, 0, rrd._R_COL_STOPS) == "2"
        assert dlg._stop_table.item(2, 1).font().strikeOut()
        dlg._btn_remove.click()
        assert not r.stops[2].removed and not r.stops[3].removed
        assert dlg._btn_remove.text() == "Remove"

    def test_removed_checkbox(self, dlg, routes):
        r = routes[0]
        cb = dlg._stop_table.cellWidget(3, rrd._S_COL_REMOVED).findChild(QCheckBox)
        cb.setChecked(True)                    # egress → ingress follows
        pump(20)                               # deferred refresh
        assert r.stops[2].removed and r.stops[3].removed and r.has_edits
        assert cell(dlg._route_table, 0, rrd._R_COL_STOPS) == "2"
        cb = dlg._stop_table.cellWidget(1, rrd._S_COL_REMOVED).findChild(QCheckBox)
        cb.setChecked(True)
        pump(20)
        assert r.stops[1].removed

    def test_toggle_passthrough_to_pair_and_back(self, dlg, routes):
        r = routes[0]
        select_stop(dlg, 1)                     # Pole 2 passthrough
        dlg._btn_toggle_type.click()
        assert [s.stop_type for s in r.stops[1:3]] == [StopType.INGRESS, StopType.EGRESS]
        assert [s.display_name for s in r.stops[1:3]] == \
            ["CBL-001_Pole 2_IN", "CBL-001_Pole 2_OUT"]
        assert len(r.stops) == 7
        select_stop(dlg, 2)                     # collapse from the egress side
        dlg._btn_toggle_type.click()
        assert len(r.stops) == 6
        assert r.stops[1].stop_type == StopType.PASSTHROUGH
        assert r.stops[1].display_name == "CBL-001_Pole 2"

    def test_toggle_ingress_without_line_name(self, qtbot, iface):
        r = Route(stops=[Stop(original_name="O", stop_type=StopType.ORIGIN),
                         Stop(original_name="X_IN", structure_name="X",
                              stop_type=StopType.INGRESS, stop_number=1),
                         Stop(original_name="D", stop_type=StopType.DESTINATION)])
        d = RouteReviewDialog([r], canvas=iface.mapCanvas())
        qtbot.addWidget(d)
        d.show()
        select_stop(d, 1)
        d._btn_toggle_type.click()             # orphan ingress → passthrough
        assert r.stops[1].stop_type == StopType.PASSTHROUGH
        assert r.stops[1].display_name == "X"
        d._btn_toggle_type.click()
        assert [s.display_name for s in r.stops[1:3]] == ["X_IN", "X_OUT"]

    def test_endpoint_edits_ignored(self, dlg, routes):
        select_stop(dlg, 0)
        dlg._on_remove()
        dlg._on_toggle_type()
        assert not routes[0].stops[0].removed and len(routes[0].stops) == 6

    def test_refresh_and_close(self, dlg):
        select_route(dlg, 1)
        dlg.refresh()
        assert dlg._route_table.selectionModel().selectedRows()[0].row() == 1
        select_route(dlg, 0)
        select_stop(dlg, 1)
        assert dlg._stop_highlights
        dlg.close()
        assert dlg._closing and dlg._route_highlights == [] and dlg._stop_highlights == []
        dlg._refresh_current()   # after close → no-op


class TestRouteChangeState:
    def test_states_from_stamp_cache(self, qtbot, iface, routes, tmp_path):
        layers = {l.name(): l for l in QgsProject.instance().mapLayers().values()}
        save_project(tmp_path)
        maps = network_mappings({"cables": layers["Fiber Cable"],
                                 "poles": layers["Poles"],
                                 "chambers": layers["Chambers"]})
        payload = eb.build_payload(routes, maps, categories_by_id(), location(), "u", "o")
        eb.stamp_ids_after_push(payload)
        d = RouteReviewDialog(routes, canvas=iface.mapCanvas())
        qtbot.addWidget(d)
        assert col(d._route_table, rrd._R_COL_STATUS) == ["No changes", "No changes"]
        assert d._route_change_state(routes[0]) == "No changes"
        routes[0].stops.pop(1)                  # structural change, no edit flag
        assert d._route_change_state(routes[0]) == "Modified"

    def test_pulled_layer_route(self, qtbot, iface):
        f = ib._build_line_feature(
            {"loc_id": "a" * 36, "origin_longitude": 0, "origin_latitude": 0,
             "LOCDestination": {"longitude": 1, "latitude": 1}}, {}, [])
        lyr = ib._create_memory_layer("pulled", "LineString", list(ib._LINE_ESSENTIAL), [f])
        QgsProject.instance().addMapLayer(lyr)
        fid = next(lyr.getFeatures()).id()
        r = Route(line_layer_id=lyr.id(), line_feature_id=fid)
        d = RouteReviewDialog([r], canvas=iface.mapCanvas())
        qtbot.addWidget(d)
        assert d._route_change_state(r) == "No changes"
        assert d._route_change_state(Route(line_layer_id=lyr.id(), line_feature_id=999)) == "New"
        assert d._route_change_state(Route(line_layer_id="gone")) == "New"

    def test_fresh_pull_shows_no_changes(self, qtbot, iface, tmp_path):
        from helpers import pulled_network
        p = pulled_network(tmp_path)
        routes = ib.reconstruct_routes(p.maps, skip_snapping=True)
        d = RouteReviewDialog(routes, canvas=iface.mapCanvas())
        qtbot.addWidget(d)
        states = {r.line_name: d._route_change_state(r) for r in routes}
        assert states == {"CBL-001": "No changes", "CBL-002": "No changes"}

    def test_highlight_guards(self, dlg):
        dlg._highlight_route(Route(line_layer_id="gone"))
        dlg._highlight_stop(Stop(point_layer_id="gone"))
        dlg._highlight_stop(Stop())
        layer = [l for l in QgsProject.instance().mapLayers().values()
                 if l.name() == "Poles"][0]
        empty = QgsFeature(layer.fields())
        layer.dataProvider().addFeatures([empty])
        fid = max(f.id() for f in layer.getFeatures())
        dlg._highlight_stop(Stop(point_layer_id=layer.id(), point_feature_id=fid))
        assert dlg._stop_highlights == []


def test_stop_type_display():
    assert [rrd._stop_type_display(t) for t in StopType] == [
        "Origin", "Destination", "Passthrough", "Ingress (_IN)", "Egress (_OUT)"]
