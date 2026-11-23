"""1.7.1 pullnow2 against a real server: Pull now onto the user's own
layers, the toolbar Pull prompt, and a pulled project reopened after QGIS
closed (Bugs 37, 38, 40, 41).  Capture mode only: Pull now is offered for
field captures, which only a capture-mode project keeps.

    LOC_LIVE_CAPTURE=on tests/run_live_videos.sh --allow-push -k own_layers

Each scenario backs the location up and restores it (the ``mx`` fixture).
"""

import copy

import pytest
from qgis.core import QgsProject, QgsVectorFileWriter, QgsVectorLayer

from helpers import pump, wait_until
from test_live_matrix import (  # noqa: F401  (fixtures)
    STAMP, WANT, _capture_in_the_field, diff, expect, lv, mx, records,
    server_rec, standalone_points, target,
)
from test_live_videos import (
    act, open_location, preview_then_cancel, pull, sign_in, title,
)

pytestmark = [pytest.mark.live, pytest.mark.live_write]


def _own_copy(lv, p, folder):
    """The pulled layer holding *p*, saved as the user's own GeoPackage
    (no plugin columns, the asset-ID column only as wide as its longest
    value) and mapped in its place; the pulled layer stays, unmapped."""
    from qgis.core import QgsField, QgsFields, QgsFeature
    src, lm = p["layer"], p["lm"]
    keep = [f for f in src.fields() if not f.name().startswith("_")]
    uid_q = lm.qgis_field_for("Unique Asset Identifier")
    width = max(8, max(len(str(f[uid_q])) for f in src.getFeatures()))
    fields = QgsFields()
    for f in keep:
        nf = QgsField(f)
        if f.name() == uid_q:
            nf.setLength(width)
        fields.append(nf)
    mem = QgsVectorLayer(f"Point?crs={src.crs().authid()}", "own", "memory")
    mem.dataProvider().addAttributes(fields.toList())
    mem.updateFields()
    feats = []
    for f in src.getFeatures():
        g = QgsFeature(mem.fields())
        g.setGeometry(f.geometry())
        g.setAttributes([f[k.name()] for k in keep])
        feats.append(g)
    mem.dataProvider().addFeatures(feats)
    path = str(folder / "own_structures.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, path, QgsProject.instance().transformContext(), opts)
    assert err[0] == QgsVectorFileWriter.NoError, err
    own = QgsVectorLayer(path, src.name().replace(" (LOC)", ""), "ogr")
    assert own.isValid() and own.featureCount() == src.featureCount()
    QgsProject.instance().addMapLayer(own)
    mine = copy.deepcopy(lm)
    mine.layer_id, mine.layer_name = own.id(), own.name()
    maps = [mine if m.layer_id == lm.layer_id else m for m in lv.plugin.layer_mappings]
    lv.plugin._on_mapping_accepted(maps, lv.plugin.snap_tolerance)
    assert QgsProject.instance().write()
    act(lv, f"Action: save {src.name()} as my own GeoPackage layer “{own.name()}” "
            f"(asset ID column {width} characters) and map it instead")
    return own, uid_q, width


def _layers_and_mapping(lv):
    return (sorted(QgsProject.instance().mapLayers()),
            [m.to_dict() for m in lv.plugin.layer_mappings])


@title("Own layers · field captures, Pull now onto my GeoPackage layer → push")
def test_live_own_layers_pull_now(mx):
    if not mx.capture:
        pytest.skip("Pull now is offered in capture mode")
    lv = mx.lv
    pull(lv)
    (p,) = standalone_points(lv, mx.baseline, 1)
    seen = preview_then_cancel(lv)              # the location as a payload
    own, uid_q, width = _own_copy(lv, p, lv.tmp)

    mx.push("first push from my own layer", answers=[])
    c0 = mx.server()
    mx.check("LOC after the first push from my own layer", mx.baseline, c0)

    short, long_ = f"Q{STAMP}", f"QA-OWN-LONG-{STAMP}-" + "X" * width
    _capture_in_the_field(mx, seen["payload"], short, p["lid"])
    _capture_in_the_field(mx, seen["payload"], long_, p["lid"])
    s1 = mx.server()
    both = [server_rec(s1, u) for u in (short, long_)]
    mx.step("two field captures reached LOC", f"{short}, {long_}",
            ", ".join(r["unique_asset_id"] for r in both if r) or "none", all(both))

    before = _layers_and_mapping(lv)
    from qgis.PyQt.QtWidgets import QMessageBox
    lv.msgbox.answers = [QMessageBox.StandardButton.Yes]          # Pull now
    n = len(lv.msgbox.calls)
    act(lv, "Action: click Push to LOC → answer Pull now")
    lv.plugin.actions["push"].trigger()
    wait_until(lambda: lv.plugin._push_state_task is None, timeout_ms=300000)
    pump(30)
    calls = lv.msgbox.calls[n:]
    stale = [c for c in calls if c[1] == "Map Is Behind LOC"]
    added = [c for c in calls if c[1] == "Field Captures Added"]
    on_layer = {str(f[uid_q]) for f in own.getFeatures()}
    text = added[0][2] if added else ""
    ok = (bool(stale) and short in stale[0][2] and long_ in stale[0][2]
          and f"{short}  ({own.name()})" in text
          and f"{long_}: {own.name()}.{uid_q} is limited to {width} characters" in text
          and short in on_layer and not any(u.startswith("QA-OWN-LONG") for u in on_layer)
          and _layers_and_mapping(lv) == before
          and not diff(s1, mx.server())["changed"])
    mx.step("Pull now onto my own layer",
            f"{short} added to {own.name()}; {long_} refused (column too narrow); "
            "layers and mapping unchanged; nothing pushed",
            ("added" if short in on_layer else "NOT added") + f"; layers/mapping "
            + ("unchanged" if _layers_and_mapping(lv) == before else "CHANGED"),
            ok, [text[:400]])

    # The long capture is still unknown to the project: warned, kept.
    mx.push("push after Pull now", want=(0, 0),
            answers=[QMessageBox.StandardButton.Ignore], want_stale=True)
    s2 = mx.server()
    mx.check("LOC after the push: both captures as captured", s1, s2)
    rec = server_rec(s2, long_)
    mx.step("refused capture untouched in LOC", long_,
            rec["unique_asset_id"] if rec else "missing", bool(rec))

    # Toolbar Pull over my own layer: asked first; Cancel adds nothing.
    lv.msgbox.answers = [QMessageBox.StandardButton.Cancel]
    n = len(lv.msgbox.calls)
    lv.plugin.actions["pull"].trigger()
    pump(10)
    ask = [c for c in lv.msgbox.calls[n:] if c[1] == "Pull a Full Copy?"]
    mx.step("toolbar Pull over my own layer asks first",
            "Pull a Full Copy? (uses your own layers); Cancel adds nothing",
            "asked" if ask else "not asked",
            bool(ask) and "uses your own layers" in ask[0][2]
            and _layers_and_mapping(lv) == before, [ask[0][2][:300]] if ask else [])


@title("Reopened pulled project · temporary layers come back empty → push refused")
def test_live_own_layers_reopened_pulled_project(mx):
    import label_on_a_cable
    from label_on_a_cable.core import export_builder as eb
    from label_on_a_cable.models.mapping import LayerMapping
    lv = mx.lv
    pull(lv)
    mapping = [LayerMapping.from_dict(m.to_dict()) for m in lv.plugin.layer_mappings]
    path = QgsProject.instance().absoluteFilePath()
    assert QgsProject.instance().write()
    act(lv, "Action: save the project and close QGIS")
    old = lv.plugin
    old.unload()
    old.unload = lambda: None                   # the fixture unloads it again
    QgsProject.instance().clear()
    eb._stamp_cache = None
    eb._stamp_cache_path = None
    eb._layer_identity.clear()
    lv.plugin = label_on_a_cable.classFactory(lv.iface)
    lv.plugin.initGui()
    QgsProject.instance().read(path)
    act(lv, "Action: start QGIS and reopen the project")
    counts = {l.name(): l.featureCount() for l in QgsProject.instance().mapLayers().values()}
    mx.step("pulled layers after reopening", "empty (temporary layers)",
            f"{sum(counts.values())} feature(s) in {len(counts)} layer(s)",
            sum(counts.values()) == 0)
    if not lv.plugin.auth.is_logged_in:
        sign_in(lv)
    open_location(lv)
    lv.plugin._on_mapping_accepted(mapping, lv.plugin.snap_tolerance)
    s0 = mx.server()
    lv.msgbox.answers = []
    n = len(lv.msgbox.calls)
    act(lv, "Action: click Push to LOC")
    lv.plugin.actions["push"].trigger()
    pump(30)
    wait_until(lambda: lv.plugin._push_state_task is None, timeout_ms=300000)
    titles = [c[1] for c in lv.msgbox.calls[n:]]
    mx.step("push from the emptied layers", "Pulled Layers Are Empty; nothing sent",
            ", ".join(titles) or "no prompt",
            titles == ["Pulled Layers Are Empty"] and not diff(s0, mx.server())["changed"])
