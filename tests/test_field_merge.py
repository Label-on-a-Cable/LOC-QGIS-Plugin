"""Pull now's placement of field captures: what it refuses, and why."""

import json
from qgis.core import QgsProject

from fake_loc_server import CAT_CHAMBER, CAT_FIBER, CAT_POLE, CAT_STOPS, LOCATION_ID
from helpers import build_network, network_mappings, save_project
from label_on_a_cable.core.field_merge import place_field_captures
from label_on_a_cable.models.category import Category


def _cats():
    from fake_loc_server import default_categories
    return {c["id"]: Category.from_api(c) for c in default_categories("http://x")}


def _single(lid, uid, cat=CAT_POLE, **kw):
    return {"loc_id": lid, "unique_asset_id": uid, "category": cat, "fields": {},
            "origin_longitude": -6.43, "origin_latitude": 53.57, **kw}


def test_reasons_for_captures_left_in_loc(tmp_path):
    layers = build_network()
    save_project(tmp_path)
    maps = network_mappings(layers)
    data = {
        "singleLOCs": [
            _single("a", "P1"),                         # already on Poles
            _single("b", ""),                           # no asset id
            _single("c", "S-1", cat=CAT_STOPS),         # no point layer for Stops
            _single("d", "CH-9", cat=CAT_CHAMBER),      # Chambers being edited
            _single("e", "NEW-1", fields={"Height": "not a number?"}),
        ],
        "dualLOCs": [{"loc_id": "f", "unique_asset_id": "CBL-9", "category": CAT_FIBER}],
    }
    layers["chambers"].startEditing()
    try:
        rep = place_field_captures(data, list("abcdef"), maps, _cats(), LOCATION_ID)
    finally:
        layers["chambers"].rollBack()
    why = dict(rep.skipped)
    assert why["P1"] == "Poles already has a feature with this asset ID"
    assert why["(no asset ID)"] == "has no asset ID to match on"
    assert why["S-1"] == "no point layer is mapped to Stops"
    assert why["CH-9"].startswith("Chambers is being edited")
    assert why["CBL-9"] == "cable captured in the field; LOC keeps it"
    assert rep.placed == [("NEW-1", "Poles")] and rep.placed_ids == ["e"]
    from label_on_a_cable.core.export_builder import known_loc_ids
    assert known_loc_ids(LOCATION_ID) == {"e"}


def test_nothing_wanted_changes_nothing(tmp_path):
    layers = build_network()
    save_project(tmp_path)
    n = layers["poles"].featureCount()
    rep = place_field_captures({"singleLOCs": [_single("a", "X")]}, [],
                               network_mappings(layers), _cats(), LOCATION_ID)
    assert (rep.placed, rep.skipped) == ([], []) and layers["poles"].featureCount() == n


# -- file-backed layers with typed, length-limited fields (probe, 15 Nov) --

import pytest
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsPointXY, QgsVectorFileWriter, QgsVectorLayer)
import test_plugin_e2e as e
from test_plugin_e2e import ui, plugin  # noqa: F401  (fixtures)
from helpers import ITM, X0, Y0


def _typed_poles(lyrs, fmt, tmp_path):
    src = lyrs["poles"]
    mem = QgsVectorLayer(f"Point?crs={ITM}&field=PoleID:string(6)&field=PoleName:string(20)"
                         "&field=Height:integer&field=Material:string(10)", "p", "memory")
    feats = []
    for f in src.getFeatures():
        g = QgsFeature(mem.fields())
        g.setGeometry(f.geometry())
        g.setAttributes([f["PoleID"], f["PoleName"], int(f["Height"]), f["Material"]])
        feats.append(g)
    mem.dataProvider().addFeatures(feats)
    path = str(tmp_path / ("poles.gpkg" if fmt == "GPKG" else "poles.shp"))
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = fmt
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsProject.instance().transformContext(), opts)
    QgsProject.instance().removeMapLayer(src.id())
    layer = QgsVectorLayer(path, "Poles", "ogr")
    QgsProject.instance().addMapLayer(layer)
    lyrs["poles"] = layer
    return layer


