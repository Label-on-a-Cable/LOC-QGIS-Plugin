"""Push payload builder: structure, identity, stamp cache, invariants."""

import json
import time
from types import SimpleNamespace

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
    QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject,
)

from helpers import (
    CAT_CHAMBER, CAT_FIBER, CAT_POLE, CAT_STOPS, ITM, LOCATION_ID, X0, Y0,
    build_network, categories_by_id, feature_where, location,
    network_mappings, pulled_network, save_project,
)
from label_on_a_cable.core import export_builder as eb
from label_on_a_cable.core import import_builder as ib
from label_on_a_cable.core import route_generator as rg
from label_on_a_cable.models.category import CategoryField
from label_on_a_cable.models.mapping import FieldMapping, LayerMapping
from label_on_a_cable.models.route import Route, Stop, StopType

OMITTED_KEYS = {"origin_status", "notes", "transaction_id", "origin_radius"}


def wgs_raw(x, y):
    xf = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(ITM),
        QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    p = xf.transform(QgsPointXY(x, y))
    return (p.x(), p.y())


def wgs(x, y):
    return pytest.approx(wgs_raw(x, y), abs=1e-9)


@pytest.fixture
def net(tmp_path):
    layers = build_network()
    maps = network_mappings(layers)
    save_project(tmp_path)
    return SimpleNamespace(
        layers=layers, maps=maps, routes=rg.generate_routes(maps),
        cats=categories_by_id(), loc=location(), tmp=tmp_path,
    )


def build(net, **kw):
    return eb.build_payload(net.routes, net.maps, net.cats, net.loc,
                            "u-0001", "org-0001", **kw)


def all_ids(payload):
    """Every server identity in a payload, in a stable order."""
    out = []
    for ml in payload["multiLocs"]:
        dl = ml["dualLoc"]
        out += [ml["id"], dl["loc_id"], dl["origin_id"],
                dl["LOCDestination"]["destination_id"]]
        for s in ml["Stops"]:
            out += [s["stop_id"], s["singleLoc"]["loc_id"], s["singleLoc"]["origin_id"]]
    for dl in payload["dual_locs"]:
        out += [dl["loc_id"], dl["origin_id"], dl["LOCDestination"]["destination_id"]]
    for sl in payload["single_locs"]:
        out += [sl["id"], sl["attributes"]["loc_id"], sl["attributes"]["origin_id"]]
    return out


def timestamps(payload):
    out = {}
    for ml in payload["multiLocs"]:
        out[ml["dualLoc"]["unique_asset_id"]] = ml["dualLoc"]["updatedAt"]
        for s in ml["Stops"]:
            out[s["singleLoc"]["unique_asset_id"]] = s["singleLoc"]["updatedAt"]
    for dl in payload["dual_locs"]:
        out[dl["unique_asset_id"]] = dl["updatedAt"]
    for sl in payload["single_locs"]:
        out[sl["unique_asset_id"]] = sl["attributes"]["updatedAt"]
    return out


def delete_feature(layer, attr, value):
    fid = [f.id() for f in layer.getFeatures() if f[attr] == value][0]
    assert layer.dataProvider().deleteFeatures([fid])
    return fid


# ----------------------------------------------------------------------
# Payload structure
# ----------------------------------------------------------------------

