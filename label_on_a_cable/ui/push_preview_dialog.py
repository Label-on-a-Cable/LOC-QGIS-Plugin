"""Push preview dialog.

Shows a local payload summary and server-side dry-run stats before
the user commits to pushing data to LOC.

Flow:
1. Dialog opens → local summary shown immediately.
2. PushPreviewTask fires in the background (``?stats=true``).
3. On completion, server stats populate a second panel.
4. User clicks **Push** → ``push_confirmed`` signal emitted.
5. User clicks **Cancel** → dialog closes.
"""

from typing import Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
)
from qgis.core import QgsApplication

from ..qt_compat import BB_OK, BB_CANCEL
from ..core.tasks import PushPreviewTask
from ..services.api_client import ApiClient


class PushPreviewDialog(QDialog):
    """Modal dialog: payload summary + server preview before push.

    Emits ``push_confirmed(dict)`` with the payload when the user
    clicks Push.
    """

    push_confirmed = pyqtSignal(dict)  # payload

    def __init__(
        self,
        api_client: ApiClient,
        payload: dict,
        local_summary: dict,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Push Preview")
        self.setMinimumWidth(400)

        self._api = api_client
        self._payload = payload
        self._task: Optional[PushPreviewTask] = None

        self._build_ui(local_summary)
        self._fetch_server_preview()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, summary: dict):
        root = QVBoxLayout(self)

        # Location header
        loc_label = QLabel(f"<b>Location:</b> {summary.get('location', '—')}")
        root.addWidget(loc_label)

        # Local summary group
        local_group = QGroupBox("What Will Be Pushed")
        local_layout = QFormLayout(local_group)
        local_layout.addRow(
            "Cable routes (with stops):",
            QLabel(str(summary.get("routes_with_stops", 0))),
        )
        local_layout.addRow(
            "Cable routes (direct):",
            QLabel(str(summary.get("routes_without_stops", 0))),
        )
        local_layout.addRow(
            "Total cable routes:",
            QLabel(str(summary.get("total_routes", 0))),
        )
        local_layout.addRow(
            "Total stops on routes:",
            QLabel(str(summary.get("total_stops", 0))),
        )
        local_layout.addRow(
            "Standalone point assets:",
            QLabel(str(summary.get("standalone_assets", 0))),
        )
        unmatched = summary.get("routes_unmatched", 0)
        if unmatched:
            unmatched_label = QLabel(str(unmatched))
            unmatched_label.setStyleSheet("color: #b35900; font-weight: bold;")
            unmatched_label.setToolTip(
                "These cable routes matched no structures within the snap "
                "tolerance. They will push with coordinates taken from the "
                "line endpoints instead of structure positions."
            )
            local_layout.addRow(
                "Routes without matched structures:", unmatched_label,
            )
        # Bold total
        total_label = QLabel(f"<b>{summary.get('total_locs', 0)}</b>")
        local_layout.addRow("Total LOCs:", total_label)

        deleted = summary.get("will_be_deleted", 0)
        if deleted:
            del_label = QLabel(str(deleted))
            del_label.setStyleSheet("color: #c0392b; font-weight: bold;")
            del_label.setToolTip(
                "LOCs that were pulled from this location but are no "
                "longer in the data being pushed. The push is a full "
                "replacement — the server will delete them."
            )
            local_layout.addRow("Will be deleted on server:", del_label)
        root.addWidget(local_group)

        # Server preview group
        self._server_group = QGroupBox("Server Response")
        self._server_layout = QVBoxLayout(self._server_group)
        self._server_status = QLabel("Validating with server (no data written yet)...")
        self._server_layout.addWidget(self._server_status)
        root.addWidget(self._server_group)

        # Buttons
        self._buttons = QDialogButtonBox(
            BB_OK | BB_CANCEL
        )
        self._push_btn = self._buttons.button(BB_OK)
        self._push_btn.setText("Push")
        self._push_btn.setEnabled(False)  # enabled after preview loads
        self._buttons.accepted.connect(self._on_push)
        self._buttons.rejected.connect(self._on_cancel)
        root.addWidget(self._buttons)

    def _on_cancel(self):
        """Cancel any running preview task and close."""
        if self._task is not None:
            self._task.taskCompleted.disconnect(self._on_preview_loaded)
            self._task.taskTerminated.disconnect(self._on_preview_loaded)
            self._task = None
        self.reject()

    # ------------------------------------------------------------------
    # Server preview fetch
    # ------------------------------------------------------------------

    def _fetch_server_preview(self):
        self._task = PushPreviewTask(self._api, self._payload)
        self._task.taskCompleted.connect(self._on_preview_loaded)
        self._task.taskTerminated.connect(self._on_preview_loaded)
        QgsApplication.taskManager().addTask(self._task)

    def _on_preview_loaded(self):
        task = self._task
        self._task = None
        if task is None:
            return

        if task.error:
            err_lower = task.error.lower()
            is_server_error = ("timeout" in err_lower
                               or "500" in task.error
                               or "502" in task.error
                               or "503" in task.error)

            # Build diagnostic detail text
            detail_parts = [task.error]
            if task.status_code:
                detail_parts.append(f"HTTP {task.status_code}")
            if task.request_id:
                detail_parts.append(f"Request-ID: {task.request_id}")
            if task.elapsed_seconds:
                detail_parts.append(f"Elapsed: {task.elapsed_seconds:.1f}s")
            detail_parts.append(
                "\nCheck server connectivity, then try again. "
                "If the problem persists, contact your administrator."
            )
            self._server_status.setText("\n".join(detail_parts))
            self._server_status.setStyleSheet("color: red;")

            if is_server_error:
                self._push_btn.setEnabled(False)
                self._push_btn.setToolTip(
                    "Push blocked: server preview failed. "
                    "Check server health and review saved response."
                )
            else:
                self._push_btn.setEnabled(True)
            return

        # Replace status label with a form of stats key-values
        self._server_status.setVisible(False)

        stats = task.stats or {}
        if not stats:
            lbl = QLabel("Server returned no stats.")
            self._server_layout.addWidget(lbl)
        else:
            form = QFormLayout()
            for key, value in stats.items():
                form.addRow(f"{_pretty_key(key)}:", QLabel(str(value)))
            self._server_layout.addLayout(form)

        self._push_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Push confirmation
    # ------------------------------------------------------------------

    def _on_push(self):
        self.push_confirmed.emit(self._payload)
        self.accept()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

