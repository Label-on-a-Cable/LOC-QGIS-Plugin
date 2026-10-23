"""Pull side: API LOC data → memory layers, mappings, route reconstruction."""

import json

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY,
    QgsProject,
)

from fake_loc_server import FakeLocServer
from helpers import (
    CAT_CHAMBER, CAT_FIBER, CAT_POLE, CAT_STOPS, ITM, LOCATION_ID, X0, Y0,
    build_network, categories, categories_by_id, location, network_mappings,
    save_project,
)
from label_on_a_cable.core import export_builder as eb
from label_on_a_cable.core import import_builder as ib
from label_on_a_cable.core import route_generator as rg
from label_on_a_cable.models.mapping import LayerMapping
from label_on_a_cable.models.route import Route, Stop, StopType


def server_state_for_network(tmp_path):
    """Push the standard network into a (not started) fake server and
    return what the server would answer to all-locs-for-location."""
    layers = build_network()
    maps = network_mappings(layers)
    save_project(tmp_path)
    payload = eb.build_payload(rg.generate_routes(maps), maps,
                               categories_by_id(), location(), "u", "o")
    srv = FakeLocServer()
    srv._apply_push(LOCATION_ID, payload)
    data = srv.fetch_shape(LOCATION_ID)
    srv.httpd.server_close()
    for lyr in layers.values():
        QgsProject.instance().removeMapLayer(lyr.id())
    return data, payload


def add_pulled(locs_data):
    layers, mappings, pulled = ib.build_layers(locs_data, categories())
    for lyr in layers:
        QgsProject.instance().addMapLayer(lyr)
    return {lyr.name(): lyr for lyr in layers}, mappings, pulled


@pytest.fixture
def pulled(tmp_path):
    data, payload = server_state_for_network(tmp_path)
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    layers, mappings, ids = add_pulled(data)
    return layers, mappings, ids, data, payload


