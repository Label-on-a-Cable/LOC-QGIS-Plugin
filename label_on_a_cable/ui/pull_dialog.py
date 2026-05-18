"""Pull from LOC dialog.

Fetches existing LOC data for the active location, shows a summary,
and imports into new QGIS memory layers on confirmation.

Flow:
1. Dialog opens → FetchLocsTask fires immediately.
2. Shows "Fetching LOC data for {location}..." while loading.
3. On completion → displays summary (single/dual/multi counts).
4. "Import" / "Cancel" buttons.
5. On Import → calls import_builder.build_layers(), emits signal.
"""

from typing import List, Optional, Set

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
)
from qgis.core import QgsApplication, QgsVectorLayer

from ..qt_compat import BB_OK, BB_CANCEL
from ..core.import_builder import (
    build_layers, extract_multi_stop_counts, import_summary,
)
from ..core.tasks import FetchLocsTask
from ..models.category import Category
from ..models.mapping import LayerMapping
from ..services.api_client import ApiClient


class PullDialog(QDialog):
    """Modal dialog: fetch LOC data, preview summary, import layers.

    Emits ``import_complete(list, list, set, dict)`` with
    (layers, mappings, pulled_ids, multi_stop_counts) when the user
    clicks Import.
    """

    import_complete = pyqtSignal(list, list, set, dict)

    def __init__(
        self,
        api_client: ApiClient,
        location_id: str,
        location_name: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Pull from LOC")
        self.setMinimumWidth(420)

        self._api = api_client
        self._location_id = location_id
        self._location_name = location_name
        self._task: Optional[FetchLocsTask] = None

        # Stored after fetch completes
        self._locs_data: Optional[dict] = None
        self._categories: List[Category] = []

        self._build_ui()
        self._start_fetch()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Header
        loc_text = self._location_name or self._location_id
        self._header = QLabel(f"<b>Location:</b> {loc_text}")
        root.addWidget(self._header)

        # Status / summary area
        self._summary_group = QGroupBox("LOC Data")
        self._summary_layout = QVBoxLayout(self._summary_group)
        self._status_label = QLabel("Fetching LOC data...")
        self._summary_layout.addWidget(self._status_label)
        root.addWidget(self._summary_group)

        # Buttons
        self._buttons = QDialogButtonBox(
            BB_OK | BB_CANCEL
        )
        self._import_btn = self._buttons.button(BB_OK)
        self._import_btn.setText("Import")
        self._import_btn.setEnabled(False)
        self._buttons.accepted.connect(self._on_import)
        self._buttons.rejected.connect(self._on_cancel)
        root.addWidget(self._buttons)

    def _on_cancel(self):
        """Cancel any running task and close."""
        if self._task is not None:
            self._task.taskCompleted.disconnect(self._on_fetch_done)
            self._task.taskTerminated.disconnect(self._on_fetch_done)
            self._task = None
        self.reject()

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _start_fetch(self):
        self._task = FetchLocsTask(self._api, self._location_id)
        self._task.taskCompleted.connect(self._on_fetch_done)
        self._task.taskTerminated.connect(self._on_fetch_done)
        QgsApplication.taskManager().addTask(self._task)

    def _on_fetch_done(self):
        task = self._task
        self._task = None
        if task is None:
            return

        if task.error:
            self._status_label.setText(task.error)
            self._status_label.setStyleSheet("color: red;")
            return

        self._locs_data = task.locs_data
        self._categories = task.categories

        # Show summary
        summary = import_summary(self._locs_data)
        self._status_label.setVisible(False)

        form = QFormLayout()

        # Singles by category
        single_by_cat = summary.get("single_by_cat", {})
        if single_by_cat:
            for cat_name, count in single_by_cat.items():
                form.addRow(f"Single LOCs ({cat_name}):", QLabel(str(count)))
        else:
            form.addRow("Single LOCs:", QLabel(str(summary.get("single", 0))))

        # Duals by category
        dual_by_cat = summary.get("dual_by_cat", {})
        if dual_by_cat:
            for cat_name, count in dual_by_cat.items():
                form.addRow(f"Dual LOCs ({cat_name}):", QLabel(str(count)))
        else:
            form.addRow("Dual LOCs:", QLabel(str(summary.get("dual", 0))))

        form.addRow("Multi LOCs:", QLabel(str(summary.get("multi", 0))))
        form.addRow("Stop single LOCs:", QLabel(str(summary.get("stop_singles", 0))))

        total_label = QLabel(f"<b>{summary.get('total', 0)}</b>")
        form.addRow("Total LOC objects:", total_label)

        self._summary_layout.addLayout(form)

        if summary.get("total", 0) > 0:
            self._import_btn.setEnabled(True)
        else:
            info = QLabel("No LOC data found for this location.")
            info.setStyleSheet("color: gray;")
            self._summary_layout.addWidget(info)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def _on_import(self):
        if self._locs_data is None:
            return

        layers, mappings, pulled_ids = build_layers(
            self._locs_data, self._categories
        )
        multi_stop_counts = extract_multi_stop_counts(self._locs_data)
        self.import_complete.emit(
            layers, mappings, pulled_ids, multi_stop_counts,
        )
        self.accept()
