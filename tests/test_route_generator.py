"""Route generation engine + layer fingerprinting."""

import pytest
from qgis.core import (
    QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsVectorLayer,
)

from helpers import (
    CAT_STOPS, X0, Y0, add_lines, add_points, build_network,
    network_mappings, wait_task,
)
from label_on_a_cable.core import route_generator as rg
from label_on_a_cable.core.change_detector import compute_fingerprint
from label_on_a_cable.core.tasks import GenerateRoutesTask
from label_on_a_cable.models.mapping import FieldMapping, LayerMapping
from label_on_a_cable.models.route import StopType


def _by_name(routes):
    return {r.line_name: r for r in routes}


def _summary(route):
    return [(s.stop_type, s.stop_number, s.original_name) for s in route.stops]


EXPECTED_CBL1 = [
    (StopType.ORIGIN, 0, "Pole 1"),
    (StopType.PASSTHROUGH, 1, "CBL-001_Pole 2"),
    (StopType.INGRESS, 2, "CBL-001_Chamber 1_IN"),
    (StopType.EGRESS, 2, "CBL-001_Chamber 1_OUT"),
    (StopType.PASSTHROUGH, 3, "CBL-001_Pole 3"),
    (StopType.DESTINATION, 0, "Pole 4"),
]


class TestGenerateRoutes:
    def test_full_network(self):
        layers = build_network()
        routes = rg.generate_routes(network_mappings(layers))
        by = _by_name(routes)
        assert set(by) == {"CBL-001", "CBL-002"}

        r1 = by["CBL-001"]
        assert _summary(r1) == EXPECTED_CBL1
        assert (r1.origin, r1.destination) == ("Pole 1", "Pole 4")
        assert r1.category_name == "Fiber Cable"
        assert r1.line_layer_id == layers["cables"].id()
        assert r1.active_stop_count == 4
        chamber_stops = [s for s in r1.stops if s.structure_name == "Chamber 1"]
        assert {s.point_layer_id for s in chamber_stops} == {layers["chambers"].id()}

        r2 = by["CBL-002"]
        assert r2.is_unmatched and r2.stops == []

    def test_snap_tolerance_excludes_offset_structure(self):
        layers = build_network()
        routes = rg.generate_routes(network_mappings(layers), snap_tolerance=0.4)
        names = [s.structure_name for s in _by_name(routes)["CBL-001"].stops]
        assert "Pole 2" not in names  # 0.5 m off the line
        assert names[0] == "Pole 1"

    @pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:3857"])
    def test_project_crs_differs_from_layer_crs(self, crs):
        layers = build_network(project_crs=crs)
        routes = rg.generate_routes(network_mappings(layers))
        assert _summary(_by_name(routes)["CBL-001"]) == EXPECTED_CBL1

    def test_layer_flags_exclude_from_generation(self):
        layers = build_network()
        maps = network_mappings(layers)
        maps[2].include_in_routes = False       # chambers
        routes = rg.generate_routes(maps)
        names = [s.structure_name for s in _by_name(routes)["CBL-001"].stops]
        assert "Chamber 1" not in names

        maps = network_mappings(layers)
        maps[0].enabled_for_export = False      # cables
        assert rg.generate_routes(maps) == []

    def test_stop_category_point_layer_ignored(self):
        layers = build_network()
        maps = network_mappings(layers)
        maps[1].category_id = CAT_STOPS  # poles mapped to the stop category
        routes = rg.generate_routes(maps)
        names = [s.structure_name for s in _by_name(routes)["CBL-001"].stops]
        assert "Pole 1" not in names and "Chamber 1" in names

    def test_needs_lines_and_points(self):
        layers = build_network()
        maps = network_mappings(layers)
        assert rg.generate_routes(maps[:1]) == []
        assert rg.generate_routes(maps[1:]) == []
        assert rg.generate_routes([LayerMapping(layer_id="gone")]) == []

    def test_unnamed_fields_fall_back(self):
        """No Route ID / asset name mapped → first mapped field, then ''."""
        layers = build_network()
        maps = network_mappings(layers)
        maps[0].field_mappings = []          # line has no name field
        maps[1].field_mappings = [FieldMapping("Height", "PoleID")]
        maps[2].field_mappings = []
        routes = rg.generate_routes(maps)
        r = [r for r in routes if r.stops][0]
        assert r.line_name == ""
        labels = [s.original_name for s in r.stops]
        assert labels == ["P1", "P2", "_IN", "_OUT", "P3", "P4"]

    def test_single_candidate_is_origin_only(self):
        layers = build_network()
        lines = QgsVectorLayer("LineString?crs=EPSG:2157", "short", "memory")
        lines.dataProvider().addAttributes(layers["cables"].fields().toList())
        lines.updateFields()
        add_lines(lines, [([(X0 - 10, Y0), (X0 + 10, Y0)], {"AssetID": "S"})])
        QgsProject.instance().addMapLayer(lines)
        maps = network_mappings(layers)
        maps[0].layer_id = lines.id()
        r = rg.generate_routes(maps)[0]
        assert _summary(r) == [(StopType.ORIGIN, 0, "Pole 1")]

    def test_multipart_line_and_empty_geometries(self):
        layers = build_network()
        multi = QgsVectorLayer("MultiLineString?crs=EPSG:2157", "multi", "memory")
        multi.dataProvider().addAttributes(layers["cables"].fields().toList())
        multi.updateFields()
        f = QgsFeature(multi.fields())
        f.setGeometry(QgsGeometry.fromMultiPolylineXY([
            [QgsPointXY(X0, Y0), QgsPointXY(X0 + 100, Y0)],
            [QgsPointXY(X0 + 100, Y0), QgsPointXY(X0 + 100, Y0 + 100)],
        ]))
        f.setAttribute("AssetID", "M1")
        empty = QgsFeature(multi.fields())  # null geometry → skipped
        empty.setAttribute("AssetID", "EMPTY")
        multi.dataProvider().addFeatures([f, empty])
        QgsProject.instance().addMapLayer(multi)

        nogeom = QgsFeature(layers["poles"].fields())  # point without geometry
        layers["poles"].dataProvider().addFeatures([nogeom])

        maps = network_mappings(layers)
        maps[0].layer_id = multi.id()
        routes = rg.generate_routes(maps)
        assert [r.line_name for r in routes] == ["M1"]
        assert routes[0].active_stop_count == 4

    def test_geometry_helpers(self):
        assert rg._snap_to_line([], [], [], QgsPointXY(0, 0)) is None
        pts = [QgsPointXY(0, 0), QgsPointXY(0, 0), QgsPointXY(10, 0)]
        cum, lengths = rg._precompute_segments(pts)
        assert cum == [0.0, 0.0] and lengths == [0.0, 10.0]
        # degenerate first segment still yields the right projection
        assert rg._snap_to_line(pts, cum, lengths, QgsPointXY(0, 3)) == (3.0, 0.0)
        assert rg._snap_to_line(pts, cum, lengths, QgsPointXY(5, 1)) == (1.0, 5.0)
        assert rg._build_stops("L", []) == []

    def test_degenerate_line_returns_none(self):
        layers = build_network()
        f = QgsFeature(layers["cables"].fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(X0, Y0)]))
        assert rg._build_route_for_feature(
            f, layers["cables"], "AssetID", [], 1.0, None, {}) is None
        f.setGeometry(QgsGeometry())
        assert rg._build_route_for_feature(
            f, layers["cables"], "AssetID", [], 1.0, None, {}) is None