class TestBuildLayers:
    def test_layers_and_features(self, pulled):
        layers, mappings, ids, data, payload = pulled
        assert set(layers) == {"Pole (LOC)", "Chamber (LOC)", "Fiber Cable (LOC)"}
        assert layers["Pole (LOC)"].featureCount() == 5
        assert layers["Chamber (LOC)"].featureCount() == 1
        assert layers["Fiber Cable (LOC)"].featureCount() == 2
        assert all(l.crs().authid() == "EPSG:4326" for l in layers.values())
        # every server LOC id is tracked: 6 singles + 2 duals + 4 stops
        assert len(ids) == 12
        assert ids == eb._collect_payload_loc_ids(payload)

    def test_point_attributes_round_trip(self, pulled):
        layers, *_ , data, _ = pulled
        poles = layers["Pole (LOC)"]
        names = poles.fields().names()
        assert names[:7] == ib._POINT_ESSENTIAL
        assert names[7:] == ["Height", "Material"]
        p1 = [f for f in poles.getFeatures() if f["Unique Asset Identifier"] == "P1"][0]
        src = [s for s in data["singleLOCs"] if s["unique_asset_id"] == "P1"][0]
        assert p1["Actual Asset Name"] == "Pole 1"
        assert (p1["Height"], p1["Material"]) == ("9", "Wood")
        assert p1["_loc_id"] == src["loc_id"]
        assert float(p1["_origin_lon"]) == src["origin_longitude"]
        assert json.loads(p1["_sloc_json"]) == src
        pt = p1.geometry().asPoint()
        assert (pt.x(), pt.y()) == (src["origin_longitude"], src["origin_latitude"])

    def test_line_attributes_and_geometry(self, pulled):
        layers, *_ , data, _ = pulled
        cables = layers["Fiber Cable (LOC)"]
        assert cables.fields().names()[len(ib._LINE_ESSENTIAL):] == \
            ["Length", "Fiber Count", "Colour"]
        multi = [f for f in cables.getFeatures() if f["Route ID"] == "CBL-001"][0]
        dual = [f for f in cables.getFeatures() if f["Route ID"] == "CBL-002"][0]
        mloc = data["multiLoc"][0]
        assert multi["_multi_id"] == mloc["id"]
        assert multi["_stop_ids"].split(",") == [s["stop_id"] for s in mloc["Stops"]]
        assert len(multi["_stop_loc_ids"].split("|")) == 4
        assert json.loads(multi["_mloc_json"])["id"] == mloc["id"]
        assert (multi["Fiber Count"], multi["Colour"], multi["Length"]) == ("144", "Blue", "200")
        assert multi["Destination"] == "Pole 4"
        assert len(multi.geometry().asPolyline()) == 6   # origin + 4 stops + dest
        assert dual["_multi_id"] in ("", None)
        assert len(dual.geometry().asPolyline()) == 2

    def test_internal_fields_hidden(self, pulled):
        poles = pulled[0]["Pole (LOC)"]
        for i, fld in enumerate(poles.fields()):
            widget = poles.editorWidgetSetup(i).type()
            assert (widget == "Hidden") == fld.name().startswith("_")
        # (the unnamed column is QGIS's own actions column, hidden by default)
        hidden = {c.name for c in poles.attributeTableConfig().columns()
                  if c.hidden and c.name}
        assert hidden == {n for n in poles.fields().names() if n.startswith("_")}

    def test_auto_mappings(self, pulled):
        layers, mappings, *_ = pulled
        by_name = {m.layer_name: m for m in mappings}
        pole = by_name["Pole (LOC)"]
        assert pole.category_id == CAT_POLE and pole.category_name == "Pole"
        assert pole.layer_id == layers["Pole (LOC)"].id()
        assert [fm.loc_field for fm in pole.field_mappings] == [
            "Unique Asset Identifier", "Actual Asset Name", "Height", "Material"]
        assert all(fm.loc_field == fm.qgis_field for fm in pole.field_mappings)
        line = by_name["Fiber Cable (LOC)"]
        assert (line.stop_category_id, line.stop_category_name) == (CAT_STOPS, "Stops")
        assert line.category_id == CAT_FIBER

    def test_legacy_and_variant_shapes(self):
        cats = categories()
        data = {
            "singleLOCs": [{
                "category_id": CAT_POLE, "loc_id": "s1",
                "origin_longitude": "-6.1", "origin_latitude": "53.1",
                "unique_asset_id": "U", "actual_asset_name": "N",
                "field_1": "11", "fields": {"field_2": "Steel", "Unknown": "x"},
            }, {
                "category": "unknown-cat", "loc_id": "", "origin_longitude": None,
            }],
            "dualLOCs": [{
                "category": CAT_FIBER, "loc_id": "d1",
                "origin_longitude": -6.0, "origin_latitude": 53.0,
                "locDestination": {"longitude": -6.01, "latitude": 53.01,
                                   "destination": "End",
                                   "destination_fields": {"field_1": "300"}},
                "fields": {"Colour": "Red", "Fiber Count": None},
            }, {
                "category": CAT_FIBER, "loc_id": "d2",
                "destination": "just a name",   # string, not a dict
            }],
            "multiLoc": [{
                "_id": "m1",
                "dualLOC": {"category_id": CAT_FIBER, "loc_id": "md1",
                            "origin_longitude": -6.0, "origin_latitude": 53.0,
                            "LOCDestination": {"longitude": -6.02, "latitude": 53.02}},
                "stops": [
                    {"stopNumber": 2, "stop_id": "st2",
                     "single_loc": {"loc_id": "sl2", "origin_longitude": -6.015,
                                    "origin_latitude": 53.015}},
                    {"stopNumber": 1, "stop_id": "st1",
                     "singleLOC": {"loc_id": "sl1", "category_id": CAT_STOPS,
                                   "origin_longitude": -6.01, "origin_latitude": 53.01}},
                    {"stopNumber": 3, "stop_id": "st3", "singleLoc": {"loc_id": "sl3"}},
                ],
            }, {
                "id": "m-nocat", "dualLoc": {"loc_id": "lost"}, "Stops": [],
            }],
        }
        layers, mappings, pulled = ib.build_layers(data, cats)
        assert pulled == {"s1", "d1", "d2", "md1", "sl1", "sl2", "sl3", "lost"}
        by_name = {l.name(): l for l in layers}
        assert set(by_name) == {"Pole (LOC)", "unknown-cat (LOC)", "Fiber Cable (LOC)"}

        pole = next(by_name["Pole (LOC)"].getFeatures())
        assert (pole["Height"], pole["Material"]) == ("11", "Steel")

        lines = {f["_loc_id"]: f for f in by_name["Fiber Cable (LOC)"].getFeatures()}
        assert set(lines) == {"d1", "d2", "md1"}
        d1 = lines["d1"]
        assert (d1["Destination"], d1["Length"], d1["Colour"]) == ("End", "300", "Red")
        assert d1["Fiber Count"] in (None, "")
        assert lines["d2"].geometry().asPolyline()[-1] == QgsPointXY(0, 0)
        m1 = lines["md1"]
        assert m1["_multi_id"] == "m1" and m1["_stop_ids"] == "st1,st2,st3"
        assert len(m1.geometry().asPolyline()) == 4  # sl3 has no coords
        assert m1["_stop_loc_ids"].startswith("-6.01,53.01=sl1,")
        line_map = [m for m in mappings if m.category_id == CAT_FIBER][0]
        assert line_map.stop_category_id == CAT_STOPS

    def test_non_dict_input(self):
        assert ib.build_layers(None, []) == ([], [], set())

    def test_category_field_names_skip_essentials(self):
        fmap = {"field_2": "Height", "field_1": "Route ID", "field_3": "Height"}
        assert ib._category_field_names(None, "line", fmap) == ["Height"]
        assert ib._category_field_names(None, "point", fmap) == ["Route ID", "Height"]


