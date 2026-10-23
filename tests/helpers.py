"""Test-data builders: QGIS layers, mappings, categories, task waits."""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsTask,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QElapsedTimer, QMetaType

from fake_loc_server import (
    CAT_CHAMBER, CAT_FIBER, CAT_POLE, CAT_STOPS, LOCATION_ID,
    default_categories, default_locations,
)

ITM = "EPSG:2157"  # Irish Transverse Mercator (metres)

# Ratoath-ish coordinates in ITM
X0, Y0 = 700000.0, 750000.0


def pump(ms=0):
    """Process Qt events for roughly *ms* milliseconds."""
    timer = QElapsedTimer()
    timer.start()
    QCoreApplication.processEvents()
    while timer.elapsed() < ms:
        QCoreApplication.processEvents()


def wait_until(predicate, timeout_ms=15000, message="condition"):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        if timer.elapsed() > timeout_ms:
            raise AssertionError(f"Timed out waiting for {message}")
        QCoreApplication.processEvents()
    QCoreApplication.processEvents()


def wait_task(task, timeout_ms=15000):
    """Wait for a QgsTask to finish and its signals to be delivered."""
    done = (QgsTask.TaskStatus.Complete, QgsTask.TaskStatus.Terminated)
    wait_until(lambda: task.status() in done, timeout_ms, f"task {task.description()}")
    pump(20)


def _layer(geom, name, fields, crs=ITM):
    layer = QgsVectorLayer(f"{geom}?crs={crs}", name, "memory")
    layer.dataProvider().addAttributes(
        [QgsField(f, QMetaType.Type.QString) for f in fields]
    )
    layer.updateFields()
    return layer


def add_points(layer, rows):
    """rows: [(x, y, {field: value})]"""
    feats = []
    for x, y, attrs in rows:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        for k, v in attrs.items():
            f.setAttribute(k, v)
        feats.append(f)
    ok, _ = layer.dataProvider().addFeatures(feats)
    assert ok
    layer.updateExtents()
    return layer


def add_lines(layer, rows):
    """rows: [([(x, y), ...], {field: value})]"""
    feats = []
    for coords, attrs in rows:
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]))
        for k, v in attrs.items():
            f.setAttribute(k, v)
        feats.append(f)
    ok, _ = layer.dataProvider().addFeatures(feats)
    assert ok
    layer.updateExtents()
    return layer


def build_network(project_crs=ITM, add_to_project=True):
    """A small FTTH design.

    Cable CBL-001 runs east 100 m then north 100 m::

        P1 ── P2 ── C1 (corner, ingress/egress chamber)
                    │
                    P3
                    │
                    P4

    Cable CBL-002 sits away from every structure (unmatched route).
    P5 is a standalone pole on no cable.
    """
    project = QgsProject.instance()
    project.setCrs(QgsCoordinateReferenceSystem(project_crs))

    cables = _layer("LineString", "Fiber Cable",
                    ["AssetID", "CableName", "FiberCount", "Colour",
                     "Length", "DestName"])
    add_lines(cables, [
        ([(X0, Y0), (X0 + 100, Y0), (X0 + 100, Y0 + 100)],
         {"AssetID": "CBL-001", "CableName": "Feeder 1", "FiberCount": "144",
          "Colour": "Blue", "Length": "200"}),
        ([(X0 + 500, Y0), (X0 + 600, Y0)],
         {"AssetID": "CBL-002", "CableName": "Drop 2", "FiberCount": "12",
          "Colour": "Black", "Length": "100"}),
    ])

    poles = _layer("Point", "Poles", ["PoleID", "PoleName", "Height", "Material"])
    add_points(poles, [
        (X0, Y0, {"PoleID": "P1", "PoleName": "Pole 1", "Height": "9", "Material": "Wood"}),
        (X0 + 50, Y0 + 0.5, {"PoleID": "P2", "PoleName": "Pole 2", "Height": "9", "Material": "Wood"}),
        (X0 + 100, Y0 + 50, {"PoleID": "P3", "PoleName": "Pole 3", "Height": "10", "Material": "Steel"}),
        (X0 + 100, Y0 + 100, {"PoleID": "P4", "PoleName": "Pole 4", "Height": "10", "Material": "Steel"}),
        (X0 + 3000, Y0 + 3000, {"PoleID": "P5", "PoleName": "Pole 5", "Height": "8", "Material": "Wood"}),
    ])

    chambers = _layer("Point", "Chambers", ["ChamberID", "ChamberName", "Depth"])
    add_points(chambers, [
        (X0 + 100, Y0, {"ChamberID": "CH1", "ChamberName": "Chamber 1", "Depth": "1.2"}),
    ])

    if add_to_project:
        for lyr in (cables, poles, chambers):
            project.addMapLayer(lyr)
    return {"cables": cables, "poles": poles, "chambers": chambers}


