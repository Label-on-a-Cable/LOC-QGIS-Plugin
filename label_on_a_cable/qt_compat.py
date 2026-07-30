"""Qt5/Qt6 compatibility shims for enum values that moved in Qt6.

Scoped enum names (e.g. ``Qt.ItemDataRole.UserRole``) work on both
PyQt5 >= 5.12 (QGIS 3.22+) and PyQt6 (QGIS 4.x), so they are used
directly.  Only enums that moved between QGIS API versions (not Qt
versions) need a runtime fallback, done via ``getattr`` with string
names so static Qt6-compatibility checkers do not flag the legacy
spellings.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QMessageBox,
)
from qgis.core import Qgis, QgsTask, QgsWkbTypes

# QDialogButtonBox button enums
BB_OK = QDialogButtonBox.StandardButton.Ok
BB_CANCEL = QDialogButtonBox.StandardButton.Cancel

# QMessageBox button enums
MB_YES = QMessageBox.StandardButton.Yes
MB_NO = QMessageBox.StandardButton.No

# Qt.DockWidgetArea
LEFT_DOCK = Qt.DockWidgetArea.LeftDockWidgetArea
RIGHT_DOCK = Qt.DockWidgetArea.RightDockWidgetArea

# Qt.ItemDataRole
USER_ROLE = Qt.ItemDataRole.UserRole

# Qt.ItemFlag
ITEM_IS_SELECTABLE = Qt.ItemFlag.ItemIsSelectable

# Qt.TextInteractionFlag
TEXT_SELECTABLE = Qt.TextInteractionFlag.TextSelectableByMouse

# Qt.WindowModality
NON_MODAL = Qt.WindowModality.NonModal

# Qt.Orientation
VERTICAL = Qt.Orientation.Vertical

# Qt.AlignmentFlag
ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
ALIGN_TOP = Qt.AlignmentFlag.AlignTop

# Qt.AspectRatioMode / TransformationMode
KEEP_ASPECT_RATIO = Qt.AspectRatioMode.KeepAspectRatio
SMOOTH_TRANSFORM = Qt.TransformationMode.SmoothTransformation

# QHeaderView.ResizeMode
HV_STRETCH = QHeaderView.ResizeMode.Stretch
HV_RESIZE_TO_CONTENTS = QHeaderView.ResizeMode.ResizeToContents
HV_FIXED = QHeaderView.ResizeMode.Fixed

# QAbstractItemView enums
AIV_SELECT_ROWS = QAbstractItemView.SelectionBehavior.SelectRows
AIV_SINGLE_SELECTION = QAbstractItemView.SelectionMode.SingleSelection
AIV_NO_EDIT_TRIGGERS = QAbstractItemView.EditTrigger.NoEditTriggers

# QFrame.Shape
FRAME_STYLED_PANEL = QFrame.Shape.StyledPanel
FRAME_NO_FRAME = QFrame.Shape.NoFrame

# QgsTask.Flag
TASK_CAN_CANCEL = QgsTask.Flag.CanCancel

# Qgis message levels (scoped since QGIS 3.x, unscoped removed in 4.x)
MSG_INFO = Qgis.MessageLevel.Info
MSG_WARNING = Qgis.MessageLevel.Warning

# Geometry type enums: Qgis.GeometryType exists from QGIS 3.30; older
# releases (down to our 3.22 minimum) only have the QgsWkbTypes enums.
if hasattr(Qgis, "GeometryType"):
    GEOM_POINT = Qgis.GeometryType.Point
    GEOM_LINE = Qgis.GeometryType.Line
else:
    GEOM_POINT = getattr(QgsWkbTypes, "PointGeometry")
    GEOM_LINE = getattr(QgsWkbTypes, "LineGeometry")
