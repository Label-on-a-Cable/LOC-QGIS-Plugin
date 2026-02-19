"""Main plugin class for Label on a Cable.

LOC QGIS plugin for automated generation, synchronisation, and management
of infrastructure labels.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from .services.api_client import ApiClient
from .services.auth_service import AuthService


class LabelOnACablePlugin:
    """QGIS plugin: Label on a Cable - KelTech IoE.

    Automated generation, synchronisation, and management of infrastructure
    labels with bidirectional data flow between QGIS and LOC.
    """

    PLUGIN_NAME = "Label on a Cable"

    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self.actions = {}
        self._plugin_dir = os.path.dirname(__file__)

        # Shared services (created once, live for plugin lifetime)
        self.api = ApiClient()
        self.auth = AuthService(self.api)

        # Plugin state
        self.active_location = None   # models.location.Location or None
        self.layer_mappings = []      # List[models.mapping.LayerMapping]
        self._categories_cache = {}   # {category_id: Category} from mapping dialog
        self._sidebar = None          # ui.location_sidebar.LocationSidebar
        self._review_dlg = None       # ui.route_review_dialog.RouteReviewDialog

        # Route generation state
        self.cached_routes = []       # List[models.route.Route]
        self._last_fingerprint = ""   # SHA-256 from change_detector
        self._gen_task = None         # core.tasks.GenerateRoutesTask
        self._push_task = None        # core.tasks.PushTask

        # Pull / import state
        self._pulled_loc_ids = set()  # Set[str] for delete tracking
        self._pulled_multi_stop_counts = {}  # {multi_id: stop_count}

    # ------------------------------------------------------------------
    # QGIS lifecycle
    # ------------------------------------------------------------------

    def initGui(self):
        """Called by QGIS when the plugin is loaded."""
        self.toolbar = self.iface.addToolBar(self.PLUGIN_NAME)
        self.toolbar.setObjectName("LabelOnACableToolbar")

        self._add_action(
            key="login",
            text="Login",
            icon="login.svg",
            callback=self._on_login,
            enabled=True,
            tooltip="Sign in to your LOC account",
        )
        self._add_action(
            key="workspace",
            text="Workspace",
            icon="workspace.svg",
            callback=self._on_workspace,
            enabled=False,
            tooltip="Open the LOC Workspace to select a location",
        )
        self._add_action(
            key="mapping",
            text="Category Mapping",
            icon="mapping.svg",
            callback=self._on_mapping,
            enabled=False,
            tooltip="Map QGIS layers and fields to LOC categories",
        )
        self._add_action(
            key="generate",
            text="Generate Route Labels",
            icon="generate.svg",
            callback=self._on_generate,
            enabled=False,
            tooltip=(
                "Generate cable routes and stops from mapped layers.\n"
                "Requires: layers mapped in Category Mapping."
            ),
        )
        self._add_action(
            key="push",
            text="Push to LOC",
            icon="push.svg",
            callback=self._on_push,
            enabled=False,
            tooltip="Upload route and asset data to the LOC server",
        )
        self._add_action(
            key="pull",
            text="Pull from LOC",
            icon="pull.svg",
            callback=self._on_pull,
            enabled=False,
            tooltip="Download existing LOC data for the selected location",
        )

        # Restore previous session from QSettings (if any)
        if self.auth.try_restore():
            self.set_logged_in(True)

    def unload(self):
        """Called by QGIS when the plugin is unloaded."""
        if self._review_dlg is not None:
            self._review_dlg.close()
            self._review_dlg = None
        if self._sidebar is not None:
            self.iface.removeDockWidget(self._sidebar)
            self._sidebar.deleteLater()
            self._sidebar = None
        for action in self.actions.values():
            self.iface.removeToolBarIcon(action)
        if self.toolbar:
            del self.toolbar
        self.actions.clear()

    # ------------------------------------------------------------------
    # Toolbar helpers
    # ------------------------------------------------------------------

    def _icon_path(self, filename):
        return os.path.join(self._plugin_dir, "icons", filename)

    def _add_action(self, key, text, icon, callback, enabled=True, tooltip=""):
        icon_path = self._icon_path(icon)
        if os.path.exists(icon_path):
            qicon = QIcon(icon_path)
        else:
            qicon = QIcon()

        action = QAction(qicon, text, self.iface.mainWindow())
        action.setEnabled(enabled)
        action.triggered.connect(callback)
        action.setObjectName(f"loc_{key}")
        if tooltip:
            action.setToolTip(tooltip)

        self.toolbar.addAction(action)
        self.iface.addToolBarIcon(action)
        self.actions[key] = action

    def _has_pulled_layers(self):
        """Check if any mapped layer has pulled tracking attributes.

        Detects pulled data by checking for ``_loc_id`` in the layer
        schema.  This survives actions that clear the ephemeral
        ``_pulled_loc_ids`` flag (e.g. reopening the mapping dialog).
        """
        from qgis.core import QgsProject, QgsVectorLayer

        project = QgsProject.instance()
        for lm in self.layer_mappings:
            layer = project.mapLayer(lm.layer_id)
            if not isinstance(layer, QgsVectorLayer):
                continue
            if "_loc_id" in layer.fields().names():
                return True
        return False

    def set_logged_in(self, logged_in):
        """Enable or disable toolbar buttons based on auth state."""
        for key, action in self.actions.items():
            if key == "login":
                continue
            action.setEnabled(logged_in)

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------

    def _on_login(self):
        """Open login dialog (or user details if already signed in)."""
        if self.auth.is_logged_in:
            from .ui.user_details_dialog import UserDetailsDialog

            dlg = UserDetailsDialog(
                self.auth.current_user, parent=self.iface.mainWindow()
            )
            dlg.signed_out.connect(self._on_sign_out)
            dlg.exec_()
            return

        from .ui.login_dialog import LoginDialog

        dlg = LoginDialog(self.auth, parent=self.iface.mainWindow())
        dlg.login_successful.connect(self._on_login_success)
        dlg.exec_()

    def _on_login_success(self):
        """Called after a successful login."""
        self.set_logged_in(True)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Logged in as {self.auth.current_user.full_name}",
        )

    def _on_sign_out(self):
        """Called when the user clicks Sign Out in the details dialog."""
        # Cancel any running background tasks
        if self._gen_task is not None:
            self._gen_task.taskCompleted.disconnect(self._on_generation_done)
            self._gen_task.taskTerminated.disconnect(self._on_generation_done)
            self._gen_task = None
        if self._push_task is not None:
            self._push_task.taskCompleted.disconnect(self._on_push_done)
            self._push_task.taskTerminated.disconnect(self._on_push_done)
            self._push_task = None

        self.auth.logout()
        self.active_location = None
        self.layer_mappings = []
        self._categories_cache = {}
        self.cached_routes = []
        self._last_fingerprint = ""
        self._pulled_loc_ids = set()
        self._pulled_multi_stop_counts = {}
        if self._review_dlg is not None:
            self._review_dlg.close()
            self._review_dlg = None
        if self._sidebar is not None:
            self._sidebar.hide()
        self.set_logged_in(False)
        self.iface.messageBar().pushInfo(
            self.PLUGIN_NAME, "Signed out."
        )

    def _on_workspace(self):
        """Toggle the workspace / location sidebar."""
        if self._sidebar is None:
            from .ui.location_sidebar import LocationSidebar

            self._sidebar = LocationSidebar(
                self.api, parent=self.iface.mainWindow()
            )
            self._sidebar.location_selected.connect(
                self._on_location_selected
            )
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._sidebar)
            self._sidebar.fetch_locations()
        else:
            self._sidebar.setVisible(not self._sidebar.isVisible())
            if self._sidebar.isVisible():
                self._sidebar.fetch_locations()

    def _on_location_selected(self, location):
        """Called when the user picks a Location in the sidebar."""
        prev = self.active_location
        self.active_location = location

        # If the location actually changed, invalidate all dependent state
        # so stale mappings / pulled data can never be pushed to the wrong
        # location.
        if prev is not None and prev.location_id != location.location_id:
            self.layer_mappings = []
            self._categories_cache = {}
            self.cached_routes = []
            self._last_fingerprint = ""
            self._pulled_loc_ids = set()
            self._pulled_multi_stop_counts = {}

        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Active location: {location.name}",
        )

    def _on_mapping(self):
        """Open the category mapping dialog."""
        if not self.active_location:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Select a Location in the Workspace sidebar first.",
            )
            return

        from .ui.category_mapping_dialog import CategoryMappingDialog

        dlg = CategoryMappingDialog(
            self.api,
            self.active_location.location_id,
            current_mappings=self.layer_mappings,
            parent=self.iface.mainWindow(),
        )
        dlg.mapping_accepted.connect(self._on_mapping_accepted)
        dlg.exec_()

        # Cache categories fetched by the dialog (needed for export payload)
        if dlg._categories:
            self._categories_cache = {
                c.category_id: c for c in dlg._categories
            }

    def _on_mapping_accepted(self, mappings):
        """Called when the user confirms category mappings."""
        self.layer_mappings = mappings
        # Clear pull state — new/changed mappings mean the user is working
        # with different layers (possibly a different project).
        self._pulled_loc_ids = set()
        self._pulled_multi_stop_counts = {}
        count = len(mappings)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Category mapping saved ({count} layer{'s' if count != 1 else ''} mapped).",
        )

    def _on_generate(self):
        """Run route label generation with caching and edit warnings."""
        if not self.layer_mappings:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Configure Category Mapping first.",
            )
            return

        from .core.change_detector import compute_fingerprint

        current_fp = compute_fingerprint(self.layer_mappings)
        has_edits = any(r.has_edits for r in self.cached_routes)

        # No changes since last generation and no manual edits → reuse cache
        if (current_fp and current_fp == self._last_fingerprint
                and not has_edits):
            count = len(self.cached_routes)
            self.iface.messageBar().pushInfo(
                self.PLUGIN_NAME,
                f"No layer changes detected — showing {count} cached route(s).",
            )
            self._show_route_review()
            return

        # Manual edits exist and layers changed → warn before overwriting
        if has_edits:
            reply = QMessageBox.question(
                self.iface.mainWindow(),
                "Regenerate Routes",
                "You have manual edits that will be discarded. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        # Pulled data → use vertex-based reconstruction instead of the
        # snapping generator (which matches ALL points to ALL lines in
        # dense areas).  Detect pulled layers by schema (presence of
        # _loc_id field) rather than relying solely on the ephemeral
        # _pulled_loc_ids flag which can be cleared by other actions.
        if self._pulled_loc_ids or self._has_pulled_layers():
            self._run_reconstruction()
            return

        self._run_generation()

    def _run_generation(self):
        """Kick off the GenerateRoutesTask."""
        if self._gen_task is not None:
            return  # already running

        from qgis.core import QgsApplication
        from .core.tasks import GenerateRoutesTask

        self._gen_task = GenerateRoutesTask(self.layer_mappings)
        self._gen_task.taskCompleted.connect(self._on_generation_done)
        self._gen_task.taskTerminated.connect(self._on_generation_done)
        QgsApplication.taskManager().addTask(self._gen_task)
        self.iface.messageBar().pushInfo(
            self.PLUGIN_NAME, "Generating route labels..."
        )

    def _on_generation_done(self):
        """Handle completed route generation."""
        task = self._gen_task
        self._gen_task = None
        if task is None:
            return

        if task.error:
            self.iface.messageBar().pushCritical(
                self.PLUGIN_NAME, task.error
            )
            return

        from .core.change_detector import compute_fingerprint

        self.cached_routes = task.routes
        self._last_fingerprint = compute_fingerprint(self.layer_mappings)
        # User explicitly regenerated — pull-time stop counts no longer apply
        self._pulled_multi_stop_counts = {}

        count = len(self.cached_routes)
        total_stops = sum(r.active_stop_count for r in self.cached_routes)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Generated {count} route(s) with {total_stops} stop(s).",
        )
        self._show_route_review()

    def _run_reconstruction(self):
        """Reconstruct routes from pulled layer geometry.

        Used instead of the snapping-based generator when the current
        layers came from a Pull (``_pulled_loc_ids`` is non-empty).
        """
        from .core.import_builder import reconstruct_routes
        from .core.change_detector import compute_fingerprint

        self.cached_routes = reconstruct_routes(self.layer_mappings)
        self._last_fingerprint = compute_fingerprint(self.layer_mappings)
        # User explicitly regenerated — pull-time stop counts no longer apply
        self._pulled_multi_stop_counts = {}

        count = len(self.cached_routes)
        total_stops = sum(r.active_stop_count for r in self.cached_routes)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Reconstructed {count} route(s) with {total_stops} stop(s).",
        )
        self._show_route_review()

    def _show_route_review(self):
        """Open (or re-focus) the modeless route review dialog."""
        if not self.cached_routes:
            return

        # Close any existing dialog first (clears old highlights)
        if self._review_dlg is not None:
            self._review_dlg.close()
            self._review_dlg = None

        from .ui.route_review_dialog import RouteReviewDialog

        self._review_dlg = RouteReviewDialog(
            self.cached_routes,
            canvas=self.iface.mapCanvas(),
            parent=self.iface.mainWindow(),
        )
        self._review_dlg.finished.connect(self._on_review_closed)
        self._review_dlg.show()
        self._review_dlg.raise_()
        self._review_dlg.activateWindow()

    def _on_review_closed(self):
        """Clean up reference when the review dialog is closed."""
        self._review_dlg = None

    def _on_push(self):
        """Build export payload, validate, show preview, then push."""
        if not self.active_location:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Select a Location in the Workspace sidebar first.",
            )
            return

        if not self.layer_mappings:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Configure Category Mapping first.",
            )
            return

        # Validate that mapped layers still exist in the project
        from qgis.core import QgsProject, QgsVectorLayer
        project = QgsProject.instance()
        missing = []
        for lm in self.layer_mappings:
            layer = project.mapLayer(lm.layer_id)
            if not isinstance(layer, QgsVectorLayer):
                missing.append(lm.layer_id)
        if missing:
            self.iface.messageBar().pushCritical(
                self.PLUGIN_NAME,
                f"{len(missing)} mapped layer(s) no longer exist in the project. "
                "Re-open Category Mapping to fix.",
            )
            return

        if not self.cached_routes:
            reply = QMessageBox.question(
                self.iface.mainWindow(),
                "No Routes Generated",
                "Route generation has not been run.\n\n"
                "If your data contains line layers (cable routes), you "
                "should generate routes first — otherwise route data "
                "will not be included in the push.\n\n"
                "Continue pushing standalone point assets only?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        if not self.auth.current_user:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME, "Not logged in.",
            )
            return

        import json as _json
        from qgis.core import Qgis, QgsMessageLog
        from .core.export_builder import (
            build_payload, log_payload_metrics, payload_summary,
            validate_payload,
        )
        from .ui.push_preview_dialog import PushPreviewDialog

        payload = build_payload(
            routes=self.cached_routes,
            layer_mappings=self.layer_mappings,
            categories=self._categories_cache,
            location=self.active_location,
            user_id=self.auth.current_user.user_id,
            organization_id=self.auth.current_user.organization.org_id,
            pulled_loc_ids=self._pulled_loc_ids or None,
        )

        # --- Instrumentation: log metrics before push ---
        metrics = log_payload_metrics(payload)
        QgsMessageLog.logMessage(metrics, "LOC Push", Qgis.Info)

        # --- Build reference data for invariant checks ---
        # Only use in-memory pull counts (set on pull, cleared on regeneration).
        # When cleared, stop-count invariant is simply skipped — the other
        # three structural invariants still run.
        expected_counts = None
        counts_authoritative = False

        if self._pulled_multi_stop_counts:
            expected_counts = self._pulled_multi_stop_counts
            counts_authoritative = True
            QgsMessageLog.logMessage(
                f"Stop count source: in-memory pull data "
                f"({len(expected_counts)} route(s))",
                "LOC Push", Qgis.Info,
            )
        else:
            QgsMessageLog.logMessage(
                "Stop count source: none (skipping stop-count invariant)",
                "LOC Push", Qgis.Info,
            )

        # --- Preflight invariant validation ---
        issues = validate_payload(
            payload, expected_counts, counts_authoritative,
        )
        errors = [msg for level, msg in issues if level == "error"]
        warnings = [msg for level, msg in issues if level == "warning"]

        if errors:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Push Validation Failed",
                "Cannot push — structural invariant violated:\n\n"
                + "\n".join(f"• {e}" for e in errors),
            )
            return

        if warnings:
            reply = QMessageBox.warning(
                self.iface.mainWindow(),
                "Push Warning",
                "Potential issues detected:\n\n"
                + "\n".join(f"• {w}" for w in warnings)
                + "\n\nContinue to preview?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        summary = payload_summary(payload)

        dlg = PushPreviewDialog(
            api_client=self.api,
            payload=payload,
            local_summary=summary,
            parent=self.iface.mainWindow(),
        )
        dlg.push_confirmed.connect(self._execute_push)
        dlg.exec_()

    def _execute_push(self, payload):
        """Execute the real push after user confirms in the preview."""
        if self._push_task is not None:
            return  # already running

        from qgis.core import QgsApplication
        from .core.tasks import PushTask

        self._push_task = PushTask(self.api, payload)
        self._push_task.taskCompleted.connect(self._on_push_done)
        self._push_task.taskTerminated.connect(self._on_push_done)
        QgsApplication.taskManager().addTask(self._push_task)
        self.iface.messageBar().pushInfo(
            self.PLUGIN_NAME, "Pushing to LOC..."
        )

    def _on_push_done(self):
        """Handle completed push."""
        task = self._push_task
        self._push_task = None
        if task is None:
            return

        if task.error:
            # Build detailed diagnostic message
            detail_lines = [task.error]
            if task.status_code:
                detail_lines.append(f"HTTP status: {task.status_code}")
            if task.request_id:
                detail_lines.append(f"Request-ID: {task.request_id}")
            if task.elapsed_seconds:
                detail_lines.append(
                    f"Elapsed: {task.elapsed_seconds:.1f}s"
                )
            if task.response_path:
                detail_lines.append(
                    f"Server response saved: {task.response_path}"
                )
            import os
            payload_path = os.path.join(
                os.path.expanduser("~"), "loc_push_payload.json",
            )
            if os.path.exists(payload_path):
                detail_lines.append(f"Payload saved: {payload_path}")

            # Distinguish true network failures from server-side 500s.
            # The server sometimes returns HTTP 500 (with a validation
            # message or "Operation timeout") but still commits the data.
            # Only treat as hard failure when we never got a response
            # (client-side timeout / connection error) or the status
            # indicates a proxy/gateway issue (502-504).
            err_lower = task.error.lower()
            is_hard_failure = (
                ("timeout" in err_lower and not task.status_code)
                or "connection" in err_lower
                or task.status_code in (502, 503, 504)
            )

            if is_hard_failure:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Push Failed",
                    "\n".join(detail_lines),
                )
            else:
                detail_lines.append(
                    "\nThe server returned an error, but the data "
                    "may have been written. Check LOC to verify."
                )
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Push — Server Warning",
                    "\n".join(detail_lines),
                )
                # Still open LOC so the user can verify
                self._open_loc_page()
            return

        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME, task.message or "Push complete."
        )
        self._open_loc_page()

    def _open_loc_page(self):
        """Open the LOC web page for the active location."""
        import webbrowser
        if self.active_location:
            loc_id = self.active_location.location_id
            webbrowser.open(f"http://63.33.18.108:8080/viewlocs/{loc_id}")
        else:
            webbrowser.open("http://63.33.18.108:8080")

    # ------------------------------------------------------------------
    # Pull from LOC
    # ------------------------------------------------------------------

    def _on_pull(self):
        """Open the pull dialog to import LOC data into QGIS layers."""
        if not self.active_location:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Select a Location in the Workspace sidebar first.",
            )
            return

        from .ui.pull_dialog import PullDialog

        dlg = PullDialog(
            api_client=self.api,
            location_id=self.active_location.location_id,
            location_name=self.active_location.name,
            parent=self.iface.mainWindow(),
        )
        dlg.import_complete.connect(self._on_pull_complete)
        dlg.exec_()

        # Cache categories fetched by the dialog
        if dlg._categories:
            self._categories_cache = {
                c.category_id: c for c in dlg._categories
            }

    def _on_pull_complete(self, layers, mappings, pulled_ids,
                          multi_stop_counts=None):
        """Handle imported layers from the pull dialog."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        for layer in layers:
            project.addMapLayer(layer)

        self.layer_mappings = mappings
        self._pulled_loc_ids = pulled_ids
        self._pulled_multi_stop_counts = multi_stop_counts or {}

        # Auto-reconstruct routes from pulled layer geometry so the user
        # can review/push without running the snapping-based generator
        # (which fails on dense pulled data).
        from .core.import_builder import reconstruct_routes
        from .core.change_detector import compute_fingerprint

        self.cached_routes = reconstruct_routes(mappings)
        self._last_fingerprint = compute_fingerprint(mappings)

        n_layers = len(layers)
        n_locs = len(pulled_ids)
        n_routes = len(self.cached_routes)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Imported {n_locs} LOC(s) into {n_layers} layer(s) "
            f"— {n_routes} route(s) ready for review.",
        )