class TestSummaries:
    def test_import_summary(self, pulled):
        data = pulled[3]
        s = ib.import_summary(data)
        assert s["single"] == 6 and s["single_by_cat"] == {"Pole": 5, "Chamber": 1}
        assert s["dual"] == 1 and s["dual_by_cat"] == {"Fiber Cable": 1}
        assert s["multi"] == 1 and s["stop_singles"] == 4
        assert s["total"] == 12

    def test_import_summary_unknown_category_and_garbage(self):
        s = ib.import_summary({"singleLOCs": [{}], "dualLOCs": [{}]})
        assert s["single_by_cat"] == {"Unknown": 1} and s["dual_by_cat"] == {"Unknown": 1}
        assert ib.import_summary([]) == {"single": 0, "dual": 0, "multi": 0, "total": 0}

    def test_extract_multi_stop_counts(self, pulled):
        data = pulled[3]
        mid = data["multiLoc"][0]["id"]
        assert ib.extract_multi_stop_counts(data) == {mid: 4}
        # ghost stop: coordinate not present among standalone singles
        data["multiLoc"][0]["Stops"][0]["singleLoc"]["origin_longitude"] = 1.0
        data["multiLoc"].append({"Stops": []})                 # no id → skipped
        data["multiLoc"].append({"_id": "x", "stops": [
            {"singleLOC": {"origin_longitude": 0, "origin_latitude": 0}}]})
        assert ib.extract_multi_stop_counts(data) == {mid: 3, "x": 0}
        assert ib.extract_multi_stop_counts("nope") == {}


