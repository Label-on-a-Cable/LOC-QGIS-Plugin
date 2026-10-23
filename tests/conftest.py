"""Shared pytest fixtures for the Label on a Cable end-to-end suite.

Boots a real (offscreen) QgsApplication, isolates QgsSettings in a temp
config dir, starts a fake LOC API server per test, and provides a fake
QGIS ``iface`` backed by a real QMainWindow + QgsMapCanvas.
"""

import os
import pathlib
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = pathlib.Path(__file__).resolve().parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Isolate QSettings/QgsSettings from the developer's real QGIS profile.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="loc-qgis-cfg-")
os.environ.pop("LOC_BASE_URL", None)

from qgis.testing import start_app  # noqa: E402

QGIS_APP = start_app()

import pytest  # noqa: E402
from qgis.core import QgsApplication, QgsProject, QgsSettings  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox  # noqa: E402

from fake_loc_server import FakeLocServer  # noqa: E402


# ----------------------------------------------------------------------
# Coverage inside QgsTask worker threads
# ----------------------------------------------------------------------
# QgsTask.run() executes on a QThreadPool thread that Python's
# ``threading`` module never sees, so coverage's per-thread tracer is not
# installed there.  Re-arm it the same way threading.Thread does.

def _install_task_tracing():
    from label_on_a_cable.core import tasks as core_tasks
    from label_on_a_cable.ui import login_dialog

    classes = [
        core_tasks.FetchLocationsTask, core_tasks.FetchCategoriesTask,
        core_tasks.PushPreviewTask, core_tasks.PushTask,
        core_tasks.FetchLocsTask, core_tasks.GenerateRoutesTask,
        login_dialog._LoginTask,
    ]
    for cls in classes:
        original = cls.run
        if getattr(original, "_loc_traced", False):
            continue

        def traced(self, _orig=original):
            hook = getattr(threading, "_trace_hook", None)
            if hook is not None and sys.gettrace() is None:
                sys.settrace(hook)
            return _orig(self)

        traced._loc_traced = True
        cls.run = traced


_install_task_tracing()


# ----------------------------------------------------------------------
# Global state reset
# ----------------------------------------------------------------------

def _reset_export_builder_globals():
    from label_on_a_cable.core import export_builder as eb
    eb._stamp_cache = None
    eb._stamp_cache_path = None
    eb._active_location_id = ""
    eb._current_stamp_layer = None
    eb._layer_identity.clear()
    eb._ambiguous_asset_keys.clear()
    eb._key_memo.clear()


def wait_tasks_idle(timeout_ms=15000):
    """Pump the event loop until the global task manager is idle."""
    from qgis.PyQt.QtCore import QCoreApplication, QElapsedTimer
    tm = QgsApplication.taskManager()
    timer = QElapsedTimer()
    timer.start()
    while tm.countActiveTasks() and timer.elapsed() < timeout_ms:
        QCoreApplication.processEvents()
    for _ in range(5):
        QCoreApplication.processEvents()


@pytest.fixture(autouse=True)
def clean_qgis_state():
    """Fresh project, settings and module caches for every test."""
    wait_tasks_idle()
    QgsProject.instance().clear()
    s = QgsSettings()
    s.remove("LOC")
    _reset_export_builder_globals()
    yield
    wait_tasks_idle()
    QgsProject.instance().clear()
    QgsSettings().remove("LOC")
    _reset_export_builder_globals()


# ----------------------------------------------------------------------
# Fake server + API client
# ----------------------------------------------------------------------

@pytest.fixture
def loc_server(monkeypatch):
    server = FakeLocServer().start()
    monkeypatch.setenv("LOC_BASE_URL", server.base_url)
    yield server
    server.stop()


@pytest.fixture
def media_server():
    """A second fake server on another host, like pre-signed S3.

    127.0.0.2 is loopback on Linux but not one of the hosts the plugin
    rewrites to the API origin, so it is treated as genuinely off-host.
    """
    server = FakeLocServer(host="127.0.0.2").start()
    yield server
    server.stop()


@pytest.fixture
def api(loc_server):
    from label_on_a_cable.services.api_client import ApiClient
    return ApiClient()


@pytest.fixture
def authed_api(api, loc_server):
    api.set_token(loc_server.issue_token())
    return api


# ----------------------------------------------------------------------
# Fake QGIS interface
# ----------------------------------------------------------------------

class FakeMessageBar:
    """Records pushes; mirrors them into a real QgsMessageBar if given."""

    def __init__(self, widget=None):
        self.messages = []
        self.widget = widget

    def _push(self, level, title, text):
        self.messages.append((level, title, text))
        if self.widget is not None:
            from qgis.core import Qgis
            levels = {"success": Qgis.MessageLevel.Success,
                      "info": Qgis.MessageLevel.Info,
                      "warning": Qgis.MessageLevel.Warning,
                      "critical": Qgis.MessageLevel.Critical}
            self.widget.clearWidgets()
            self.widget.pushMessage(title, text, levels[level], 0)

    def pushSuccess(self, title, text):
        self._push("success", title, text)

    def pushInfo(self, title, text):
        self._push("info", title, text)

    def pushWarning(self, title, text):
        self._push("warning", title, text)

    def pushCritical(self, title, text):
        self._push("critical", title, text)

    def texts(self, level=None):
        return [m[2] for m in self.messages if level is None or m[0] == level]

    def last(self):
        return self.messages[-1] if self.messages else None


