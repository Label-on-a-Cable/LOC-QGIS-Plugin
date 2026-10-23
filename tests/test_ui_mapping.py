"""Category mapping dialog: layer rows, field combos, presets, accept."""

import pytest
from qgis.core import QgsField, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox

from fake_loc_server import CAT_CHAMBER, CAT_FIBER, CAT_POLE, CAT_STOPS, LOCATION_ID
from helpers import build_network, network_mappings, pump, wait_until
from label_on_a_cable.models.mapping import MappingPreset, load_all_presets, save_preset
from label_on_a_cable.qt_compat import BB_CANCEL, BB_OK
from label_on_a_cable.ui.category_mapping_dialog import CategoryMappingDialog


@pytest.fixture
def layers():
    lyrs = build_network()
    # A polygon layer must never get a row.
    poly = QgsVectorLayer("Polygon?crs=EPSG:2157", "Parcels", "memory")
    QgsProject.instance().addMapLayer(poly)
    # Internal / auxiliary fields are hidden from the field combos.
    pdp = lyrs["poles"].dataProvider()
    pdp.addAttributes([QgsField("_loc_id", QMetaType.Type.QString),
                       QgsField("auxiliary_storage_x", QMetaType.Type.QString)])
    lyrs["poles"].updateFields()
    return lyrs


def open_dialog(qtbot, api, current=None, snap=1.0):
    dlg = CategoryMappingDialog(api, LOCATION_ID, current_mappings=current,
                                snap_tolerance=snap)
    qtbot.addWidget(dlg)
    dlg.show()
    wait_until(lambda: dlg._task is None, message="categories")
    return dlg


def rows(dlg):
    return {r.layer_name: r for r in dlg._layer_rows}


def combo_items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def pick(combo, data):
    idx = combo.findData(data)
    assert idx >= 0, f"{data!r} not in combo"
    combo.setCurrentIndex(idx)


def fields(row):
    return [name for name, _ in row._field_combos]


def map_fields(row, mapping):
    for name, combo in row._field_combos:
        if name in mapping:
            pick(combo, mapping[name])