class TestReconstructRoutes:
    def test_reconstructs_pulled_routes(self, pulled):
        mappings = pulled[1]
        routes = {r.line_name: r for r in ib.reconstruct_routes(mappings, skip_snapping=True)}
        assert set(routes) == {"CBL-001", "CBL-002"}
        r1 = routes["CBL-001"]
        assert [(s.stop_type, s.stop_number, s.original_name) for s in r1.stops] == [
            (StopType.ORIGIN, 0, "Pole 1"),
            (StopType.PASSTHROUGH, 1, "CBL-001_Pole 2"),
            (StopType.INGRESS, 2, "CBL-001_Chamber 1_IN"),
            (StopType.EGRESS, 2, "CBL-001_Chamber 1_OUT"),
            (StopType.PASSTHROUGH, 3, "CBL-001_Pole 3"),
            (StopType.DESTINATION, 0, "Pole 4"),
        ]
        assert all(s.point_feature_id >= 0 for s in r1.stops)
        assert r1.category_name == "Fiber Cable"
        r2 = routes["CBL-002"]
        assert [s.stop_type for s in r2.stops] == [StopType.ORIGIN, StopType.DESTINATION]
        assert r2.origin == "CBL-002" and r2.stops[0].point_feature_id == -1

    def test_snapping_merges_new_point_and_new_line(self, pulled):
        layers, mappings, *_ = pulled
        poles, cables = layers["Pole (LOC)"], layers["Fiber Cable (LOC)"]
        multi = [f for f in cables.getFeatures() if f["Route ID"] == "CBL-001"][0]
        verts = multi.geometry().asPolyline()
        p3, p4 = verts[4], verts[5]
        mid = QgsPointXY((p3.x() + p4.x()) / 2, (p3.y() + p4.y()) / 2)
        new_pole = QgsFeature(poles.fields())
        new_pole.setGeometry(QgsGeometry.fromPointXY(mid))
        new_pole.setAttribute("Unique Asset Identifier", "P-NEW")
        new_pole.setAttribute("Actual Asset Name", "New Pole")
        poles.dataProvider().addFeatures([new_pole])

        new_line = QgsFeature(cables.fields())         # drawn in QGIS, never pushed
        new_line.setGeometry(QgsGeometry.fromPolylineXY([verts[0], verts[1], verts[2]]))
        new_line.setAttribute("Route ID", "CBL-NEW")
        cables.dataProvider().addFeatures([new_line])

        routes = {r.line_name: r for r in ib.reconstruct_routes(mappings, snap_tolerance=1.0)}
        assert set(routes) == {"CBL-001", "CBL-002", "CBL-NEW"}
        r1 = routes["CBL-001"]
        assert [s.structure_name for s in r1.stops][-2:] == ["New Pole", "Pole 4"]
        assert r1.stops[-2].stop_number == 4
        assert routes["CBL-NEW"].active_stop_count >= 1

        # default tolerance path
        assert len(ib.reconstruct_routes(mappings)) == 3

    def test_snapped_stop_inserted_in_line_order(self, pulled):
        layers, mappings, *_ = pulled
        poles, cables = layers["Pole (LOC)"], layers["Fiber Cable (LOC)"]
        verts = [f for f in cables.getFeatures()
                 if f["Route ID"] == "CBL-001"][0].geometry().asPolyline()
        mid = QgsPointXY((verts[1].x() + verts[2].x()) / 2,
                         (verts[1].y() + verts[2].y()) / 2)   # between P2 and CH1
        nf = QgsFeature(poles.fields())
        nf.setGeometry(QgsGeometry.fromPointXY(mid))
        nf.setAttribute("Actual Asset Name", "New Pole")
        poles.dataProvider().addFeatures([nf])
        r1 = [r for r in ib.reconstruct_routes(mappings, snap_tolerance=1.0)
              if r.line_name == "CBL-001"][0]
        names = [s.structure_name for s in r1.stops]
        assert names.index("New Pole") == 2

    def test_duplicate_vertex_runs(self):
        """2 vertices → IN/OUT, 3 → IN, _2 passthrough, OUT; ghost skipped."""
        from label_on_a_cable.core.import_builder import _create_memory_layer, _build_line_feature
        pts = ib._create_memory_layer("pts", "Point", list(ib._POINT_ESSENTIAL), [])
        feats = []
        for x, name in ((0, "A"), (1, "B"), (2, "C"), (4, "D")):
            f = QgsFeature(pts.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, 0)))
            f.setAttribute("Actual Asset Name", name)
            feats.append(f)
        pts.dataProvider().addFeatures(feats)
        dloc = {"loc_id": "L", "origin_longitude": 0, "origin_latitude": 0,
                "unique_asset_id": "R",
                "LOCDestination": {"longitude": 4, "latitude": 0}}
        line_feat = _build_line_feature(
            dloc, {}, [], extra_coords=[(1, 0), (1, 0), (2, 0), (2, 0), (2, 0), (3, 0)],
            multi_id="M")
        lines = _create_memory_layer("lines", "LineString", list(ib._LINE_ESSENTIAL), [line_feat])
        for l in (pts, lines):
            QgsProject.instance().addMapLayer(l)
        maps = [
            LayerMapping(layer_id=lines.id(), category_id="dual",
                         field_mappings=ib._auto_mapping_line(
                             lines, "dual", "D", ["Route ID"]).field_mappings),
            LayerMapping(layer_id=pts.id(), category_id="single",
                         field_mappings=ib._auto_mapping_point(
                             pts, "single", "S", ["Actual Asset Name"]).field_mappings),
        ]
        (route,) = ib.reconstruct_routes(maps, skip_snapping=True)
        assert [(s.stop_type, s.stop_number, s.original_name) for s in route.stops] == [
            (StopType.ORIGIN, 0, "A"),
            (StopType.INGRESS, 1, "R_B_IN"),
            (StopType.EGRESS, 1, "R_B_OUT"),
            (StopType.INGRESS, 2, "R_C_IN"),
            (StopType.PASSTHROUGH, 2, "R_C_2"),
            (StopType.EGRESS, 2, "R_C_OUT"),
            (StopType.DESTINATION, 0, "D"),
        ]

    def test_filters(self, pulled):
        mappings = pulled[1]
        for m in mappings:
            if m.category_id == CAT_FIBER:
                m.include_in_routes = False
        assert ib.reconstruct_routes(mappings) == []
        assert ib.reconstruct_routes([LayerMapping(layer_id="missing")]) == []

    def test_non_pulled_lines_skipped_without_snapping(self, pulled):
        layers, mappings, *_ = pulled
        cables = layers["Fiber Cable (LOC)"]
        f = QgsFeature(cables.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(1, 1)]))
        empty = QgsFeature(cables.fields())
        single_vertex = QgsFeature(cables.fields())
        single_vertex.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(0, 0)]))
        single_vertex.setAttribute("_loc_id", "x" * 36)
        cables.dataProvider().addFeatures([f, empty, single_vertex])
        assert len(ib.reconstruct_routes(mappings, skip_snapping=True)) == 2