def _capture(ui, uid, dx, fields, name=""):
    t = QgsCoordinateTransform(QgsCoordinateReferenceSystem(ITM),
                               QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    p = t.transform(QgsPointXY(X0 + dx, Y0 + 2000))
    ui.server.loc_state()["single"][uid.lower()] = {
        "loc_id": uid.lower(), "unique_asset_id": uid, "actual_asset_name": name,
        "category": CAT_POLE, "fields": fields,
        "origin_longitude": p.x(), "origin_latitude": p.y()}


@pytest.mark.parametrize("fmt", ["GPKG", "ESRI Shapefile"])
def test_pull_now_never_writes_a_value_the_layer_cannot_hold(ui, fmt, tmp_path):
    """A capture's asset ID longer than the layer's field was left empty,
    and the push renamed the LOC record to "Feature_6", rounded 7.5 to 8
    and blanked a long Material.  Such a capture now stays out of the
    layer, named with the reason, and LOC's record is untouched."""
    lyrs = build_network()
    poles = _typed_poles(lyrs, fmt, tmp_path)
    ui.server.capture_projects.add("p-1")
    save_project(ui.tmp)
    e.login(ui)
    e.select_location(ui)
    e.map_categories(ui, network_mappings(lyrs))
    e.generate(ui)
    e.push(ui)
    _capture(ui, "F1", 2000, {"Height": "7", "Material": "Wood"}, "Field 1")
    _capture(ui, "F2", 2100, {"Height": "7.5", "Material": "Wood"}, "Field 2")
    _capture(ui, "FIELD-LONG", 2200, {"Height": "7"}, "Field 3")
    _capture(ui, "F4", 2300, {"Height": "7", "Material": "Composite-Fibreglass"}, "Field 4")
    before = {u: json.loads(json.dumps(r)) for u, r in ui.server.loc_state()["single"].items()}
    ui.msgbox.answers = [e.YES]                                   # Pull now
    e.start_push(ui)
    (added,) = [c for c in ui.msgbox.calls if c[1] == "Field Captures Added"]
    assert "F1  (Poles)" in added[2]
    assert "F2: Poles.Height cannot hold this value exactly ('7.5')" in added[2]
    assert "FIELD-LONG: Poles.PoleID is limited to 6 characters" in added[2]
    assert "F4: Poles.Material is limited to 10 characters" in added[2]
    assert [f["PoleID"] for f in poles.getFeatures() if str(f["PoleID"]).startswith("F")] == ["F1"]
    e.generate(ui)
    ui.msgbox.answers = [e.PUSH_ANYWAY]
    e.push(ui)
    assert e.last_preview_stats(ui).get("Created LOCs") == "0"
    after = ui.server.loc_state()["single"]
    for uid in ("f2", "field-long", "f4"):
        assert after[uid]["unique_asset_id"] == before[uid]["unique_asset_id"]
        assert after[uid]["fields"] == before[uid]["fields"]
    assert after["f1"]["unique_asset_id"] == "F1"
    assert {k: str(v) for k, v in after["f1"]["fields"].items()} == {"Height": "7", "Material": "Wood"}


def test_capture_on_a_cable_becomes_a_named_stop(ui):
    """A capture with no name standing on a cable joins the route at the
    next Generate; its stop is named from the asset ID ("CBL-001_None"
    before)."""
    ui.server.capture_projects.add("p-1")
    e.design_project(ui)
    e.generate(ui)
    e.push(ui)
    t = QgsCoordinateTransform(QgsCoordinateReferenceSystem(ITM),
                               QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    p = t.transform(QgsPointXY(X0 + 75, Y0))                    # on CBL-001
    ui.server.loc_state()["single"]["on-cable"] = {
        "loc_id": "on-cable", "unique_asset_id": "FIELD-ON", "category": CAT_POLE,
        "fields": {}, "origin_longitude": p.x(), "origin_latitude": p.y()}
    ui.msgbox.answers = [e.YES]
    e.start_push(ui)
    e.generate(ui)
    e.push(ui)
    (route,) = ui.server.loc_state()["multi"].values()
    names = [s["singleLoc"]["unique_asset_id"]
             for s in sorted(route["Stops"], key=lambda s: s["stopNumber"])]
    assert names == ["CBL-001_Pole 2", "CBL-001_FIELD-ON", "CBL-001_Chamber 1_IN",
                     "CBL-001_Chamber 1_OUT", "CBL-001_Pole 3"]
    assert ui.server.loc_state()["single"]["on-cable"]["unique_asset_id"] == "FIELD-ON"


def test_capture_on_a_cable_joins_the_route_in_a_pulled_project(ui):
    """Plan 26c on a pulled project: the pulled layers carry the hidden
    position columns, and a pulled structure off a route was never made a
    stop ("LOC already decided").  A capture placed by Pull now is not
    such a decision, so the next Generate puts it on the cable."""
    ui.server.capture_projects.add("p-1")
    e.design_project(ui)
    e.generate(ui)
    e.push(ui)
    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    save_project(ui.tmp, "pulled.qgz")
    e.pull(ui)
    t = QgsCoordinateTransform(QgsCoordinateReferenceSystem(ITM),
                               QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    p = t.transform(QgsPointXY(X0 + 75, Y0))                    # on CBL-001
    ui.server.loc_state()["single"]["on-cable"] = {
        "loc_id": "on-cable", "unique_asset_id": "FIELD-ON", "category": CAT_POLE,
        "fields": {}, "origin_longitude": p.x(), "origin_latitude": p.y()}
    ui.msgbox.answers = [e.YES]                                 # Pull now
    e.start_push(ui)
    e.generate(ui)
    e.push(ui)
    (route,) = ui.server.loc_state()["multi"].values()
    names = [s["singleLoc"]["unique_asset_id"]
             for s in sorted(route["Stops"], key=lambda s: s["stopNumber"])]
    assert "CBL-001_FIELD-ON" in names
    assert names.index("CBL-001_FIELD-ON") == names.index("CBL-001_Pole 2") + 1
    assert not any(n.endswith("_None") for n in names)


def test_a_full_pull_forgets_pull_now_captures(ui):
    """After a full pull LOC's routes are what the project holds again:
    a pulled structure off a route stays off it."""
    from label_on_a_cable.core.field_merge import (
        field_capture_ids, remember_field_captures)
    remember_field_captures(["x-1"])
    assert field_capture_ids() == {"x-1"}
    e.design_project(ui)
    e.generate(ui)
    e.push(ui)
    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    save_project(ui.tmp, "pulled.qgz")
    remember_field_captures(["x-1"])
    e.pull(ui)
    assert field_capture_ids() == set()


def test_toolbar_pull_over_own_layers_asks_and_keeps_the_mapping(ui):
    """Pull on a project mapped to the user's own layers moved the mapping
    onto a new "(LOC)" copy without a word.  It now asks, points at Pull
    now, and saves the mapping as a preset that loads back."""
    from label_on_a_cable.models.mapping import load_all_presets
    layers = e.design_project(ui)
    e.generate(ui)
    e.push(ui)
    mine = [m.to_dict() for m in ui.plugin.layer_mappings]
    n_layers = len(QgsProject.instance().mapLayers())
    ui.msgbox.answers = [e.CANCEL]
    ui.plugin.actions["pull"].trigger()
    (ask,) = [c for c in ui.msgbox.calls if c[1] == "Pull a Full Copy?"]
    assert "Category Mapping uses your own layers" in ask[2] and "Pull now" in ask[2]
    assert len(QgsProject.instance().mapLayers()) == n_layers
    assert [m.to_dict() for m in ui.plugin.layer_mappings] == mine
    ui.msgbox.answers = [e.YES]                                   # Pull a Copy
    e.pull(ui)
    assert len(QgsProject.instance().mapLayers()) == 2 * n_layers
    (name,) = [n for n in load_all_presets() if "before pull" in n]
    preset = load_all_presets()[name]
    assert [m.to_dict() for m in preset.layer_mappings] == mine
    assert {m.layer_id for m in preset.layer_mappings} == {l.id() for l in layers.values()}


def test_second_pull_on_a_pulled_project_says_it_adds_a_copy(ui):
    from qgis.core import QgsCoordinateReferenceSystem
    e.design_project(ui)
    e.generate(ui)
    e.push(ui)
    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    save_project(ui.tmp, "pulled.qgz")
    e.pull(ui)
    assert not [c for c in ui.msgbox.calls if c[1] == "Pull a Full Copy?"]   # first pull: no ask
    ui.msgbox.answers = [e.CANCEL]
    ui.plugin.actions["pull"].trigger()
    (ask,) = [c for c in ui.msgbox.calls if c[1] == "Pull a Full Copy?"]
    assert "already holds a pull of this location" in ask[2]