class TestGenerateRoutesTask:
    def test_runs_in_task_manager(self):
        layers = build_network()
        task = GenerateRoutesTask(network_mappings(layers), snap_tolerance=1.0)
        QgsApplication.taskManager().addTask(task)
        wait_task(task)
        assert task.error is None
        assert len(task.routes) == 2

    def test_error_captured(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("bad geometry")
        monkeypatch.setattr(rg, "generate_routes", boom)
        task = GenerateRoutesTask([])
        assert task.run() is False
        assert task.error == "Generation failed: bad geometry"


class TestFingerprint:
    def test_empty_and_missing(self):
        assert compute_fingerprint([]) == ""
        assert compute_fingerprint([LayerMapping(layer_id="nope")]) == ""

    def test_deterministic(self):
        maps = network_mappings(build_network())
        fp = compute_fingerprint(maps)
        assert len(fp) == 64
        assert fp == compute_fingerprint(list(reversed(maps)))

    def test_changes_on_geometry_and_mapped_attribute(self):
        layers = build_network()
        maps = network_mappings(layers)
        base = compute_fingerprint(maps)
        poles = layers["poles"]
        fid = next(poles.getFeatures()).id()

        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("PoleName"): "Renamed"}})
        moved = compute_fingerprint(maps)
        assert moved != base

        poles.dataProvider().changeGeometryValues(
            {fid: QgsGeometry.fromPointXY(QgsPointXY(X0 + 1, Y0))})
        assert compute_fingerprint(maps) != moved

    def test_unmapped_attribute_ignored(self):
        layers = build_network()
        maps = network_mappings(layers)
        maps[1].field_mappings = [FieldMapping("Unique Asset Identifier", "PoleID")]
        base = compute_fingerprint(maps)
        poles = layers["poles"]
        fid = next(poles.getFeatures()).id()
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("Material"): "Concrete"}})
        assert compute_fingerprint(maps) == base

    def test_mapping_flags_affect_fingerprint(self):
        layers = build_network()
        maps = network_mappings(layers)
        base = compute_fingerprint(maps)
        maps[2].default_stop_type = "passthrough"
        assert compute_fingerprint(maps) != base

    def test_empty_geometry_feature(self):
        layers = build_network()
        layers["poles"].dataProvider().addFeatures([QgsFeature(layers["poles"].fields())])
        assert compute_fingerprint(network_mappings(layers))