class TestImportHelpers:
    def test_find_point(self):
        idx = {(0, 0): [("L1", 1, "a", "stops"), ("L2", 2, "b", "pole")]}
        assert ib._find_point(idx, (9, 9)) is None
        assert ib._find_point(idx, (0, 0)) == ("L1", 1, "a")
        assert ib._find_point(idx, (0, 0), exclude_cat="stops") == ("L2", 2, "b")
        assert ib._find_point(idx, (0, 0), prefer_cat="pole") == ("L2", 2, "b")
        assert ib._find_point(idx, (0, 0), prefer_cat="none") == ("L1", 1, "a")
        only = {(0, 0): [("L1", 1, "a", "stops")]}
        assert ib._find_point(only, (0, 0), exclude_cat="stops") == ("L1", 1, "a")

    def test_merge_stops(self):
        recon = Route(stops=[
            Stop(stop_type=StopType.ORIGIN, point_layer_id="L", point_feature_id=1),
            Stop(stop_type=StopType.PASSTHROUGH, stop_number=1,
                 point_layer_id="L", point_feature_id=2),
            Stop(stop_type=StopType.DESTINATION, point_layer_id="L", point_feature_id=3),
        ])
        snap = Route(stops=[
            Stop(stop_type=StopType.PASSTHROUGH, point_layer_id="L", point_feature_id=2),
            Stop(original_name="new", stop_type=StopType.PASSTHROUGH,
                 point_layer_id="L", point_feature_id=7),
            Stop(stop_type=StopType.PASSTHROUGH, point_feature_id=-1),
            Stop(stop_type=StopType.DESTINATION, point_layer_id="L", point_feature_id=9),
        ])
        assert ib._merge_stops(recon, snap) == 1
        assert [s.point_feature_id for s in recon.stops] == [1, 2, 7, 3]
        assert recon.stops[2].stop_number == 2
        assert ib._merge_stops(recon, Route()) == 0
        no_dest = Route(stops=[Stop(stop_type=StopType.ORIGIN)])
        ib._merge_stops(no_dest, Route(stops=[Stop(point_layer_id="L", point_feature_id=5)]))
        assert no_dest.stops[-1].point_feature_id == 5

    def test_value_helpers(self):
        f = QgsFeature(ib._make_qgs_fields(["a", "b"]))
        f.setAttributes(["NULL", "v"])
        assert ib._safe_feat_attr(f, "") == ""
        assert ib._safe_feat_attr(f, "a") == ""
        assert ib._safe_feat_attr(f, "b") == "v"
        assert ib._safe_feat_attr(f, "missing") == ""
        assert [ib._clean_val(v) for v in (None, "NULL", "N/A", "None", 5)] == ["", "", "", "", "5"]
        assert ib._float("x") == 0.0 and ib._float("2") == 2.0

    def test_field_name_maps(self):
        maps = ib._build_field_name_maps(categories())
        assert maps[CAT_POLE] == {"field_1": "Height", "field_2": "Material"}
        assert maps[CAT_FIBER] == {"field_1": "Fiber Count", "field_2": "Colour",
                                   "destination_field_1": "Length"}
