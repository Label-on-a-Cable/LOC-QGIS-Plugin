"""Login dialog with email/password and optional 2FA OTP field."""

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)
from qgis.core import QgsApplication, QgsTask

from ..services.auth_service import AuthService
from ..services.exceptions import (
    LOCAPIException,
    OTPRequiredException,
)


class _LoginTask(QgsTask):
    """Runs the blocking login call off the main thread."""

    def __init__(self, auth_service, email, password, otp):
        super().__init__("LOC Login", QgsTask.CanCancel)
        self.auth = auth_service
        self.email = email
        self.password = password
        self.otp = otp
        self.error = None   # str or None
        self.needs_otp = False

    def run(self):
        try:
            self.auth.login(self.email, self.password, self.otp or None)
        except OTPRequiredException as exc:
            self.needs_otp = True
            self.error = str(exc)
            return True  # not a failure — dialog will show OTP field
        except LOCAPIException as exc:
            self.error = str(exc)
            return False
        except Exception as exc:
            self.error = f"Unexpected error: {exc}"
            return False
        return True


class LoginDialog(QDialog):
    """Modal login dialog.

    Emits ``login_successful`` when the user authenticates.
    """

    login_successful = pyqtSignal()

    def __init__(self, auth_service: AuthService, parent=None):
        super().__init__(parent)
        self.auth = auth_service
        self._task: _LoginTask | None = None

        self.setWindowTitle("LOC Login")
        self.setMinimumSize(QSize(360, 0))
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._email = QLineEdit()
        self._email.setPlaceholderText("email@example.com")
        form.addRow("Email:", self._email)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self._password)

        self._otp_label = QLabel("2FA Code:")
        self._otp = QLineEdit()
        self._otp.setPlaceholderText("Check your email")
        self._otp_label.setVisible(False)
        self._otp.setVisible(False)
        form.addRow(self._otp_label, self._otp)

        layout.addLayout(form)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._buttons.button(QDialogButtonBox.Ok).setText("Login")
        self._buttons.accepted.connect(self._on_submit)
        self._buttons.rejected.connect(self._on_cancel)
        layout.addWidget(self._buttons)

    def _on_cancel(self):
        """Cancel any running task and close."""
        if self._task is not None:
            self._task.taskCompleted.disconnect(self._on_task_finished)
            self._task.taskTerminated.disconnect(self._on_task_finished)
            self._task = None
        self.reject()

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def _on_submit(self):
        email = self._email.text().strip()
        password = self._password.text()
        otp = self._otp.text().strip() if self._otp.isVisible() else ""

        if not email or not password:
            self._show_error("Email and password are required.")
            return

        self._set_busy(True)
        self._error_label.setVisible(False)

        self._task = _LoginTask(self.auth, email, password, otp)
        self._task.taskCompleted.connect(self._on_task_finished)
        self._task.taskTerminated.connect(self._on_task_finished)
        QgsApplication.taskManager().addTask(self._task)

    def _on_task_finished(self):
        task = self._task
        self._task = None
        if task is None:
            return
        self._set_busy(False)

        if task.needs_otp:
            # Show OTP field and let the user re-submit
            self._otp_label.setVisible(True)
            self._otp.setVisible(True)
            self._otp.setFocus()
            self._show_error(task.error or "Enter the 2FA code sent to your email.")
            return

        if task.error:
            self._show_error(task.error)
            return

        # Success
        self.login_successful.emit()
        self.accept()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_error(self, msg: str):
        self._error_label.setText(msg)
        self._error_label.setVisible(True)

    def _set_busy(self, busy: bool):
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(not busy)
        self._email.setEnabled(not busy)
        self._password.setEnabled(not busy)
        self._otp.setEnabled(not busy)
