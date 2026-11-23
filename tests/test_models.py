"""Domain models: Category, Location, User, Route/Stop, mappings + presets."""

import json

from qgis.core import QgsSettings

from label_on_a_cable.models.category import Category, CategoryField, _parse_image
from label_on_a_cable.models.location import GlobalIdentifier, Location, Project
from label_on_a_cable.models.mapping import (
    FieldMapping, LayerMapping, MappingPreset,
    delete_preset, load_all_presets, save_preset,
)
from label_on_a_cable.models.route import Route, Stop, StopType
from label_on_a_cable.models.user import Organization, User


# ----------------------------------------------------------------------
# Category
# ----------------------------------------------------------------------

class TestCategory:
    def test_field_from_api_shapes(self):
        assert CategoryField.from_api("Height") == CategoryField("Height", False)
        assert CategoryField.from_api({"name": "UID", "required": 1}) == \
            CategoryField("UID", True)
        assert CategoryField.from_api(42) == CategoryField()

    def test_parse_image_prefers_signed_url(self):
        assert _parse_image("http://a/icon.png") == "http://a/icon.png"
        assert _parse_image({"url": "raw", "signedUrl": "signed"}) == "signed"
        assert _parse_image({"url": "raw"}) == "raw"
        assert _parse_image({"black": "b", "green": "g"}) == "b"
        assert _parse_image({"src": "s"}) == "s"
        assert _parse_image({"url": 5, "signedUrl": ""}) == ""
        assert _parse_image(None) == ""

    def test_from_api_full(self):
        cat = Category.from_api({
            "id": 7, "name": "Fiber", "LOC_type": "Dual",
            "fields": ["Route ID", {"name": "Count"}],
            "destination_fields": [{"name": "Destination"}],
            "image": {"signedUrl": "x"},
        })
        assert cat.category_id == "7"
        assert cat.is_dual and not cat.is_single
        assert [f.name for f in cat.fields] == ["Route ID", "Count"]
        assert cat.destination_fields[0].name == "Destination"
        assert cat.image == "x"

    def test_from_api_fallback_keys_and_nulls(self):
        cat = Category.from_api({"_id": "abc", "loc_type": "single",
                                 "fields": None, "destination_fields": None})
        assert cat.category_id == "abc"
        assert cat.is_single
        assert cat.fields == [] and cat.destination_fields == []

    def test_list_from_api(self):
        cats = Category.list_from_api({"categories": [{"id": 1}, {"id": 2}]})
        assert [c.category_id for c in cats] == ["1", "2"]
        assert Category.list_from_api([{"id": 1}]) == []
        assert Category.list_from_api({}) == []


# ----------------------------------------------------------------------
# Location
# ----------------------------------------------------------------------

class TestLocation:
    def test_nested_project_shape(self):
        loc = Location.from_api({
            "_id": "L1", "name": "Ratoath", "longitude": "-6.4",
            "latitude": 53.5, "radius": None,
            "Project": {"_id": "P1", "name": "Proj",
                        "GlobalIdentifier": {"id": "G1", "name": "GID"}},
        })
        assert loc.location_id == "L1"
        assert loc.longitude == -6.4 and loc.latitude == 53.5
        assert loc.radius == 0.0
        assert loc.project.project_id == "P1"
        assert loc.project.global_identifier.name == "GID"

    def test_flat_v1_shape(self):
        loc = Location.from_api({"id": "L2", "name": "X", "project_id": "p",
                                 "project_name": "PN", "gid": "g", "gid_name": "GN"})
        assert loc.project == Project("p", "PN", GlobalIdentifier("g", "GN"))

    def test_flat_v1_gid_falls_back_to_gid_name(self):
        loc = Location.from_api({"id": "L3", "gid_name": "Only Name"})
        assert loc.project.global_identifier.gid_id == "Only Name"

    def test_project_without_gid(self):
        p = Project.from_api({"id": 3, "GlobalIdentifier": None})
        assert p.project_id == "3" and p.global_identifier == GlobalIdentifier()

    def test_list_from_api_shapes(self):
        assert len(Location.list_from_api({"locations": [{"id": 1}]})) == 1
        assert len(Location.list_from_api([{"id": 1}, {"id": 2}])) == 2
        assert Location.list_from_api("garbage") == []


# ----------------------------------------------------------------------
# User
# ----------------------------------------------------------------------

def test_user_from_api():
    u = User.from_api({"user_id": 9, "fullName": "Ann", "email": "a@b",
                       "role": "admin", "enabled2fa": 1, "org_id": 4,
                       "organization": {"name": "Org", "lite": True}})
    assert u.user_id == "9" and u.full_name == "Ann"
    assert u.enabled_2fa is True
    assert u.organization == Organization("4", "Org", True)


