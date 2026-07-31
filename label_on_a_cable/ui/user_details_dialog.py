"""User details dialog — shows profile info, sign-out, and dashboard link."""

import webbrowser

from qgis.PyQt.QtCore import QSize, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..models.user import User
from ..services.config import get_base_url


class UserDetailsDialog(QDialog):
    """Modal dialog showing the current user's profile.

    Emits ``signed_out`` when the user clicks Sign Out.
    Emits ``sync_requested`` when the user clicks Sync with LOC.
    """

    signed_out = pyqtSignal()
    sync_requested = pyqtSignal()

    def __init__(self, user: User, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LOC User Details")
        self.setMinimumSize(QSize(340, 0))
        self._build_ui(user)

    def _build_ui(self, user: User):
        layout = QVBoxLayout(self)

        # -- Profile info --
        form = QFormLayout()
        form.addRow("Name:", QLabel(user.full_name))
        form.addRow("Email:", QLabel(user.email))
        form.addRow("Role:", QLabel(user.role))
        form.addRow("Organisation:", QLabel(user.organization.name))
        layout.addLayout(form)

        layout.addSpacing(12)

        # -- Sync button --
        btn_sync = QPushButton("Sync with LOC")
        btn_sync.setToolTip(
            "Match existing QGIS features to server LOC records by their "
            "unique asset ID, so future pushes update instead of re-creating."
        )
        btn_sync.clicked.connect(self._on_sync)
        layout.addWidget(btn_sync)

        layout.addSpacing(8)

        # -- Buttons --
        btn_row = QHBoxLayout()

        btn_dashboard = QPushButton("Open Dashboard")
        btn_dashboard.clicked.connect(self._open_dashboard)
        btn_row.addWidget(btn_dashboard)

        btn_row.addStretch()

        btn_signout = QPushButton("Sign Out")
        btn_signout.clicked.connect(self._on_sign_out)
        btn_row.addWidget(btn_signout)

        layout.addLayout(btn_row)

    def _open_dashboard(self):
        webbrowser.open(get_base_url())

    def _on_sync(self):
        self.sync_requested.emit()
        self.accept()

    def _on_sign_out(self):
        self.signed_out.emit()
        self.accept()