def categories():
    from label_on_a_cable.models.category import Category
    return [Category.from_api(c) for c in default_categories("http://x")]


def categories_by_id():
    return {c.category_id: c for c in categories()}


def location(idx=0):
    from label_on_a_cable.models.location import Location
    return Location.from_api(default_locations()[idx])


def network_mappings(layers):
    """LayerMappings equivalent to what a user sets in the mapping dialog."""
    from label_on_a_cable.models.mapping import FieldMapping as FM, LayerMapping
    return [
        LayerMapping(
            layer_id=layers["cables"].id(), layer_name="Fiber Cable",
            category_id=CAT_FIBER, category_name="Fiber Cable",
            field_mappings=[
                FM("Route ID", "AssetID"), FM("Fiber Count", "FiberCount"),
                FM("Colour", "Colour"), FM("Destination", ""),
                FM("Length", "Length"),
            ],
            stop_category_id=CAT_STOPS, stop_category_name="Stops",
            stop_field_mappings=[FM("Actual Asset Name", "CableName"),
                                 FM("Colour", "Colour")],
        ),
        LayerMapping(
            layer_id=layers["poles"].id(), layer_name="Poles",
            category_id=CAT_POLE, category_name="Pole",
            field_mappings=[
                FM("Unique Asset Identifier", "PoleID"),
                FM("Actual Asset Name", "PoleName"),
                FM("Height", "Height"), FM("Material", "Material"),
            ],
        ),
        LayerMapping(
            layer_id=layers["chambers"].id(), layer_name="Chambers",
            category_id=CAT_CHAMBER, category_name="Chamber",
            field_mappings=[
                FM("Unique Asset Identifier", "ChamberID"),
                FM("Actual Asset Name", "ChamberName"),
                FM("Depth", "Depth"),
            ],
            default_stop_type="ingress_egress",
        ),
    ]


def save_project(tmp_path, name="network.qgz"):
    path = tmp_path / name
    assert QgsProject.instance().write(str(path)), "project save failed"
    return path


def seed_server(server, tmp_path, keep_layers=False, extra_poles=()):
    """Store the standard network on *server* as if pushed earlier.

    Returns the payload that was applied.  Unless *keep_layers*, the
    source layers are removed again so the project only contains what a
    later Pull imports.  *extra_poles* ([(x, y, attrs)]) are added to the
    poles layer first, e.g. structures sharing a coordinate.
    """
    from label_on_a_cable.core import export_builder as eb
    from label_on_a_cable.core.route_generator import generate_routes

    layers = build_network()
    if extra_poles:
        add_points(layers["poles"], list(extra_poles))
    maps = network_mappings(layers)
    save_project(tmp_path, "seed.qgz")
    payload = eb.build_payload(generate_routes(maps), maps, categories_by_id(),
                               location(), "u-0001", "org-0001")
    server._apply_push(LOCATION_ID, payload)
    if not keep_layers:
        for lyr in layers.values():
            QgsProject.instance().removeMapLayer(lyr.id())
    eb._stamp_cache = None
    eb._stamp_cache_path = None
    return payload