def test_user_from_api_minimal():
    u = User.from_api({"organization": None})
    assert u == User(organization=Organization())


# ----------------------------------------------------------------------
# Route / Stop
# ----------------------------------------------------------------------

class TestRoute:
    def _route(self):
        return Route(line_name="R", stops=[
            Stop(original_name="O", stop_type=StopType.ORIGIN),
            Stop(original_name="S1", stop_type=StopType.PASSTHROUGH, stop_number=1),
            Stop(original_name="S2", stop_type=StopType.INGRESS, stop_number=2,
                 removed=True),
            Stop(original_name="D", stop_type=StopType.DESTINATION),
        ])

    def test_stop_display_name_defaults(self):
        assert Stop(original_name="abc").display_name == "abc"
        assert Stop(original_name="abc", display_name="x").display_name == "x"

    def test_endpoint_flags(self):
        assert Stop(stop_type=StopType.ORIGIN).is_endpoint
        assert Stop(stop_type=StopType.DESTINATION).is_endpoint
        assert not Stop(stop_type=StopType.EGRESS).is_endpoint

    def test_stop_collections(self):
        r = self._route()
        assert [s.original_name for s in r.active_stops] == ["O", "S1", "D"]
        assert [s.original_name for s in r.intermediate_stops] == ["S1"]
        assert r.active_stop_count == 1
        assert not r.is_unmatched
        assert Route().is_unmatched

    def test_status_and_unique_ids(self):
        r = self._route()
        assert r.status == "generated"
        r.has_edits = True
        assert r.status == "edited"
        assert Route().route_id != Route().route_id


# ----------------------------------------------------------------------
# Mappings + presets
# ----------------------------------------------------------------------

def _mapping():
    return LayerMapping(
        layer_id="lid", layer_name="Cables", category_id="cat",
        category_name="Fiber",
        field_mappings=[FieldMapping("Route ID", "AssetID"),
                        FieldMapping("Colour", "")],
        default_stop_type="ingress_egress",
        stop_category_id="stop", stop_category_name="Stops",
        stop_field_mappings=[FieldMapping("Actual Asset Name", "Name")],
        include_in_routes=False, enabled_for_export=False,
    )


class TestLayerMapping:
    def test_field_lookups_are_case_insensitive(self):
        lm = _mapping()
        assert lm.qgis_field_for("route id") == "AssetID"
        assert lm.qgis_field_for("missing") == ""
        assert lm.stop_qgis_field_for("ACTUAL ASSET NAME") == "Name"
        assert lm.stop_qgis_field_for("nope") == ""

    def test_first_mapped_field(self):
        assert _mapping().first_mapped_qgis_field() == "AssetID"
        lm = LayerMapping(field_mappings=[FieldMapping("a", "")])
        assert lm.first_mapped_qgis_field() == ""

    def test_dict_round_trip(self):
        lm = _mapping()
        assert LayerMapping.from_dict(lm.to_dict()) == lm

    def test_from_dict_defaults(self):
        lm = LayerMapping.from_dict({})
        assert lm.default_stop_type == "passthrough"
        assert lm.include_in_routes and lm.enabled_for_export


class TestPresets:
    def test_preset_dict_round_trip(self):
        p = MappingPreset("P", [_mapping()], snap_tolerance=2.5)
        d = p.to_dict()
        assert d["snap_tolerance"] == 2.5
        assert MappingPreset.from_dict(d) == p

    def test_legacy_preset_without_tolerance(self):
        d = MappingPreset("Old", []).to_dict()
        assert "snap_tolerance" not in d
        assert MappingPreset.from_dict(d).snap_tolerance is None
        assert MappingPreset.from_dict({"snap_tolerance": "bad"}).snap_tolerance is None

    def test_save_load_delete(self):
        assert load_all_presets() == {}
        save_preset(MappingPreset("A", [_mapping()], 1.0))
        save_preset(MappingPreset("B", []))
        save_preset(MappingPreset("A", [], 3.0))  # overwrite
        presets = load_all_presets()
        assert sorted(presets) == ["A", "B"]
        assert presets["A"].snap_tolerance == 3.0
        delete_preset("A")
        delete_preset("does-not-exist")
        assert list(load_all_presets()) == ["B"]

    def test_corrupt_settings_ignored(self):
        QgsSettings().setValue("LOC/mapping_presets", "{not json")
        assert load_all_presets() == {}

    def test_settings_format_is_json(self):
        save_preset(MappingPreset("A", [_mapping()]))
        raw = QgsSettings().value("LOC/mapping_presets")
        assert json.loads(raw)["A"]["layer_mappings"][0]["layer_name"] == "Cables"
