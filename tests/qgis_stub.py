"""Minimal ``qgis`` stubs so core modules import outside QGIS.

The plugin's core modules import ``qgis.core`` at module scope, so a
plain path-load (the trick ``test_grouping`` uses) is not enough for
them.  ``install()`` registers just enough of the QGIS/Qt surface in
``sys.modules`` for the pure-Python logic in ``core.export_builder`` to
be exercised by unit tests.

Only attribute lookup, enum identity and a couple of container types are
modelled — nothing here pretends to behave like real QGIS.
"""

import sys
import types


class _Node:
    """Stands in for a QGIS/Qt namespace or scoped-enum member.

    Attribute access returns another node, so arbitrarily deep lookups
    such as ``Qt.ItemDataRole.UserRole`` resolve.  Nodes compare and hash
    by their dotted path, which is what makes enum sentinels like
    ``Qgis.GeometryType.Point`` behave as distinct stable values.
    """

    __slots__ = ("_path",)

    def __init__(self, path: str):
        self._path = path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Node(f"{self._path}.{name}")

    def __call__(self, *args, **kwargs):
        return _Node(f"{self._path}()")

    def __eq__(self, other):
        return isinstance(other, _Node) and other._path == self._path

    def __hash__(self):
        return hash(self._path)

    def __repr__(self):
        return f"<stub {self._path}>"


class QgsVectorLayer:
    """Just enough layer for coverage auditing: id, name, geom, count."""

    def __init__(self, layer_id="", name="", geometry_type=None, features=0):
        self._id = layer_id
        self._name = name
        self._geom = geometry_type
        self._count = features

    def id(self):
        return self._id

    def name(self):
        return self._name

    def geometryType(self):
        return self._geom

    def featureCount(self):
        return self._count

    def source(self):
        return f"memory:{self._id}"


class QgsProject:
    """Singleton stand-in whose layer table tests can populate."""

    _instance = None

    def __init__(self):
        self._layers = {}

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = cls()
        return cls._instance

    def add(self, layer):
        self._layers[layer.id()] = layer
        return layer

    def mapLayer(self, layer_id):
        return self._layers.get(layer_id)

    def absoluteFilePath(self):
        return ""

    def transformContext(self):
        return _Node("QgsProject.transformContext()")


class QgsMessageLog:
    @staticmethod
    def logMessage(*args, **kwargs):
        return None


class QgsSettings:
    _store = {}

    def value(self, key, default=None):
        return self._store.get(key, default)

    def setValue(self, key, value):
        self._store[key] = value

    def remove(self, key):
        self._store.pop(key, None)


def install():
    """Register the stub modules in ``sys.modules`` (idempotent)."""
    if "qgis" in sys.modules:
        return

    qgis = types.ModuleType("qgis")
    core = types.ModuleType("qgis.core")
    pyqt = types.ModuleType("qgis.PyQt")
    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtwidgets = types.ModuleType("qgis.PyQt.QtWidgets")
    qtgui = types.ModuleType("qgis.PyQt.QtGui")

    for name in (
        "Qgis", "QgsCoordinateReferenceSystem", "QgsCoordinateTransform",
        "QgsFeature", "QgsFields", "QgsField", "QgsGeometry", "QgsPointXY",
        "QgsTask", "QgsWkbTypes", "QgsApplication", "QgsEditorWidgetSetup",
        "QgsAttributeTableConfig",
    ):
        setattr(core, name, _Node(name))
    core.QgsProject = QgsProject
    core.QgsVectorLayer = QgsVectorLayer
    core.QgsMessageLog = QgsMessageLog
    core.QgsSettings = QgsSettings

    for name in ("Qt", "QVariant", "QMetaType", "QSize", "pyqtSignal",
                 "QThread", "QTimer"):
        setattr(qtcore, name, _Node(name))
    for name in (
        "QAbstractItemView", "QDialogButtonBox", "QFrame", "QHeaderView",
        "QMessageBox", "QDialog", "QLabel", "QPushButton", "QVBoxLayout",
        "QHBoxLayout", "QFormLayout", "QGroupBox", "QWidget",
    ):
        setattr(qtwidgets, name, _Node(name))
    for name in ("QIcon", "QPixmap", "QColor"):
        setattr(qtgui, name, _Node(name))

    pyqt.QtCore = qtcore
    pyqt.QtWidgets = qtwidgets
    pyqt.QtGui = qtgui
    qgis.core = core
    qgis.PyQt = pyqt

    sys.modules.update({
        "qgis": qgis,
        "qgis.core": core,
        "qgis.PyQt": pyqt,
        "qgis.PyQt.QtCore": qtcore,
        "qgis.PyQt.QtWidgets": qtwidgets,
        "qgis.PyQt.QtGui": qtgui,
    })