class FakeIface:
    def __init__(self):
        from qgis.gui import QgsMessageBar
        from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget
        self._main = QMainWindow()
        self._main.setWindowTitle("TestLocation — QGIS")
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._bar_widget = QgsMessageBar(central)
        self._canvas = QgsMapCanvas(central)
        layout.addWidget(self._bar_widget)
        layout.addWidget(self._canvas)
        self._main.setCentralWidget(central)
        self._bar = FakeMessageBar(self._bar_widget)
        self.toolbar_icons = []
        self.docks = []
        self._main.resize(1280, 732)
        self._main.show()

    def mainWindow(self):
        return self._main

    def mapCanvas(self):
        return self._canvas

    def messageBar(self):
        return self._bar

    def addToolBar(self, name):
        return self._main.addToolBar(name)

    def addToolBarIcon(self, action):
        self.toolbar_icons.append(action)

    def removeToolBarIcon(self, action):
        self.toolbar_icons.remove(action)

    def addDockWidget(self, area, dock):
        self._main.addDockWidget(area, dock)
        self.docks.append(dock)

    def removeDockWidget(self, dock):
        self._main.removeDockWidget(dock)
        self.docks.remove(dock)

    def close(self):
        self._main.close()
        self._main.deleteLater()


@pytest.fixture
def iface(qapp):
    fi = FakeIface()
    yield fi
    fi.close()


# ----------------------------------------------------------------------
# Modal dialog / message box drivers
# ----------------------------------------------------------------------

@pytest.fixture
def drive_modal(monkeypatch):
    """Replace ``cls.exec`` with a driver callback (non-blocking).

    Usage: ``opened = drive_modal(LoginDialog, lambda dlg: ...)``; the
    returned list collects every dialog instance that was "executed".
    """

    def _install(cls, driver):
        opened = []

        def fake_exec(self):
            self.show()
            opened.append(self)
            driver(self)
            return self.result()

        monkeypatch.setattr(cls, "exec", fake_exec)
        return opened

    return _install


class MessageBoxRecorder:
    """Records static QMessageBox calls and answers them."""

    def __init__(self):
        self.calls = []        # (kind, title, text)
        self.answers = []      # queue of StandardButton for question/warning
        self.default = QMessageBox.StandardButton.Yes
        self.on_call = None    # recorder hook: on_call(kind, title, text)

    def _make(self, kind):
        def _fn(parent, title, text, *args, **kwargs):
            self.calls.append((kind, title, text))
            if self.on_call is not None:
                self.on_call(kind, title, text)
            if kind == "critical" or kind == "information":
                return QMessageBox.StandardButton.Ok
            return self.answers.pop(0) if self.answers else self.default
        return _fn

    def titles(self, kind=None):
        return [c[1] for c in self.calls if kind is None or c[0] == kind]


@pytest.fixture
def msgbox(monkeypatch):
    rec = MessageBoxRecorder()
    for kind in ("question", "warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(rec._make(kind)))
    return rec


# ----------------------------------------------------------------------
# Video recording (Cypress-style): --video=off|failed|all
# ----------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--video", choices=("off", "failed", "all"), default="failed",
        help="Save step videos for tests that use the `recorder` fixture: "
             "only failed/xfailed tests (default), all of them, or none.",
    )
    parser.addoption(
        "--live-target", choices=("real", "fake"), default="real",
        help="tests/test_live_videos.py: run against the real LOC server "
             "(needs LOC_LIVE_EMAIL/LOC_LIVE_PASSWORD) or the fake one.",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


VIDEO_DIR = TESTS / "reports" / "videos"


@pytest.fixture
def recorder(request, iface, msgbox):
    """Captioned step recorder over the fake QGIS window.

    Message boxes the plugin raises are rendered into the video too.
    """
    from recorder import Recorder
    title = getattr(request.function, "video_title", None) or request.node.name
    rec = Recorder(title, iface.mainWindow())
    msgbox.on_call = rec.show_message_box
    yield rec
    mode = request.config.getoption("--video")
    rep = getattr(request.node, "rep_call", None)
    failed = rep is not None and (rep.failed or hasattr(rep, "wasxfail"))
    if mode == "all" or (mode == "failed" and failed):
        out = VIDEO_DIR / os.environ.get("LOC_VIDEO_SUBDIR", "")
        rec.save(out, request.node.name.replace("[", "_").replace("]", ""))


@pytest.fixture
def opened_urls(monkeypatch):
    import webbrowser
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: urls.append(url))
    return urls
