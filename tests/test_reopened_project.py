"""A pulled project reopened after QGIS closed: its "(LOC)" layers are
temporary and come back empty (probe, 15 Nov 2026)."""
from qgis.core import QgsCoordinateReferenceSystem, QgsProject

import label_on_a_cable
import test_plugin_e2e as e
from test_plugin_e2e import ui, plugin  # noqa: F401  (fixtures)
from helpers import ITM, pump, save_project
from label_on_a_cable.core import export_builder as eb
from label_on_a_cable.models.mapping import LayerMapping


def _restart(ui):
    """Save, close QGIS (a new plugin instance, caches dropped), reopen."""
    path = QgsProject.instance().absoluteFilePath()
    assert QgsProject.instance().write()
    old = ui.plugin
    old.unload()
    old.unload = lambda: None                   # the fixture unloads it again
    QgsProject.instance().clear()
    eb._stamp_cache = None
    eb._stamp_cache_path = None
    eb._layer_identity.clear()
    ui.plugin = label_on_a_cable.classFactory(ui.iface)
    ui.plugin.initGui()
    QgsProject.instance().read(path)
    if not ui.plugin.auth.is_logged_in:
        e.login(ui)
    e.select_location(ui)


def _pulled_project(ui):
    ui.server.capture_projects.add("p-1")
    e.design_project(ui)
    e.generate(ui)
    e.push(ui)
    QgsProject.instance().clear()
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    save_project(ui.tmp, "pulled.qgz")
    e.pull(ui)
    return [LayerMapping.from_dict(m.to_dict()) for m in ui.plugin.layer_mappings]


def test_push_from_emptied_pulled_layers_is_refused(ui):
    """Mapped again after the restart, the empty layers read as 12 LOCs
    "removed in QGIS" and one Yes binned the location."""
    mapping = _pulled_project(ui)
    _restart(ui)
    assert all(l.featureCount() == 0 for l in QgsProject.instance().mapLayers().values())
    e.map_categories(ui, mapping)
    ui.plugin.actions["generate"].trigger()
    pump(10)
    posts = len(ui.server.requests_to("LOCs/plugin-v3", "POST"))
    ui.msgbox.calls.clear()
    ui.plugin.actions["push"].trigger()
    pump(10)
    assert [c[1] for c in ui.msgbox.calls] == ["Pulled Layers Are Empty"]
    assert "Pull again" in ui.msgbox.calls[0][2]
    assert len(ui.server.requests_to("LOCs/plugin-v3", "POST")) == posts
    assert ui.plugin._push_state_task is None and ui.server.bin == []


def test_layer_emptied_on_purpose_in_this_session_still_pushes(ui):
    """Deleting every feature of a layer pulled in this session is a real
    delete, not a lost layer."""
    _pulled_project(ui)
    ch = next(l for l in QgsProject.instance().mapLayers().values()
              if l.name() == "Chamber (LOC)")
    ch.dataProvider().deleteFeatures([f.id() for f in ch.getFeatures()])
    ui.msgbox.answers = [e.YES]
    e.push(ui)
    assert "Pulled Layers Are Empty" not in ui.msgbox.titles()
    assert any(c[1] == "LOCs Will Be Deleted" for c in ui.msgbox.calls)
