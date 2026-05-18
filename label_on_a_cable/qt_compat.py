"""Qt5/Qt6 compatibility shims for enum values that moved in Qt6."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QMessageBox,
)
from qgis.core import QgsTask, QgsWkbTypes

# QDialogButtonBox button enums
try:
    BB_OK = QDialogButtonBox.StandardButton.Ok
    BB_CANCEL = QDialogButtonBox.StandardButton.Cancel
except AttributeError:
    BB_OK = QDialogButtonBox.Ok
    BB_CANCEL = QDialogButtonBox.Cancel

# QMessageBox button enums
try:
    MB_YES = QMessageBox.StandardButton.Yes
    MB_NO = QMessageBox.StandardButton.No
except AttributeError:
    MB_YES = QMessageBox.Yes
    MB_NO = QMessageBox.No

# Qt.DockWidgetArea
try:
    LEFT_DOCK = Qt.DockWidgetArea.LeftDockWidgetArea
    RIGHT_DOCK = Qt.DockWidgetArea.RightDockWidgetArea
except AttributeError:
    LEFT_DOCK = Qt.LeftDockWidgetArea
    RIGHT_DOCK = Qt.RightDockWidgetArea

# Qt.ItemDataRole
try:
    USER_ROLE = Qt.ItemDataRole.UserRole
except AttributeError:
    USER_ROLE = Qt.UserRole

# Qt.ItemFlag
try:
    ITEM_IS_SELECTABLE = Qt.ItemFlag.ItemIsSelectable
except AttributeError:
    ITEM_IS_SELECTABLE = Qt.ItemIsSelectable

# Qt.WindowModality
try:
    NON_MODAL = Qt.WindowModality.NonModal
except AttributeError:
    NON_MODAL = Qt.NonModal

# Qt.Orientation
try:
    VERTICAL = Qt.Orientation.Vertical
except AttributeError:
    VERTICAL = Qt.Vertical

# Qt.AlignmentFlag
try:
    ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
    ALIGN_TOP = Qt.AlignmentFlag.AlignTop
except AttributeError:
    ALIGN_CENTER = Qt.AlignCenter
    ALIGN_TOP = Qt.AlignTop

# Qt.AspectRatioMode / TransformationMode
try:
    KEEP_ASPECT_RATIO = Qt.AspectRatioMode.KeepAspectRatio
    SMOOTH_TRANSFORM = Qt.TransformationMode.SmoothTransformation
except AttributeError:
    KEEP_ASPECT_RATIO = Qt.KeepAspectRatio
    SMOOTH_TRANSFORM = Qt.SmoothTransformation

# QHeaderView.ResizeMode
try:
    HV_STRETCH = QHeaderView.ResizeMode.Stretch
    HV_RESIZE_TO_CONTENTS = QHeaderView.ResizeMode.ResizeToContents
    HV_FIXED = QHeaderView.ResizeMode.Fixed
except AttributeError:
    HV_STRETCH = QHeaderView.Stretch
    HV_RESIZE_TO_CONTENTS = QHeaderView.ResizeToContents
    HV_FIXED = QHeaderView.Fixed

# QAbstractItemView enums
try:
    AIV_SELECT_ROWS = QAbstractItemView.SelectionBehavior.SelectRows
    AIV_SINGLE_SELECTION = QAbstractItemView.SelectionMode.SingleSelection
    AIV_NO_EDIT_TRIGGERS = QAbstractItemView.EditTrigger.NoEditTriggers
except AttributeError:
    AIV_SELECT_ROWS = QAbstractItemView.SelectRows
    AIV_SINGLE_SELECTION = QAbstractItemView.SingleSelection
    AIV_NO_EDIT_TRIGGERS = QAbstractItemView.NoEditTriggers

# QFrame.Shape
try:
    FRAME_STYLED_PANEL = QFrame.Shape.StyledPanel
    FRAME_NO_FRAME = QFrame.Shape.NoFrame
except AttributeError:
    FRAME_STYLED_PANEL = QFrame.StyledPanel
    FRAME_NO_FRAME = QFrame.NoFrame

# QgsTask.Flag
try:
    TASK_CAN_CANCEL = QgsTask.Flag.CanCancel
except AttributeError:
    TASK_CAN_CANCEL = QgsTask.CanCancel

# QgsWkbTypes geometry type enums
try:
    from qgis.core import Qgis as _Qgis
    GEOM_POINT = _Qgis.GeometryType.Point
    GEOM_LINE = _Qgis.GeometryType.Line
except AttributeError:
    GEOM_POINT = QgsWkbTypes.PointGeometry
    GEOM_LINE = QgsWkbTypes.LineGeometry