class TestCategoryMappingDialog:
    def test_rows_and_category_filtering(self, qtbot, authed_api, layers):
        dlg = open_dialog(qtbot, authed_api)
        assert not dlg._status.isVisible()
        assert dlg._buttons.button(BB_OK).isEnabled()
        r = rows(dlg)
        assert set(r) == {"Fiber Cable", "Poles", "Chambers"}
        assert combo_items(r["Poles"]._cat_combo) == ["(unmapped)", "Pole", "Chamber", "Stops"]
        assert combo_items(r["Fiber Cable"]._cat_combo) == ["(unmapped)", "Fiber Cable"]
        assert combo_items(r["Fiber Cable"]._stop_cat_combo) == \
            ["(select stop category)", "Pole", "Chamber", "Stops"]
        assert r["Poles"]._stop_cat_combo is None
        assert r["Fiber Cable"]._stop_type_combo is None
        assert r["Poles"]._qgis_fields == ["PoleID", "PoleName", "Height", "Material"]
        # icons downloaded for Pole + Chamber only
        assert set(dlg._cat_icons) == {CAT_POLE, CAT_CHAMBER}
        assert not r["Poles"]._cat_combo.itemIcon(1).isNull()
        assert r["Poles"]._cat_combo.itemIcon(3).isNull()

    def test_field_combos_follow_category(self, qtbot, authed_api, layers):
        dlg = open_dialog(qtbot, authed_api)
        poles = rows(dlg)["Poles"]
        assert poles._field_combos == []
        pick(poles._cat_combo, CAT_POLE)
        assert fields(poles) == ["Unique Asset Identifier", "Actual Asset Name",
                                 "Height", "Material"]
        assert combo_items(poles._field_combos[0][1]) == \
            ["(unmapped)", "PoleID", "PoleName", "Height", "Material"]
        pick(poles._cat_combo, CAT_STOPS)  # lacks UID → injected first
        assert fields(poles) == ["Unique Asset Identifier", "Actual Asset Name", "Colour"]
        pick(poles._cat_combo, "")
        assert fields(poles) == [] and not poles.is_mapped()

        cable = rows(dlg)["Fiber Cable"]
        pick(cable._cat_combo, CAT_FIBER)
        assert fields(cable) == ["Route ID", "Fiber Count", "Colour", "Destination", "Length"]
        pick(cable._stop_cat_combo, CAT_POLE)  # UID is resolved automatically
        assert [n for n, _ in cable._stop_field_combos] == \
            ["Actual Asset Name", "Height", "Material"]
        pick(cable._stop_cat_combo, CAT_STOPS)
        assert [n for n, _ in cable._stop_field_combos] == ["Actual Asset Name", "Colour"]
        assert cable._stop_cat_combo.findData(CAT_FIBER) == -1  # duals never stops
        cable._stop_cat_combo.setCurrentIndex(0)
        assert cable._stop_field_combos == []

    def test_injected_essentials_for_bare_categories(self, qtbot, authed_api, loc_server, layers):
        loc_server.categories.append({"id": "bare-dual", "name": "Duct", "LOC_type": "dual"})
        loc_server.categories.append({"id": "bare-single", "name": "Marker", "LOC_type": "single"})
        dlg = open_dialog(qtbot, authed_api)
        cable, poles = rows(dlg)["Fiber Cable"], rows(dlg)["Poles"]
        pick(cable._cat_combo, "bare-dual")
        assert fields(cable) == ["Route ID"]
        pick(poles._cat_combo, "bare-single")
        assert fields(poles) == ["Unique Asset Identifier", "Actual Asset Name"]
        pick(cable._stop_cat_combo, "bare-single")
        assert [n for n, _ in cable._stop_field_combos] == ["Actual Asset Name"]

    def test_build_mapping_and_accept(self, qtbot, authed_api, layers):
        dlg = open_dialog(qtbot, authed_api, snap=2.5)
        r = rows(dlg)
        cable, poles, chambers = r["Fiber Cable"], r["Poles"], r["Chambers"]
        pick(cable._cat_combo, CAT_FIBER)
        map_fields(cable, {"Route ID": "AssetID", "Fiber Count": "FiberCount",
                           "Colour": "Colour", "Length": "Length"})
        pick(cable._stop_cat_combo, CAT_STOPS)
        for name, combo in cable._stop_field_combos:
            pick(combo, {"Actual Asset Name": "CableName", "Colour": "Colour"}[name])
        pick(poles._cat_combo, CAT_POLE)
        map_fields(poles, {"Unique Asset Identifier": "PoleID",
                           "Actual Asset Name": "PoleName"})
        pick(poles._stop_type_combo, "ingress_egress")
        poles._include_cb.setChecked(False)
        # chambers left unmapped → not emitted

        got = []
        dlg.mapping_accepted.connect(lambda m, s: got.append((m, s)))
        dlg._snap_spin.setValue(3.5)
        dlg._buttons.button(BB_OK).click()
        (mappings, snap), = got
        assert snap == 3.5 and dlg.result() == dlg.DialogCode.Accepted
        by = {m.layer_name: m for m in mappings}
        assert set(by) == {"Fiber Cable", "Poles"}
        c = by["Fiber Cable"]
        assert (c.layer_id, c.category_id, c.category_name) == \
            (layers["cables"].id(), CAT_FIBER, "Fiber Cable")
        assert c.qgis_field_for("Route ID") == "AssetID"
        assert c.qgis_field_for("Destination") == ""
        assert (c.stop_category_id, c.stop_category_name) == (CAT_STOPS, "Stops")
        assert c.stop_qgis_field_for("Actual Asset Name") == "CableName"
        assert c.default_stop_type == "passthrough"
        p = by["Poles"]
        assert p.default_stop_type == "ingress_egress"
        assert p.include_in_routes is False and p.enabled_for_export is True
        assert p.stop_category_id == "" and p.stop_field_mappings == []

    def test_restores_current_mappings(self, qtbot, authed_api, layers):
        current = network_mappings(layers)
        current[1].enabled_for_export = False
        current[2].include_in_routes = False
        dlg = open_dialog(qtbot, authed_api, current=current)
        r = rows(dlg)
        restored = {lm.layer_id: lm for lm in (row.to_mapping() for row in r.values())}
        for orig in current:
            got = restored[orig.layer_id]
            assert got.category_id == orig.category_id
            assert got.stop_category_id == orig.stop_category_id
            assert got.default_stop_type == orig.default_stop_type
            assert got.include_in_routes == orig.include_in_routes
            assert got.enabled_for_export == orig.enabled_for_export
            for fm in orig.field_mappings:
                assert got.qgis_field_for(fm.loc_field) == fm.qgis_field
            for fm in orig.stop_field_mappings:
                assert got.stop_qgis_field_for(fm.loc_field) == fm.qgis_field
        assert r["Poles"]._detail_widget.isHidden()      # export off → collapsed

    def test_export_bulk_toggles(self, qtbot, authed_api, layers):
        dlg = open_dialog(qtbot, authed_api)
        dlg._deselect_all_export()
        assert all(not row._export_cb.isChecked() and row._detail_widget.isHidden()
                   for row in dlg._layer_rows)
        dlg._select_all_export()
        assert all(row._export_cb.isChecked() and not row._detail_widget.isHidden()
                   for row in dlg._layer_rows)

    def test_presets_save_load_delete(self, qtbot, authed_api, layers, monkeypatch, msgbox):
        dlg = open_dialog(qtbot, authed_api, current=network_mappings(layers), snap=4.0)
        answers = iter([("  Ratoath  ", True), ("", True), ("x", False)])
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: next(answers)))
        dlg._save_preset()
        dlg._save_preset()   # blank name → ignored
        dlg._save_preset()   # cancelled → ignored
        presets = load_all_presets()
        assert list(presets) == ["Ratoath"]
        assert presets["Ratoath"].snap_tolerance == 4.0
        assert len(presets["Ratoath"].layer_mappings) == 3
        assert dlg._preset_combo.currentText() == "Ratoath"

        for row in dlg._layer_rows:
            row.reset()
        dlg._snap_spin.setValue(1.0)
        dlg._load_preset()
        assert dlg._snap_spin.value() == 4.0
        assert all(row.is_mapped() for row in dlg._layer_rows)

        msgbox.answers = [QMessageBox.StandardButton.No]
        dlg._delete_preset()
        assert "Ratoath" in load_all_presets()
        dlg._delete_preset()  # default answer: Yes
        assert load_all_presets() == {}
        assert dlg._preset_combo.count() == 0
        dlg._load_preset()    # empty combo → no-op
        dlg._delete_preset()

    def test_preset_matches_by_layer_name_and_resets_others(self, qtbot, authed_api, layers):
        maps = network_mappings(layers)
        for m in maps:
            m.layer_id = "from-another-project-" + m.layer_name
        save_preset(MappingPreset("Other project", maps[:2]))  # no chambers, no tolerance
        current = network_mappings(layers)
        dlg = open_dialog(qtbot, authed_api, current=current, snap=7.0)
        assert dlg._preset_combo.currentText() == "Other project"
        dlg._load_preset()
        r = rows(dlg)
        assert r["Fiber Cable"].to_mapping().category_id == CAT_FIBER
        assert r["Poles"].to_mapping().category_id == CAT_POLE
        assert not r["Chambers"].is_mapped()     # not in preset → reset
        assert dlg._snap_spin.value() == 7.0     # legacy preset keeps tolerance

    def test_load_missing_preset_name(self, qtbot, authed_api, layers):
        dlg = open_dialog(qtbot, authed_api)
        dlg._preset_combo.addItem("ghost")
        dlg._load_preset()  # not in settings → no-op

    def test_category_fetch_error(self, qtbot, api, layers):
        dlg = open_dialog(qtbot, api)  # no token
        assert "Invalid or expired token" in dlg._status.text()
        assert not dlg._buttons.button(BB_OK).isEnabled()
        assert dlg._layer_rows == []

    def test_cancel_while_loading(self, qtbot, authed_api, loc_server, layers):
        loc_server.delay["organizations/categories"] = 0.3
        dlg = CategoryMappingDialog(authed_api, LOCATION_ID)
        qtbot.addWidget(dlg)
        dlg._buttons.button(BB_CANCEL).click()
        assert dlg._task is None and dlg.result() == dlg.DialogCode.Rejected
        pump(500)
        dlg._on_categories_loaded()
        assert dlg._layer_rows == []