def pulled_network(tmp_path, mutate=None, extra_poles=()):
    """Pull the standard network back into memory layers.

    *mutate(locs_data)* may edit the server answer before import.
    Returns a namespace with layers (by name), maps, ids, data, cats, loc.
    """
    from types import SimpleNamespace
    from fake_loc_server import FakeLocServer
    from label_on_a_cable.core import import_builder as ib

    srv = FakeLocServer()
    try:
        seed_server(srv, tmp_path, extra_poles=extra_poles)
        data = srv.fetch_shape(LOCATION_ID)
    finally:
        srv.httpd.server_close()
    if mutate:
        mutate(data)
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(ITM))
    layers, maps, ids = ib.build_layers(data, categories())
    for lyr in layers:
        QgsProject.instance().addMapLayer(lyr)
    return SimpleNamespace(layers={l.name(): l for l in layers}, maps=maps,
                           ids=ids, data=data, cats=categories_by_id(),
                           loc=location())


def feature_where(layer, field, value):
    return [f for f in layer.getFeatures() if f[field] == value][0]


_POINT_COLOURS = ["#e08a1e", "#3d7dd8", "#2e9e4f", "#b04fc8"]


def show_map(iface, extent_layers=None):
    """Style + label every project layer and draw them on the canvas."""
    from qgis.core import (
        QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings,
        QgsSingleSymbolRenderer, QgsTextFormat, QgsVectorLayerSimpleLabeling,
        QgsRectangle,
    )
    from qgis.PyQt.QtGui import QColor
    from label_on_a_cable.qt_compat import GEOM_LINE, GEOM_POINT
    project = QgsProject.instance()
    layers = [l for l in project.mapLayers().values()
              if isinstance(l, QgsVectorLayer)]
    lines = [l for l in layers if l.geometryType() == GEOM_LINE]
    points = [l for l in layers if l.geometryType() == GEOM_POINT]
    for lyr in lines:
        lyr.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple(
            {"color": "#7a4f7f", "width": "0.9"})))
    for i, lyr in enumerate(points):
        lyr.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": _POINT_COLOURS[i % 4],
             "outline_color": "#333333", "size": "3.4"})))
    for lyr in layers:
        names = lyr.fields().names()
        field = next((f for f in ("PoleName", "ChamberName", "Actual Asset Name",
                                  "AssetID") if f in names), None)
        if lyr in lines and "Route ID" in names:
            field = "Route ID"
        if not field:
            continue
        s = QgsPalLayerSettings()
        s.fieldName = field
        fmt = QgsTextFormat()
        fmt.setSize(9)
        fmt.setColor(QColor("#1b2230"))
        buf = fmt.buffer()
        buf.setEnabled(True)
        buf.setSize(1)
        fmt.setBuffer(buf)
        s.setFormat(fmt)
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
        lyr.setLabelsEnabled(True)
        lyr.triggerRepaint()

    canvas = iface.mapCanvas()
    canvas.setDestinationCrs(project.crs())
    canvas.setCanvasColor(QColor("#ffffff"))
    canvas.setLayers(points + lines)
    extent = QgsRectangle()
    from qgis.core import QgsCoordinateTransform
    for lyr in (extent_layers or lines or layers):
        xf = QgsCoordinateTransform(lyr.crs(), project.crs(), project)
        r = xf.transformBoundingBox(lyr.extent())
        if extent.isEmpty():
            extent = r
        else:
            extent.combineExtentWith(r)
    if not extent.isEmpty():
        extent.scale(1.25)
        canvas.setExtent(extent)
    refresh_map(iface)


def refresh_map(iface):
    canvas = iface.mapCanvas()
    canvas.refreshAllLayers()
    canvas.refresh()
    pump(50)
    canvas.waitWhileRendering()
    pump(50)


__all__ = [
    "CAT_CHAMBER", "CAT_FIBER", "CAT_POLE", "CAT_STOPS", "LOCATION_ID",
    "ITM", "X0", "Y0", "pump", "wait_until", "wait_task", "add_points",
    "add_lines", "build_network", "categories", "categories_by_id",
    "location", "network_mappings", "save_project",
]
