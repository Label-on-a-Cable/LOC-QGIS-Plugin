"""Workspace sidebar — LOC Location selector dock widget.

Displays a tree: Global Identifier → Project → Location.
Fetches locations via QgsTask on first show / refresh.
Emits ``location_selected`` when the user picks a location.
"""

from typing import List, Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication

from ..qt_compat import LEFT_DOCK, RIGHT_DOCK, USER_ROLE, ITEM_IS_SELECTABLE
from ..core.grouping import group_locations
from ..core.tasks import FetchLocationsTask
from ..models.location import Location
from ..services.api_client import ApiClient


class LocationSidebar(QDockWidget):
    """Dock widget for selecting a LOC Location."""

    location_selected = pyqtSignal(object)  # emits a Location
    auth_failed = pyqtSignal()  # emitted when token is rejected (401/403)

    def __init__(self, api_client: ApiClient, parent=None):
        super().__init__("LOC Workspace", parent)
        self.api = api_client
        self._task: Optional[FetchLocationsTask] = None
        self._locations: List[Location] = []

        self.setAllowedAreas(LEFT_DOCK | RIGHT_DOCK)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        # Header row with refresh button
        header = QHBoxLayout()
        header.addWidget(QLabel("Select a Location:"))
        header.addStretch()
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.clicked.connect(self.fetch_locations)
        header.addWidget(self._btn_refresh)
        layout.addLayout(header)

        # Description
        desc = QLabel(
            "Select a location to work with. "
            "Locations are grouped by Global Identifier and Project."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(desc)

        # Status label (loading / error)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        layout.addWidget(self._status)

        # Selection feedback
        self._selection_label = QLabel()
        self._selection_label.setWordWrap(True)
        self._selection_label.setStyleSheet(
            "color: #2a7d2e; font-weight: bold; font-size: 11px;"
        )
        self._selection_label.setVisible(False)
        layout.addWidget(self._selection_label)

        # Tree: GID → Project → Location
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._tree)

        self.setWidget(container)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def fetch_locations(self):
        """Kick off a background fetch of all locations."""
        if self._task is not None:
            return  # already fetching

        self._btn_refresh.setEnabled(False)
        self._status.setText("Loading locations...")
        self._status.setStyleSheet("")
        self._status.setVisible(True)

        self._task = FetchLocationsTask(self.api)
        self._task.taskCompleted.connect(self._on_fetch_done)
        self._task.taskTerminated.connect(self._on_fetch_done)
        QgsApplication.taskManager().addTask(self._task)

    def closeEvent(self, event):
        """Cancel any running task before closing."""
        if self._task is not None:
            self._task.taskCompleted.disconnect(self._on_fetch_done)
            self._task.taskTerminated.disconnect(self._on_fetch_done)
            self._task = None
        super().closeEvent(event)

    def _on_fetch_done(self):
        task = self._task
        self._task = None
        if task is None:
            return
        self._btn_refresh.setEnabled(True)

        if task.error:
            if task.auth_failed:
                self._status.setText(
                    "Session expired — please sign in again.")
                self._status.setStyleSheet("color: red;")
                self._status.setVisible(True)
                self.auth_failed.emit()
                return
            self._status.setText(task.error)
            self._status.setStyleSheet("color: red;")
            self._status.setVisible(True)
            return

        self._locations = task.locations
        self._status.setVisible(False)
        self._populate_tree()

    # ------------------------------------------------------------------
    # Tree building
    # ------------------------------------------------------------------

    def _populate_tree(self):
        self._tree.clear()

        if not self._locations:
            self._status.setText(
                "No locations found. Check your account permissions."
            )
            self._status.setStyleSheet("color: gray;")
            self._status.setVisible(True)
            return

        for gid_name, projects in group_locations(self._locations):
            count = sum(len(locs) for _, locs in projects)
            display = f"{gid_name} ({count} location{'s' if count != 1 else ''})"
            gid_item = QTreeWidgetItem([display])
            gid_item.setFlags(gid_item.flags() & ~ITEM_IS_SELECTABLE)
            self._tree.addTopLevelItem(gid_item)

            for project_name, locs in projects:
                n = len(locs)
                project_item = QTreeWidgetItem(
                    [f"{project_name} ({n} location{'s' if n != 1 else ''})"]
                )
                project_item.setFlags(
                    project_item.flags() & ~ITEM_IS_SELECTABLE
                )
                gid_item.addChild(project_item)

                for loc in locs:
                    loc_item = QTreeWidgetItem([loc.name])
                    loc_item.setData(0, USER_ROLE, loc)
                    project_item.addChild(loc_item)

                project_item.setExpanded(True)

            gid_item.setExpanded(True)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int):
        loc = item.data(0, USER_ROLE)
        if isinstance(loc, Location):
            self.location_selected.emit(loc)
            self._selection_label.setText(
                f"Selected: {loc.name}."
            )
            self._selection_label.setVisible(True)