# Known server stat keys → human-friendly labels.
_KNOWN_LABELS = {
    "createdlocs": "Created LOCs",
    "updatedlocs": "Updated LOCs",
    "deletedlocs": "Deleted LOCs",
    "created_locs": "Created LOCs",
    "updated_locs": "Updated LOCs",
    "deleted_locs": "Deleted LOCs",
    "createdlocations": "Created Locations",
    "updatedlocations": "Updated Locations",
    "created_locations": "Created Locations",
    "updated_locations": "Updated Locations",
    "totalprocessed": "Total Processed",
    "total_processed": "Total Processed",
    "totallocs": "Total LOCs",
    "total_locs": "Total LOCs",
    "singlelocs": "Single LOCs",
    "single_locs": "Single LOCs",
    "duallocs": "Dual LOCs",
    "dual_locs": "Dual LOCs",
    "multilocs": "Multi LOCs",
    "multi_locs": "Multi LOCs",
    "errors": "Errors",
    "warnings": "Warnings",
    "message": "Message",
    "status": "Status",
}


def _pretty_key(key: str) -> str:
    """Turn a snake_case or camelCase key into a readable label."""
    # Check known labels first (case-insensitive)
    lookup = key.lower().strip()
    if lookup in _KNOWN_LABELS:
        return _KNOWN_LABELS[lookup]

    import re
    # Insert space before uppercase letters (camelCase → Camel Case)
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", key)
    # Replace underscores/hyphens with spaces
    spaced = spaced.replace("_", " ").replace("-", " ")
    # Title-case, then restore known acronyms
    result = spaced.title()
    for acronym in ("Loc", "Locs", "Id", "Uuid", "Api", "Url"):
        result = result.replace(acronym, acronym.upper())
    # Fix "LOCS" → "LOCs"
    result = result.replace("LOCS", "LOCs")
    return result