class TestPayloadStructure:
    def test_envelope(self, net):
        p = build(net)
        assert p["location_id"] == LOCATION_ID
        assert p["organization_id"] == "org-0001"
        assert p["Location"] == {"center": {"x": -6.43, "y": 53.57},
                                 "radius": 500.0, "name": "Testing Location",
                                 "is_update": True}
        assert (len(p["multiLocs"]), len(p["dual_locs"]), len(p["single_locs"])) == (1, 1, 6)
        assert [e["type"] for e in p["_feature_map"]].count("single") == 6
        assert p["_stale_stop_drops"] == {}
        assert "_deleted_loc_ids" not in p

    def test_multiloc_route(self, net):
        ml = build(net)["multiLocs"][0]
        assert ml["multiName"] == "CBL-001"
        assert [s["stopNumber"] for s in ml["Stops"]] == [1, 2, 3, 4]
        assert [s["singleLoc"]["unique_asset_id"] for s in ml["Stops"]] == [
            "CBL-001_Pole 2", "CBL-001_Chamber 1_IN",
            "CBL-001_Chamber 1_OUT", "CBL-001_Pole 3",
        ]
        dl = ml["dualLoc"]
        assert dl["unique_asset_id"] == "CBL-001"
        assert dl["actual_asset_name"] == "Pole 1"         # origin structure
        assert dl["LOCDestination"]["destination"] == "Pole 4"
        assert dl["category"] == CAT_FIBER and dl["category_type"] == "dual"
        assert dl["fields"] == {"Fiber Count": "144", "Colour": "Blue"}
        assert (dl["field_1"], dl["field_2"]) == ("144", "Blue")
        assert dl["LOCDestination"]["destination_fields"] == {"Length": "200"}
        assert dl["LOCDestination"]["destination_field_1"] == "200"
        assert (dl["origin_longitude"], dl["origin_latitude"]) == wgs(X0, Y0)
        assert (dl["LOCDestination"]["longitude"],
                dl["LOCDestination"]["latitude"]) == wgs(X0 + 100, Y0 + 100)
        assert dl["LOCDestination"]["loc_id"] == dl["loc_id"]
        assert dl["user_id"] == "u-0001" and dl["location_id"] == LOCATION_ID
        assert not OMITTED_KEYS & set(dl) and not OMITTED_KEYS & set(dl["LOCDestination"])

    def test_stop_single_locs(self, net):
        stops = build(net)["multiLocs"][0]["Stops"]
        sl = stops[0]["singleLoc"]
        assert sl["category"] == CAT_STOPS and sl["category_name"] == "Stops"
        assert sl["fields"] == {"Colour": "Blue"}  # read from the LINE feature
        assert sl["field_1"] == "Blue"
        assert sl["name"] == "Stop_1_CBL-001"
        assert (sl["origin_longitude"], sl["origin_latitude"]) == wgs(X0 + 50, Y0 + 0.5)
        assert not OMITTED_KEYS & set(sl)
        # IN/OUT pair share a coordinate but never an identity
        assert stops[1]["singleLoc"]["loc_id"] != stops[2]["singleLoc"]["loc_id"]
        assert len({s["stop_id"] for s in stops}) == 4
        assert all("_matched_stop_id" not in s["singleLoc"] for s in stops)

    def test_unmatched_route_is_standalone_dual(self, net):
        dl = build(net)["dual_locs"][0]
        assert dl["unique_asset_id"] == "CBL-002"
        assert dl["actual_asset_name"] == "CBL-002"   # line-name fallback
        assert (dl["origin_longitude"], dl["origin_latitude"]) == wgs(X0 + 500, Y0)
        assert (dl["LOCDestination"]["longitude"],
                dl["LOCDestination"]["latitude"]) == wgs(X0 + 600, Y0)

    def test_standalone_single_locs(self, net):
        singles = {s["unique_asset_id"]: s for s in build(net)["single_locs"]}
        assert set(singles) == {"P1", "P2", "P3", "P4", "P5", "CH1"}
        p1 = singles["P1"]
        assert p1["actual_asset_name"] == p1["name"] == "Pole 1"
        assert p1["category"] == CAT_POLE
        assert p1["fields"] == {"Height": "9", "Material": "Wood"}
        attrs = p1["attributes"]
        assert attrs["category_id"] == CAT_POLE and attrs["LOC_type"] == "single"
        assert (attrs["field_1"], attrs["field_2"]) == ("9", "Wood")
        assert tuple(p1["coordinates"]) == wgs(X0, Y0)
        assert not OMITTED_KEYS & set(attrs)
        assert singles["CH1"]["fields"] == {"Depth": "1.2"}

    def test_identity_fallbacks_for_single_locs(self, net):
        poles = net.layers["poles"]
        fid = next(poles.getFeatures()).id()
        idx = poles.fields().indexOf
        poles.dataProvider().changeAttributeValues(
            {fid: {idx("PoleID"): None, idx("PoleName"): None}})
        maps = network_mappings(net.layers)
        maps[1].field_mappings = [
            FieldMapping("Unique Asset Identifier", "PoleID"),
            FieldMapping("Actual Asset Name", "PoleName"),
        ]
        net.maps = maps
        uids = {s["unique_asset_id"] for s in build(net)["single_locs"]}
        assert f"Feature_{fid}" in uids

        maps[1].field_mappings = [FieldMapping("Height", "Height")]
        uids = {s["unique_asset_id"] for s in build(net)["single_locs"]}
        assert "9" in uids  # first mapped field when no identity mapped

    def test_disabled_layer_excluded(self, net):
        net.maps[1].enabled_for_export = False
        p = build(net)
        assert {s["category"] for s in p["single_locs"]} == {CAT_CHAMBER}

    def test_unmapped_line_layer_route_skipped(self, net):
        net.maps[0].enabled_for_export = False
        p = build(net)
        assert p["multiLocs"] == [] and p["dual_locs"] == []

    def test_non_point_layer_with_single_category_skipped(self, net):
        net.maps[0].category_id = CAT_POLE
        p = build(net)
        assert len(p["single_locs"]) == 6

    def test_deleted_line_feature_dropped(self, net):
        delete_feature(net.layers["cables"], "AssetID", "CBL-002")
        assert build(net)["dual_locs"] == []

    def test_deleted_stop_point_dropped_and_recorded(self, net):
        delete_feature(net.layers["poles"], "PoleID", "P2")
        p = build(net)
        ml = p["multiLocs"][0]
        assert [s["singleLoc"]["unique_asset_id"] for s in ml["Stops"]][0] == \
            "CBL-001_Chamber 1_IN"
        assert [s["stopNumber"] for s in ml["Stops"]] == [1, 2, 3]
        assert p["_stale_stop_drops"] == {ml["id"]: 1}

    def test_removed_stops_excluded(self, net):
        r = [r for r in net.routes if r.line_name == "CBL-001"][0]
        r.stops[1].removed = True
        stops = build(net)["multiLocs"][0]["Stops"]
        assert len(stops) == 3

    def test_all_stops_removed_becomes_dual(self, net):
        r = [r for r in net.routes if r.line_name == "CBL-001"][0]
        for s in r.intermediate_stops:
            s.removed = True
        p = build(net)
        assert p["multiLocs"] == []
        assert {d["unique_asset_id"] for d in p["dual_locs"]} == {"CBL-001", "CBL-002"}

    def test_renamed_stop_label_wins(self, net):
        r = [r for r in net.routes if r.line_name == "CBL-001"][0]
        r.stops[1].display_name = "Custom Label"
        sl = build(net)["multiLocs"][0]["Stops"][0]["singleLoc"]
        assert sl["unique_asset_id"] == "CBL-001_Custom Label"
        assert sl["actual_asset_name"] == "Custom Label"

    def test_mapped_destination_and_name_fields(self, net):
        net.maps[0].field_mappings += [FieldMapping("Actual Asset Name", "CableName")]
        net.maps[0].field_mappings[3] = FieldMapping("Destination", "Colour")
        dl = build(net)["multiLocs"][0]["dualLoc"]
        assert dl["actual_asset_name"] == "Feeder 1"
        assert dl["LOCDestination"]["destination"] == "Blue"

    def test_pulled_ids_produce_delete_list(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        kept = p1["single_locs"][0]["attributes"]["loc_id"]
        p2 = build(net, pulled_loc_ids={kept, "gone-1", "gone-2"})
        assert p2["_deleted_loc_ids"] == ["gone-1", "gone-2"]

    def test_payload_is_json_serialisable(self, net):
        json.dumps(build(net))


# ----------------------------------------------------------------------
# Summary / metrics / validation
# ----------------------------------------------------------------------

def test_payload_summary(net):
    assert eb.payload_summary(build(net)) == {
        "location": "Testing Location", "routes_with_stops": 1,
        "routes_without_stops": 1, "total_routes": 2, "total_stops": 4,
        "standalone_assets": 6, "total_locs": 12, "will_be_deleted": 0,
    }


def test_log_payload_metrics(net):
    p = build(net)
    text = eb.log_payload_metrics(p)
    for needle in ("multiLocs: 1", "dual_locs: 1", "single_locs: 6",
                   "Route 'CBL-001': 4 stop(s)", "Total stops: 4", "Payload size:"):
        assert needle in text
    assert "DUPLICATE" not in text and "Will be deleted" not in text

    p["_deleted_loc_ids"] = ["a", "b"]
    dup = p["multiLocs"][0]["Stops"][0]["singleLoc"]["loc_id"]
    p["multiLocs"][0]["Stops"][1]["singleLoc"]["loc_id"] = dup
    text = eb.log_payload_metrics(p)
    assert "Will be deleted on server (absent from payload): 2" in text
    assert "DUPLICATE stop loc_ids: 1" in text


class TestValidatePayload:
    def test_generated_payload_is_valid(self, net):
        assert eb.validate_payload(build(net)) == []

    def _ml(self, name="R", mid="m1", loc_id="d1", stops=()):
        return {"id": mid, "multiName": name, "dualLoc": {"loc_id": loc_id},
                "Stops": [{"stopNumber": n, "singleLoc": {"loc_id": l}}
                          for n, l in stops]}

    def test_duplicate_stop_loc_id_across_routes(self):
        p = {"multiLocs": [self._ml("A", stops=[(1, "x")]),
                           self._ml("B", mid="m2", loc_id="d2", stops=[(1, "x"), (2, "")])]}
        issues = eb.validate_payload(p)
        assert ("error", "Duplicate stop loc_id x... in routes 'A' and 'B'") in issues

    def test_non_sequential_stop_numbers(self):
        p = {"multiLocs": [self._ml(stops=[(1, "a"), (3, "b")])]}
        assert eb.validate_payload(p)[0][0] == "error"
        assert "non-sequential" in eb.validate_payload(p)[0][1]

    def test_stop_count_invariant(self):
        p = {"multiLocs": [self._ml(stops=[(1, "a")])], "_stale_stop_drops": {}}
        assert eb.validate_payload(p, {"m1": 2}, True)[0][0] == "error"
        assert eb.validate_payload(p, {"m1": 2}, False)[0][0] == "warning"
        assert eb.validate_payload(p, {"m1": 1}, True) == []
        assert eb.validate_payload(p, {"other": 9}, True) == []
        p["_stale_stop_drops"] = {"m1": 1}  # an intentional delete
        assert eb.validate_payload(p, {"m1": 2}, True) == []

    def test_missing_ids(self):
        p = {"multiLocs": [self._ml(mid="", loc_id="")],
             "dual_locs": [{"unique_asset_id": "D", "loc_id": ""}]}
        issues = eb.validate_payload(p)
        assert ("warning", "Route 'R' has no multiLoc ID — server will treat "
                "this as a new route.") in issues
        assert ("error", "Route 'R' dualLoc is missing loc_id.") in issues
        assert ("error", "Standalone dual LOC 'D' is missing loc_id.") in issues

    def test_duplicate_single_uid_warning(self):
        p = {"single_locs": [{"unique_asset_id": "P"}, {"unique_asset_id": "P"},
                             {"unique_asset_id": ""}]}
        (level, msg), = eb.validate_payload(p)
        assert level == "warning" and "'P' is used by 2 features" in msg


# ----------------------------------------------------------------------
# Stamp cache: stable IDs across pushes
# ----------------------------------------------------------------------

class TestStampCache:
    def test_sidecar_written_next_to_project(self, net):
        p = build(net)
        assert eb.stamp_ids_after_push(p) == 8
        sidecar = net.tmp / "network_loc_stamps.json"
        data = json.loads(sidecar.read_text("utf-8"))
        entries = data[LOCATION_ID]
        assert len(entries) == 8
        multi = [e for e in entries.values() if e.get("_multi_id")][0]
        assert multi["_route_name"] == "CBL-001"
        assert len(multi["_stop_ids"].split(",")) == 4
        assert len(multi["_stop_loc_ids"].split("|")) == 4
        single = [e for e in entries.values() if e.get("_unique_asset_id") == "P1"][0]
        assert single["_wrapper_id"] == [s for s in p["single_locs"]
                                         if s["unique_asset_id"] == "P1"][0]["id"]

    def test_repush_reuses_every_id_and_timestamp(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        eb._stamp_cache = None  # force a re-read from disk
        p2 = build(net)
        assert all_ids(p2) == all_ids(p1)
        assert timestamps(p2) == timestamps(p1)
        assert eb.validate_payload(p2) == []

    def test_edit_bumps_only_the_changed_asset(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        time.sleep(0.01)
        poles = net.layers["poles"]
        fid = [f.id() for f in poles.getFeatures() if f["PoleID"] == "P5"][0]
        poles.dataProvider().changeAttributeValues(
            {fid: {poles.fields().indexOf("Height"): "12"}})
        before, after = timestamps(p1), timestamps(build(net))
        changed = {k for k in before if before[k] != after[k]}
        assert changed == {"P5"}

    def test_route_field_edit_bumps_dual(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        time.sleep(0.01)
        cables = net.layers["cables"]
        fid = [f.id() for f in cables.getFeatures() if f["AssetID"] == "CBL-002"][0]
        cables.dataProvider().changeAttributeValues(
            {fid: {cables.fields().indexOf("FiberCount"): "24"}})
        before, after = timestamps(p1), timestamps(build(net))
        assert {k for k in before if before[k] != after[k]} == {"CBL-002"}

    def test_stop_count_change_bumps_all_stops(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        time.sleep(0.01)
        r = [r for r in net.routes if r.line_name == "CBL-001"][0]
        r.stops[-2].removed = True  # drop Pole 3
        p2 = build(net)
        stops = p2["multiLocs"][0]["Stops"]
        assert len(stops) == 3
        old = timestamps(p1)
        assert all(s["singleLoc"]["updatedAt"] != old[s["singleLoc"]["unique_asset_id"]]
                   for s in stops)
        # identities of surviving stops are still reused
        old_ids = {s["singleLoc"]["loc_id"] for s in p1["multiLocs"][0]["Stops"]}
        assert {s["singleLoc"]["loc_id"] for s in stops} <= old_ids

    def test_stamp_guards(self, net):
        assert eb.stamp_ids_after_push({}) == 0
        assert eb.stamp_ids_after_push({"_feature_map": [{}]}) == 0
        p = build(net)
        p["_feature_map"].append({"layer_id": "gone", "feature_id": 1,
                                  "type": "single", "index": 0})
        p["_feature_map"].append({"layer_id": net.layers["poles"].id(),
                                  "feature_id": 9999, "type": "single", "index": 0})
        p["_feature_map"].append({"layer_id": net.layers["poles"].id(),
                                  "feature_id": 1, "type": "single", "index": 99})
        assert eb.stamp_ids_after_push(p) == 8

    def test_unsaved_project_keeps_cache_in_memory(self, net):
        p = build(net)
        QgsProject.instance().setFileName("")
        eb._stamp_cache_path = None
        assert eb.stamp_ids_after_push(p) == 8
        assert eb._get_stamp_cache_path() is None
        assert not (net.tmp / "network_loc_stamps.json").exists()

    def test_corrupt_or_foreign_sidecar(self, net):
        sidecar = net.tmp / "network_loc_stamps.json"
        sidecar.write_text("{broken", "utf-8")
        assert eb._load_stamp_cache() == {}
        eb._stamp_cache = None
        sidecar.write_text("[1, 2]", "utf-8")
        assert eb._load_stamp_cache() == {}

    def test_sidecar_write_failure_is_logged_not_raised(self, net):
        sidecar = net.tmp / "network_loc_stamps.json"
        sidecar.mkdir()
        eb._get_stamp_cache_path()          # resolve the path first
        eb._save_stamp_cache({"x": {}})     # path is a directory → write fails
        assert sidecar.is_dir()
        assert eb._stamp_cache == {"x": {}}  # still usable for this session

    def test_project_switch_resolves_new_sidecar(self, net, tmp_path):
        eb.stamp_ids_after_push(build(net))
        assert eb._load_stamp_cache()
        other = tmp_path / "sub"
        other.mkdir()
        save_project(other, "other.qgz")
        assert eb._get_stamp_cache_path() == other / "other_loc_stamps.json"
        assert eb._load_stamp_cache() == {}

    def test_switch_to_unsaved_project_drops_stamps(self, net):
        eb.stamp_ids_after_push(build(net))
        assert eb._load_stamp_cache()
        QgsProject.instance().clear()          # new, never-saved project
        assert eb._get_stamp_cache_path() is None
        assert eb._load_stamp_cache() == {}

    def test_project_switch_does_not_serve_stale_stamps(self, net, tmp_path):
        eb.stamp_ids_after_push(build(net))
        other = tmp_path / "sub"
        other.mkdir()
        save_project(other, "other.qgz")
        assert eb._load_stamp_cache() == {}

    def test_stamps_scoped_by_location(self, net):
        eb.stamp_ids_after_push(build(net))
        p_other = eb.build_payload(net.routes, net.maps, net.cats, location(1),
                                   "u", "o")
        p_same = build(net)
        assert set(all_ids(p_other)).isdisjoint(all_ids(p_same))


class TestStampMigration:
    """Layer source strings change on regeneration/pull; IDs must survive."""

    def _clone_layers(self, net):
        clones = {}
        for key, layer in net.layers.items():
            clone = layer.materialize(QgsFeatureRequest())
            clone.setName(layer.name())
            QgsProject.instance().addMapLayer(clone)
            QgsProject.instance().removeMapLayer(layer.id())
            clones[key] = clone
        net.layers = clones
        net.maps = network_mappings(clones)
        net.routes = rg.generate_routes(net.maps)

    def test_multi_routes_and_assets_keep_ids(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        self._clone_layers(net)
        p2 = build(net)
        m1, m2 = p1["multiLocs"][0], p2["multiLocs"][0]
        assert m2["id"] == m1["id"]
        assert m2["dualLoc"]["loc_id"] == m1["dualLoc"]["loc_id"]
        assert [s["singleLoc"]["loc_id"] for s in m2["Stops"]] == \
            [s["singleLoc"]["loc_id"] for s in m1["Stops"]]
        s1 = {s["unique_asset_id"]: s["attributes"]["loc_id"] for s in p1["single_locs"]}
        s2 = {s["unique_asset_id"]: s["attributes"]["loc_id"] for s in p2["single_locs"]}
        assert s2 == s1

    def test_standalone_dual_keeps_id(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        self._clone_layers(net)
        p2 = build(net)
        assert p2["dual_locs"][0]["loc_id"] == p1["dual_locs"][0]["loc_id"]

    def test_saved_ids_are_keyed_by_asset(self, net):
        eb.stamp_ids_after_push(build(net))
        keys = set(json.loads((net.tmp / "network_loc_stamps.json")
                              .read_text())[LOCATION_ID])
        assert f"asset|{CAT_POLE}|P1" in keys
        assert f"asset|{CAT_CHAMBER}|CH1" in keys
        assert f"asset|{CAT_FIBER}|CBL-001" in keys and f"asset|{CAT_FIBER}|CBL-002" in keys
        assert all(k.startswith("asset|") for k in keys)

    def test_ids_survive_feature_renumbering(self, net):
        """Shapefile-style: features rewritten in another order get new
        feature ids; saved IDs must still follow the assets."""
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        for layer in net.layers.values():
            feats = list(layer.getFeatures())
            layer.dataProvider().deleteFeatures([f.id() for f in feats])
            layer.dataProvider().addFeatures(list(reversed(feats)))
        net.routes = rg.generate_routes(net.maps)
        p2 = build(net)
        assert sorted(all_ids(p2)) == sorted(all_ids(p1))
        assert timestamps(p2) == timestamps(p1)

    def test_duplicate_asset_ids_keep_separate_records(self, net):
        poles = net.layers["poles"]
        p5 = feature_where(poles, "PoleID", "P5")
        poles.dataProvider().changeAttributeValues(
            {p5.id(): {poles.fields().indexOf("PoleID"): "P1",
                       poles.fields().indexOf("PoleName"): "Pole 1"}})
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        p2 = build(net)
        ids1 = [s["attributes"]["loc_id"] for s in p1["single_locs"] if s["unique_asset_id"] == "P1"]
        ids2 = [s["attributes"]["loc_id"] for s in p2["single_locs"] if s["unique_asset_id"] == "P1"]
        assert len(set(ids1)) == 2 and ids2 == ids1      # distinct, and stable
        keys = eb._load_stamp_cache()[LOCATION_ID]
        assert f"asset|{CAT_POLE}|P1" not in keys          # per-feature keys instead

    def _to_legacy_sidecar(self, net):
        """Rewrite the saved IDs in the old layer_source:fid format."""
        cache = eb._load_stamp_cache()
        legacy_of = {}
        for layer in net.layers.values():
            for f in layer.getFeatures():
                k = eb._asset_key_for(layer, f)
                if k:
                    legacy_of[k] = f"{layer.source()}:{f.id()}"
        cache[LOCATION_ID] = {legacy_of.get(k, k): v
                              for k, v in cache[LOCATION_ID].items()}
        (net.tmp / "network_loc_stamps.json").write_text(json.dumps(cache))
        eb._stamp_cache = None

    def test_old_sidecar_upgraded_in_place(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        self._to_legacy_sidecar(net)
        p2 = build(net)
        assert sorted(all_ids(p2)) == sorted(all_ids(p1))
        keys = json.loads((net.tmp / "network_loc_stamps.json").read_text())[LOCATION_ID]
        assert all(k.startswith("asset|") for k in keys)

    def test_old_sidecar_upgraded_after_layers_recreated(self, net):
        p1 = build(net)
        eb.stamp_ids_after_push(p1)
        self._to_legacy_sidecar(net)
        self._clone_layers(net)             # old layer sources are gone
        p2 = build(net)
        assert sorted(all_ids(p2)) == sorted(all_ids(p1))

    def test_nothing_to_upgrade(self, net):
        assert eb._migrate_stamps(net.routes, net.maps, LOCATION_ID) == 0
        eb.stamp_ids_after_push(build(net))
        assert eb._migrate_stamps(net.routes, net.maps, LOCATION_ID) == 0


# ----------------------------------------------------------------------
# Pulled data → push (raw-JSON patch paths, round-trip fidelity)
# ----------------------------------------------------------------------

def build_pulled(p, routes=None):
    if routes is None:
        routes = ib.reconstruct_routes(p.maps, skip_snapping=True)
    return eb.build_payload(routes, p.maps, p.cats, p.loc, "u-0001", "org-0001",
                            pulled_loc_ids=p.ids)


def clear_hidden_coords(layer, fid, *names):
    idx = layer.fields().indexOf
    layer.dataProvider().changeAttributeValues({fid: {idx(n): None for n in names}})


class TestPulledRoundTrip:
    def test_unchanged_pull_preserves_server_records(self, tmp_path):
        def mutate(data):
            for s in data["singleLOCs"]:
                s["QA_reject_reason"] = "blurry photo"
            data["multiLoc"][0]["dualLoc"]["imageDescription"] = "kept"
            data["multiLoc"][0]["Stops"][0]["singleLoc"]["sla_fields"] = {"x": 1}
        p = pulled_network(tmp_path, mutate)
        payload = build_pulled(p)
        assert payload["_deleted_loc_ids"] == []

        src = {s["unique_asset_id"]: s for s in p.data["singleLOCs"]}
        for s in payload["single_locs"]:
            a, orig = s["attributes"], src[s["unique_asset_id"]]
            assert a["QA_reject_reason"] == "blurry photo"
            assert (a["loc_id"], a["updatedAt"]) == (orig["loc_id"], orig["updatedAt"])
            assert a["category_id"] == orig["category"] and a["location_id"] == LOCATION_ID

        ml, raw = payload["multiLocs"][0], p.data["multiLoc"][0]
        assert ml["id"] == raw["id"]
        assert ml["dualLoc"]["imageDescription"] == "kept"
        assert ml["dualLoc"]["updatedAt"] == raw["dualLoc"]["updatedAt"]
        assert [s["stop_id"] for s in ml["Stops"]] == [s["stop_id"] for s in raw["Stops"]]
        assert [s["singleLoc"]["updatedAt"] for s in ml["Stops"]] == \
            [s["singleLoc"]["updatedAt"] for s in raw["Stops"]]
        assert ml["Stops"][0]["singleLoc"]["sla_fields"] == {"x": 1}
        assert payload["dual_locs"][0]["loc_id"] == p.data["dualLOCs"][0]["loc_id"]
        counts = ib.extract_multi_stop_counts(p.data)
        assert eb.validate_payload(payload, counts, True) == []

    def test_dual_route_edits_are_patched_and_bumped(self, tmp_path):
        p = pulled_network(tmp_path)
        cables = p.layers["Fiber Cable (LOC)"]
        f = feature_where(cables, "Route ID", "CBL-002")
        idx = cables.fields().indexOf
        cables.dataProvider().changeAttributeValues({f.id(): {
            idx("Route ID"): "CBL-002B", idx("Destination"): "Cabinet 9",
            idx("Length"): "150", idx("Actual Asset Name"): "Renamed"}})
        verts = f.geometry().asPolyline()
        moved_end = QgsPointXY(verts[-1].x() + 0.0002, verts[-1].y())
        cables.dataProvider().changeGeometryValues(
            {f.id(): QgsGeometry.fromPolylineXY([verts[0], moved_end])})
        clear_hidden_coords(cables, f.id(), "_dest_lon", "_dest_lat")
        time.sleep(0.01)

        dl = build_pulled(p)["dual_locs"][0]
        raw = p.data["dualLOCs"][0]
        assert dl["loc_id"] == raw["loc_id"]
        assert (dl["unique_asset_id"], dl["actual_asset_name"]) == ("CBL-002B", "Renamed")
        dest = dl["LOCDestination"]
        assert dest["destination"] == "Cabinet 9"
        assert dest["destination_fields"]["Length"] == "150"
        assert dest["longitude"] == pytest.approx(moved_end.x())
        assert dl["origin_longitude"] == raw["origin_longitude"]   # not moved
        assert dl["updatedAt"] != raw["updatedAt"] and dest["updatedAt"] == dl["updatedAt"]

    def test_patch_helpers_with_sparse_raw_json(self, tmp_path):
        p = pulled_network(tmp_path)
        f = feature_where(p.layers["Fiber Cable (LOC)"], "Route ID", "CBL-002")
        eb._set_current_layer(p.layers["Fiber Cable (LOC)"])  # as build_payload does
        route = Route(line_name="L", origin="O", destination="D")
        mapping = LayerMapping(category_id=CAT_FIBER, category_name="Fiber Cable",
                               field_mappings=[FieldMapping("Length", "Length"),
                                               FieldMapping("Colour", "Colour")])
        out = eb._patch_dual_loc({"fields": None}, route, f, mapping,
                                 p.cats[CAT_FIBER], (1.0, 2.0), (3.0, 4.0), "NOW")
        assert (out["origin_longitude"], out["LOCDestination"]["longitude"]) == (1.0, 3.0)
        assert out["fields"] == {"Colour": "Black"}
        assert out["LOCDestination"]["destination_fields"] == {"Length": "100"}
        assert (out["unique_asset_id"], out["actual_asset_name"]) == ("L", "O")
        assert out["LOCDestination"]["destination"] == "D"
        assert out["updatedAt"] == "NOW" and "location_id" not in out
        bare = eb._patch_dual_loc({}, route, None, LayerMapping(), None,
                                  (0.0, 0.0), (7.0, 8.0), "NOW")
        assert bare["LOCDestination"] == {"longitude": 7.0, "latitude": 8.0,
                                          "destination": "D", "updatedAt": "NOW"}
        assert "fields" not in bare

        pf = feature_where(p.layers["Pole (LOC)"], "Unique Asset Identifier", "P1")
        eb._set_current_layer(p.layers["Pole (LOC)"])
        lm = LayerMapping(category_id=CAT_POLE, category_name="Pole",
                          field_mappings=[FieldMapping("Height", "Height")])
        res = eb._patch_standalone_sloc({"fields": "legacy"}, pf, lm,
                                        p.cats[CAT_POLE].fields, (5.0, 6.0), "NOW")
        assert res["attributes"]["fields"] == {"Height": "9"}
        assert res["unique_asset_id"] == "9"          # first mapped field
        assert res["attributes"]["updatedAt"] == "NOW"
        assert "location_id" not in res["attributes"]
        res = eb._patch_standalone_sloc({}, pf, LayerMapping(category_id=CAT_POLE),
                                        [], (5.0, 6.0), "NOW")
        assert res["unique_asset_id"] == f"Feature_{pf.id()}"

    def test_stop_coordinate_drift_uses_sequential_fallback(self, tmp_path):
        p = pulled_network(tmp_path)
        routes = ib.reconstruct_routes(p.maps, skip_snapping=True)
        poles = p.layers["Pole (LOC)"]
        f = feature_where(poles, "Unique Asset Identifier", "P2")
        pt = f.geometry().asPoint()
        poles.dataProvider().changeGeometryValues(
            {f.id(): QgsGeometry.fromPointXY(QgsPointXY(pt.x() + 0.0001, pt.y()))})
        clear_hidden_coords(poles, f.id(), "_origin_lon", "_origin_lat")
        stops = build_pulled(p, routes)["multiLocs"][0]["Stops"]
        raw = p.data["multiLoc"][0]["Stops"]
        assert [s["stop_id"] for s in stops] == [s["stop_id"] for s in raw]
        assert [s["singleLoc"]["loc_id"] for s in stops] == \
            [s["singleLoc"]["loc_id"] for s in raw]
        assert stops[0]["singleLoc"]["origin_longitude"] == pytest.approx(pt.x() + 0.0001)

    def _edit_pulled_route(self, tmp_path, remove=(), add_pole=False):
        """Pull, optionally add a pole near the end of CBL-001, reconstruct,
        then soft-remove the named stops in review.  Returns (p, payload)."""
        p = pulled_network(tmp_path)
        poles, cables = p.layers["Pole (LOC)"], p.layers["Fiber Cable (LOC)"]
        if add_pole:
            verts = feature_where(cables, "Route ID", "CBL-001").geometry().asPolyline()
            mid = QgsPointXY((verts[4].x() + verts[5].x()) / 2,
                             (verts[4].y() + verts[5].y()) / 2)
            nf = QgsFeature(poles.fields())
            nf.setGeometry(QgsGeometry.fromPointXY(mid))
            nf.setAttribute("Unique Asset Identifier", "P-NEW")
            nf.setAttribute("Actual Asset Name", "New Pole")
            poles.dataProvider().addFeatures([nf])
        routes = ib.reconstruct_routes(p.maps, snap_tolerance=1.0)
        r = [r for r in routes if r.line_name == "CBL-001"][0]
        for s in r.stops:
            if s.structure_name in remove:
                s.removed = True
        time.sleep(0.01)
        return p, build_pulled(p, routes)

    def _raw_stops(self, p):
        return {s["singleLoc"]["loc_id"]: s for s in p.data["multiLoc"][0]["Stops"]}

    def test_removed_stop_renumbers_and_bumps(self, tmp_path):
        p, payload = self._edit_pulled_route(tmp_path, remove=("Pole 2",))
        raw = self._raw_stops(p)
        stops = payload["multiLocs"][0]["Stops"]
        assert [s["stopNumber"] for s in stops] == [1, 2, 3]
        assert [s["singleLoc"]["unique_asset_id"] for s in stops] == [
            "CBL-001_Chamber 1_IN", "CBL-001_Chamber 1_OUT", "CBL-001_Pole 3"]
        for s in stops:                          # same identity, renumbered
            orig = raw[s["singleLoc"]["loc_id"]]
            assert s["stop_id"] == orig["stop_id"]
            assert s["singleLoc"]["updatedAt"] != orig["singleLoc"]["updatedAt"]
            assert s["singleLoc"]["name"] == f"Stop_{s['stopNumber']}_CBL-001"
        p2_stop = [lid for lid, s in raw.items()
                   if s["singleLoc"]["unique_asset_id"] == "CBL-001_Pole 2"]
        assert payload["_deleted_loc_ids"] == p2_stop
        assert eb.validate_payload(payload) == []

    def test_added_stop_is_created(self, tmp_path):
        p, payload = self._edit_pulled_route(tmp_path, add_pole=True)
        raw = self._raw_stops(p)
        stops = payload["multiLocs"][0]["Stops"]
        assert [s["stopNumber"] for s in stops] == [1, 2, 3, 4, 5]
        assert stops[4]["singleLoc"]["unique_asset_id"] == "CBL-001_New Pole"
        assert stops[4]["singleLoc"]["loc_id"] not in raw
        assert stops[4]["stop_id"] not in {s["stop_id"] for s in raw.values()}
        assert [s["singleLoc"]["loc_id"] for s in stops[:4]] == list(raw)
        assert eb.validate_payload(payload) == []

    def test_remove_one_add_one_keeps_identities_distinct(self, tmp_path):
        _, payload = self._edit_pulled_route(tmp_path, remove=("Pole 2",), add_pole=True)
        lids = [s["singleLoc"]["loc_id"] for s in payload["multiLocs"][0]["Stops"]]
        assert len(set(lids)) == len(lids)
        assert eb.validate_payload(payload) == []

    def test_new_stop_gets_unique_stop_id(self, tmp_path):
        _, payload = self._edit_pulled_route(
            tmp_path, remove=("Pole 2", "Pole 3"), add_pole=True)
        stop_ids = [s["stop_id"] for s in payload["multiLocs"][0]["Stops"]]
        assert len(stop_ids) == 3
        assert len(set(stop_ids)) == len(stop_ids)

    # Two structures at one coordinate, as on staging Howth:
    #  * "Pole 1 Twin" shares Pole 1's spot, the route's origin
    #    (Splice Enclosure 8 / Handhole 31 on OM_FIB_4_4);
    #  * "Handhole X" shares Chamber 1's spot, so three stops sit on one
    #    coordinate: Handhole X, Chamber 1 IN, Chamber 1 OUT (OM_DUCT_14).
    COLOCATED = [
        (X0, Y0, {"PoleID": "P1T", "PoleName": "Pole 1 Twin",
                  "Height": "9", "Material": "Wood"}),
        (X0 + 100, Y0, {"PoleID": "HX", "PoleName": "Handhole X",
                        "Height": "1", "Material": "Concrete"}),
    ]

    def test_colocated_structures_rebuild_exactly(self, tmp_path):
        p = pulled_network(tmp_path, extra_poles=self.COLOCATED)
        raw = p.data["multiLoc"][0]
        raw_uids = [s["singleLoc"]["unique_asset_id"]
                    for s in sorted(raw["Stops"], key=lambda s: s["stopNumber"])]
        routes = ib.reconstruct_routes(p.maps, snap_tolerance=1.0)
        r = [r for r in routes if r.line_name == "CBL-001"][0]
        assert r.stops[0].structure_name == "Pole 1"              # right twin
        assert [s.structure_name for s in r.intermediate_stops] == [
            "Pole 1 Twin", "Pole 2", "Handhole X", "Chamber 1", "Chamber 1", "Pole 3"]
        assert len(r.intermediate_stops) == len(raw_uids)          # nothing "added"

        payload = build_pulled(p, routes)
        assert payload["_deleted_loc_ids"] == []
        stops = payload["multiLocs"][0]["Stops"]
        assert [s["singleLoc"]["unique_asset_id"] for s in stops] == raw_uids
        assert [s["stop_id"] for s in stops] == \
            [s["stop_id"] for s in sorted(raw["Stops"], key=lambda s: s["stopNumber"])]
        assert eb.validate_payload(payload) == []

    def test_server_names_kept_when_id_points_at_colocated_structure(self, tmp_path):
        """Howth pattern: stop "Splice Enclosure 2_OUT" whose unique id is
        the co-located "Vault 8".  Unedited round trip changes nothing and
        no phantom stop is added."""
        def mutate(data):
            stops = data["multiLoc"][0]["Stops"]
            out = next(s for s in stops
                       if s["singleLoc"]["unique_asset_id"].endswith("Chamber 1_OUT"))
            out["singleLoc"]["unique_asset_id"] = "CBL-001_Handhole X"
        p = pulled_network(tmp_path, mutate, extra_poles=self.COLOCATED)
        raw = sorted(p.data["multiLoc"][0]["Stops"], key=lambda s: s["stopNumber"])
        for kw in (dict(skip_snapping=True), dict(snap_tolerance=1.0)):
            routes = ib.reconstruct_routes(p.maps, **kw)
            r = [r for r in routes if r.line_name == "CBL-001"][0]
            assert len(r.intermediate_stops) == len(raw)              # no phantom
            out_stop = next(s for s in r.stops if s.original_name.endswith("_OUT"))
            assert out_stop.stop_type == StopType.EGRESS
            assert out_stop.structure_name == "Handhole X"            # per its id
            stops = build_pulled(p, routes)["multiLocs"][0]["Stops"]
            assert [(s["singleLoc"]["unique_asset_id"], s["singleLoc"]["actual_asset_name"])
                    for s in stops] == \
                [(s["singleLoc"]["unique_asset_id"], s["singleLoc"]["actual_asset_name"])
                 for s in raw]
            assert [s["singleLoc"]["updatedAt"] for s in stops] == \
                [s["singleLoc"]["updatedAt"] for s in raw]

    def test_renamed_pulled_stop_pushes_new_name(self, tmp_path):
        p = pulled_network(tmp_path)
        routes = ib.reconstruct_routes(p.maps, skip_snapping=True)
        r = [r for r in routes if r.line_name == "CBL-001"][0]
        r.intermediate_stops[0].display_name = "Renamed In Review"
        sl = build_pulled(p, routes)["multiLocs"][0]["Stops"][0]["singleLoc"]
        assert sl["actual_asset_name"] == "Renamed In Review"
        assert sl["unique_asset_id"] == "CBL-001_Renamed In Review"

    def test_new_stop_never_takes_a_colocated_stops_record(self, tmp_path):
        """Even if a co-located structure is snapped in as a new stop, it
        gets its own loc_id instead of an existing stop's record."""
        p = pulled_network(tmp_path, extra_poles=self.COLOCATED)
        routes = ib.reconstruct_routes(p.maps, skip_snapping=True)
        r = [r for r in routes if r.line_name == "CBL-001"][0]
        # simulate the old mis-match: point the Handhole X stop at Chamber 1
        hx = next(s for s in r.stops if s.structure_name == "Handhole X")
        ch = next(s for s in r.stops if s.structure_name == "Chamber 1")
        extra = Stop(original_name="CBL-001_Handhole X", structure_name="Handhole X",
                     stop_type=StopType.PASSTHROUGH, stop_number=9,
                     point_layer_id=hx.point_layer_id, point_feature_id=hx.point_feature_id)
        r.stops.insert(r.stops.index(ch), extra)
        payload = build_pulled(p, routes)
        lids = [s["singleLoc"]["loc_id"] for s in payload["multiLocs"][0]["Stops"]]
        assert len(set(lids)) == len(lids)
        assert eb.validate_payload(payload) == []

    def test_variant_keys_normalised(self, tmp_path):
        def mutate(data):
            m = data["multiLoc"][0]
            m["stops"] = m.pop("Stops")
            m["dualLOC"] = m.pop("dualLoc")
            for s in m["stops"]:
                s["singleLOC"] = s.pop("singleLoc")
        p = pulled_network(tmp_path, mutate)
        ml = build_pulled(p)["multiLocs"][0]
        assert {"Stops", "dualLoc"} <= set(ml) and not {"stops", "dualLOC"} & set(ml)
        assert len(ml["Stops"]) == 4
        assert all("singleLoc" in s and "singleLOC" not in s for s in ml["Stops"])


# ----------------------------------------------------------------------
# Sync stamps from server (match by unique asset id)
# ----------------------------------------------------------------------

def test_sync_stamps_from_server(net):
    p2 = wgs_raw(X0 + 50, Y0 + 0.5)
    server = {
        "singleLOCs": [
            {"unique_asset_id": "P1", "loc_id": "L-P1", "origin_id": "O-P1",
             "id": "W-P1", "createdAt": "c", "updatedAt": "u"},
            {"unique_asset_id": "", "loc_id": "ignored"},
        ],
        "dualLOCs": [
            {"unique_asset_id": "CBL-002", "loc_id": "L-D2", "origin_id": "O-D2",
             "LOCDestination": {"destination_id": "DST-D2"}},
            {"unique_asset_id": ""},
        ],
        "multiLoc": [
            {"id": "M-1", "multiName": "CBL-001",
             "dualLOC": {"unique_asset_id": "CBL-001", "loc_id": "L-M1",
                         "origin_id": "O-M1", "LOCDestination": None},
             "stops": [
                 {"stop_id": "S-1", "stopNumber": 1,
                  "singleLOC": {"loc_id": "L-S1", "origin_id": "O-S1",
                                "origin_longitude": p2[0],
                                "origin_latitude": p2[1]}},
                 {"stop_id": "S-x", "stopNumber": 2,
                  "single_loc": {"loc_id": "L-bad", "origin_longitude": "abc",
                                 "origin_latitude": "1"}},
             ]},
            {"id": "M-2", "dualLoc": {}},
        ],
    }
    maps = network_mappings(net.layers) + [
        LayerMapping(layer_id=net.layers["poles"].id()),  # no category: skipped
        LayerMapping(layer_id="missing", category_id="c"),
        LayerMapping(layer_id=net.layers["chambers"].id(), category_id=CAT_CHAMBER),
    ]
    matched, total = eb.sync_stamps_from_server(server, LOCATION_ID, maps)
    assert (matched, total) == (3, 3)

    p = build(net)
    ml = p["multiLocs"][0]
    assert ml["id"] == "M-1" and ml["dualLoc"]["loc_id"] == "L-M1"
    assert ml["Stops"][0]["singleLoc"]["loc_id"] == "L-S1"
    assert ml["Stops"][0]["stop_id"] == "S-1"
    assert p["dual_locs"][0]["loc_id"] == "L-D2"
    assert p["dual_locs"][0]["LOCDestination"]["destination_id"] == "DST-D2"
    p1 = [s for s in p["single_locs"] if s["unique_asset_id"] == "P1"][0]
    assert p1["id"] == "W-P1" and p1["attributes"]["loc_id"] == "L-P1"


def test_sync_with_no_server_locs(net):
    assert eb.sync_stamps_from_server({}, LOCATION_ID, net.maps) == (0, 0)


# ----------------------------------------------------------------------
# Low-level helpers
# ----------------------------------------------------------------------

class TestHelpers:
    def test_parse_stop_loc_ids(self):
        raw = ("1.0,2.0=L1,O1,1.000000001,2.000000001,S1,c1,u1,3|"
               "1.0,2.0=L2|bad|x,y=L3|1,2,3=L4|3.0,4.0=L5,O5,nan?,4,S5,c,u,notint")
        out = eb._parse_stop_loc_ids(raw)
        assert out[(1.0, 2.0)] == [
            ("L1", "O1", 1.000000001, 2.000000001, "S1", "c1", "u1", 3),
            ("L2", "", 0.0, 0.0, "", "", "", 0),
        ]
        assert out[(3.0, 4.0)] == [("L5", "O5", 0.0, 0.0, "S5", "c", "u", 0)]
        assert eb._parse_stop_loc_ids("") == {}

    def test_safe_value(self, net):
        poles = net.layers["poles"]
        feat = next(poles.getFeatures())
        assert eb._safe_value(feat, "PoleID") == "P1"
        assert eb._safe_value(feat, "NoSuchField") == ""
        feat.setAttribute("PoleName", None)
        assert eb._safe_value(feat, "PoleName") == ""

    def test_safe_value_tracking_fields(self, net):
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QMetaType
        poles = net.layers["poles"]
        poles.dataProvider().addAttributes([QgsField("_loc_id", QMetaType.Type.QString)])
        poles.updateFields()
        feat = next(poles.getFeatures())
        feat.setAttribute("_loc_id", "truncated-uuid")
        assert eb._safe_value(feat, "_loc_id") == ""          # guard
        feat.setAttribute("_loc_id", "x" * 36)
        assert eb._safe_value(feat, "_loc_id") == "x" * 36
        # stamp cache wins over the attribute
        eb._active_location_id = LOCATION_ID
        eb._load_stamp_cache()[LOCATION_ID] = {
            eb._stamp_cache_key(poles, feat.id()): {"_loc_id": "y" * 36}}
        eb._set_current_layer(poles)
        assert eb._safe_value(feat, "_loc_id") == "y" * 36
        assert eb._get_stamp_value(poles, feat.id(), "_loc_id") == "y" * 36
        eb._active_location_id = ""
        assert eb._get_stamp_value(poles, feat.id(), "_loc_id") == ""

    def test_numbered_fields(self):
        cat = [CategoryField("Route ID"), CategoryField("A"), CategoryField("B")]
        fms = [FieldMapping("a", "fa"), FieldMapping("B", "")]
        feat = QgsFeature()
        top, sub = eb._numbered_fields(cat, fms, None, {"route id"})
        assert top == {"field_1": None, "field_2": None}
        assert sub == {"A": "N/A", "B": "N/A"}
        top, sub = eb._numbered_fields(cat, fms, None, {"route id"},
                                       use_named_keys=False, mapped_only=True)
        assert top == {"field_1": None} and sub == {"field_1": "N/A"}
        assert eb._numbered_fields([], fms, feat, set()) == ({}, {})

    def test_point_wgs84(self, net):
        assert eb._point_wgs84(None, "x", {}) == (0.0, 0.0)
        assert eb._point_wgs84(QgsFeature(), "x", {}) == (0.0, 0.0)
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(5, 6)))
        assert eb._point_wgs84(f, "x", {}) == (5.0, 6.0)

    def test_line_endpoints_wgs84(self):
        zero = ((0.0, 0.0), (0.0, 0.0))
        assert eb._line_endpoints_wgs84(None, "x", {}) == zero
        assert eb._line_endpoints_wgs84(QgsFeature(), "x", {}) == zero
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromMultiPolylineXY([
            [QgsPointXY(1, 1), QgsPointXY(2, 2)],
            [QgsPointXY(3, 3), QgsPointXY(4, 4)]]))
        assert eb._line_endpoints_wgs84(f, "x", {}) == ((1.0, 1.0), (4.0, 4.0))

    def test_line_endpoints_stored_strings_only_while_unmoved(self):
        from label_on_a_cable.core.import_builder import _make_qgs_fields
        f = QgsFeature(_make_qgs_fields(["_origin_lon", "_origin_lat",
                                         "_dest_lon", "_dest_lat"]))
        # geometry within 1 cm of the stored values → stored strings win
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(-6.10000001, 53.1), QgsPointXY(-6.2, 53.20000001)]))
        f.setAttributes(["-6.1", "53.1", "-6.2", "53.2"])
        assert eb._line_endpoints_wgs84(f, "x", {}) == ((-6.1, 53.1), (-6.2, 53.2))
        # destination moved → its geometry wins, origin keeps the stored value
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(-6.1, 53.1), QgsPointXY(-6.3, 53.3)]))
        assert eb._line_endpoints_wgs84(f, "x", {}) == ((-6.1, 53.1), (-6.3, 53.3))
        f.setAttributes(["bad", "53.1", "", ""])
        assert eb._line_endpoints_wgs84(f, "x", {}) == ((-6.1, 53.1), (-6.3, 53.3))
        # no geometry → stored values are all there is
        f.setGeometry(QgsGeometry())
        f.setAttributes(["-6.1", "53.1", "-6.2", "53.2"])
        assert eb._line_endpoints_wgs84(f, "x", {}) == ((-6.1, 53.1), (-6.2, 53.2))

    def test_point_stored_strings_only_while_unmoved(self):
        from label_on_a_cable.core.import_builder import _make_qgs_fields
        f = QgsFeature(_make_qgs_fields(["_origin_lon", "_origin_lat"]))
        f.setAttributes(["-6.123456789", "53.5"])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-6.12345679, 53.5)))
        assert eb._point_wgs84(f, "x", {}) == (-6.123456789, 53.5)   # bit-exact
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-6.2, 53.5)))
        assert eb._point_wgs84(f, "x", {}) == (-6.2, 53.5)           # moved
        f.setGeometry(QgsGeometry())
        assert eb._point_wgs84(f, "x", {}) == (-6.123456789, 53.5)

    def test_point_wgs84_bad_stored_strings(self):
        from label_on_a_cable.core.import_builder import _make_qgs_fields
        f = QgsFeature(_make_qgs_fields(["_origin_lon", "_origin_lat"]))
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(7, 8)))
        f.setAttributes(["oops", "1"])
        assert eb._point_wgs84(f, "x", {}) == (7.0, 8.0)

    def test_raw_json(self):
        from label_on_a_cable.core.import_builder import _make_qgs_fields
        f = QgsFeature(_make_qgs_fields(["_j"]))
        assert eb._get_raw_json(None, "_j") is None
        for raw, expected in (("", None), ("{bad", None), ("[1]", None),
                              ('{"a": 1}', {"a": 1})):
            f.setAttributes([raw])
            assert eb._get_raw_json(f, "_j") == expected

    def test_stop_identity(self):
        lm = LayerMapping(field_mappings=[
            FieldMapping("Unique Asset Identifier", "PoleID"),
            FieldMapping("Actual Asset Name", "PoleName")])
        from label_on_a_cable.core.import_builder import _make_qgs_fields
        f = QgsFeature(_make_qgs_fields(["PoleID", "PoleName"]))
        f.setAttributes(["P9", "Pole 9"])
        assert eb._stop_identity(Stop(original_name="R_x"), f, lm, "R") == ("R_x", "R_x")
        assert eb._stop_identity(Stop(original_name="x"), f, lm, "R") == ("R_x", "x")
        assert eb._stop_identity(Stop(original_name="x"), f, lm, "") == ("x", "x")
        blank = Stop()
        assert eb._stop_identity(blank, f, lm, "R") == ("R_P9", "Pole 9")
        assert eb._stop_identity(blank, f, lm, "") == ("P9", "Pole 9")
        f.setAttributes(["", "Only Name"])
        assert eb._stop_identity(blank, f, lm, "R") == ("R_Only Name", "Only Name")
        f.setAttributes(["Only UID", ""])
        assert eb._stop_identity(blank, f, lm, "R") == ("R_Only UID", "Only UID")
        assert eb._stop_identity(blank, None, None, "R") == ("R_", "")

    def test_misc(self):
        assert eb._float_val("1.5") == 1.5 and eb._float_val(None) == 0.0
        assert eb._fields_fingerprint({"a": 1}, "x") == eb._fields_fingerprint({"a": 1}, "x")
        assert eb._fields_fingerprint({"a": 1}) != eb._fields_fingerprint({"a": 2})
        ts = eb._now_iso()
        assert len(ts) == 24 and ts.endswith("Z") and ts[10] == "T"
        assert eb._get_tracking_id(None, "_loc_id") != eb._get_tracking_id(None, "_loc_id")
        assert eb._get_feature(QgsProject.instance(), "missing", 1) is None

    def test_collect_payload_loc_ids(self):
        p = {"multiLocs": [{"dualLoc": {"loc_id": "d"},
                            "Stops": [{"singleLoc": {"loc_id": "s"}}, {"singleLoc": {}}]},
                           {"dualLoc": {}, "Stops": []}],
             "dual_locs": [{"loc_id": "dd"}, {}],
             "single_locs": [{"attributes": {"loc_id": "a"}}, {"loc_id": "b"}, {}]}
        assert eb._collect_payload_loc_ids(p) == {"d", "s", "dd", "a", "b"}

    def test_extract_expected_stop_counts(self, net):
        from label_on_a_cable.core.import_builder import _make_qgs_fields
        from qgis.core import QgsVectorLayer
        lyr = QgsVectorLayer("LineString?crs=EPSG:4326", "pulled", "memory")
        lyr.dataProvider().addAttributes(
            _make_qgs_fields(["_multi_id", "_stop_ids"]).toList())
        lyr.updateFields()
        feats = []
        m1, m3 = "1" * 36, "3" * 36  # tracking *_id values must be full UUIDs
        for attrs in ([m1, "a,b,,c"], ["", "a"], [m3, ""], ["short", "a,b"]):
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(0, 0), QgsPointXY(1, 1)]))
            f.setAttributes(attrs)
            feats.append(f)
        lyr.dataProvider().addFeatures(feats)
        QgsProject.instance().addMapLayer(lyr)
        ids = [f.id() for f in lyr.getFeatures()]
        routes = [Route(line_layer_id=lyr.id(), line_feature_id=i) for i in ids]
        routes.append(Route(line_layer_id="missing", line_feature_id=1))
        assert eb.extract_expected_stop_counts(routes) == {m1: 3}

    def test_route_endpoint_fallback_when_origin_point_deleted(self, net):
        r = [r for r in net.routes if r.line_name == "CBL-001"][0]
        delete_feature(net.layers["poles"], "PoleID", "P1")
        r.stops[-1].removed = True  # destination ignored
        transforms = eb._build_wgs84_transforms(QgsProject.instance(), net.maps)
        o, d = eb._route_endpoint_coords(r, QgsProject.instance(), transforms)
        assert o == wgs(X0, Y0) and d == wgs(X0 + 100, Y0 + 100)  # line ends
