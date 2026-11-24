"""Main plugin class for Label on a Cable.

LOC QGIS plugin for automated generation, synchronisation, and management
of infrastructure labels.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from .qt_compat import MB_YES, MB_NO, RIGHT_DOCK
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

        # Snap tolerance (metres) for route generation, persisted across
        # QGIS sessions.  _routes_snap_tolerance records the value the
        # current cached_routes were generated with (None = reconstruction,
        # which does not snap).
        self.snap_tolerance = self._load_snap_tolerance()
        self._routes_snap_tolerance = None

        # Pull / import state
        self._session_pulled_layers = set()  # layer ids pulled since QGIS started
        self._pulled_loc_ids = set()  # Set[str], used to pick the route
                                      # reconstruction path on regenerate
        self._pulled_multi_stop_counts = {}  # {multi_id: stop_count}

        # Live server snapshot fetched at the start of a push so the
        # preview can predict deletions from what is actually on the
        # server rather than from what a Pull happened to remember.
        self._push_state_task = None
        # loc_ids the user confirmed for deletion in the last push preview
        self._pending_delete_ids = []
        self._pending_rebase = None     # merge with LOC for the confirmed push
        self._push_rebase = None

    _SNAP_TOLERANCE_KEY = "LOC/snap_tolerance"

    def _load_snap_tolerance(self) -> float:
        from qgis.core import QgsSettings
        from .core.route_generator import DEFAULT_SNAP_TOLERANCE
        raw = QgsSettings().value(
            self._SNAP_TOLERANCE_KEY, DEFAULT_SNAP_TOLERANCE,
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return DEFAULT_SNAP_TOLERANCE

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

        Detects pulled data by checking for a valid ``_loc_id`` value on
        at least one feature.  Only memory layers from a Pull have valid
        36-char UUIDs; Shapefiles may have truncated values from the old
        field-based stamping approach, which must be ignored.
        """
        from qgis.core import QgsProject, QgsVectorLayer

        project = QgsProject.instance()
        for lm in self.layer_mappings:
            layer = project.mapLayer(lm.layer_id)
            if not isinstance(layer, QgsVectorLayer):
                continue
            if "_loc_id" not in layer.fields().names():
                continue
            # Check first feature for a valid (non-truncated) UUID
            for feat in layer.getFeatures():
                try:
                    val = feat.attribute("_loc_id")
                except Exception:
                    break
                if val and len(str(val)) == 36:
                    return True
                break  # only need to check one feature
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
            dlg.sync_requested.connect(self._on_sync_stamps)
            dlg.exec()
            return

        from .ui.login_dialog import LoginDialog

        dlg = LoginDialog(self.auth, parent=self.iface.mainWindow())
        dlg.login_successful.connect(self._on_login_success)
        dlg.exec()

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

    def _on_auth_expired(self):
        """Called when an API call returns 401/403 (token expired)."""
        self.auth.logout()
        self.active_location = None
        self.set_logged_in(False)
        self.iface.messageBar().pushWarning(
            self.PLUGIN_NAME,
            "Session expired — please sign in again.",
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
            self._sidebar.auth_failed.connect(self._on_auth_expired)
            self.iface.addDockWidget(RIGHT_DOCK, self._sidebar)
            self._sidebar.fetch_locations()
        else:
            self._sidebar.setVisible(not self._sidebar.isVisible())
            if self._sidebar.isVisible():
                self._sidebar.fetch_locations()

    def _on_location_selected(self, location):
        """Called when the user picks a Location in the sidebar."""
        prev = self.active_location
        self.active_location = location

        # Set active location for stamp cache lookups (route review, etc.)
        from .core import export_builder as _eb
        _eb._active_location_id = location.location_id

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
            snap_tolerance=self.snap_tolerance,
            parent=self.iface.mainWindow(),
        )
        dlg.mapping_accepted.connect(self._on_mapping_accepted)
        dlg.exec()

        # Cache categories fetched by the dialog (needed for export payload)
        if dlg._categories:
            self._categories_cache = {
                c.category_id: c for c in dlg._categories
            }

    def _on_mapping_accepted(self, mappings, snap_tolerance):
        """Called when the user confirms category mappings."""
        self.layer_mappings = mappings
        # Saved LOC IDs are keyed by each layer's asset id field
        from .core.export_builder import set_layer_mappings
        set_layer_mappings(mappings)
        # Clear pull state — new/changed mappings mean the user is working
        # with different layers (possibly a different project).
        self._pulled_loc_ids = set()
        self._pulled_multi_stop_counts = {}

        if snap_tolerance != self.snap_tolerance:
            self.snap_tolerance = snap_tolerance
            # Cached routes were generated with the old tolerance —
            # invalidate so the next Generate re-runs.
            self._last_fingerprint = ""
        from qgis.core import QgsSettings
        QgsSettings().setValue(self._SNAP_TOLERANCE_KEY, snap_tolerance)

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
                MB_YES | MB_NO,
                MB_NO,
            )
            if reply != MB_YES:
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

        self._gen_task = GenerateRoutesTask(
            self.layer_mappings, snap_tolerance=self.snap_tolerance,
        )
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
        self._routes_snap_tolerance = task.snap_tolerance
        # User explicitly regenerated — pull-time stop counts no longer apply
        self._pulled_multi_stop_counts = {}

        count = len(self.cached_routes)
        total_stops = sum(r.active_stop_count for r in self.cached_routes)
        unmatched = sum(1 for r in self.cached_routes if r.is_unmatched)
        msg = f"Generated {count} route(s) with {total_stops} stop(s)."
        if unmatched:
            msg += (
                f" {unmatched} route(s) matched no structures within "
                f"{task.snap_tolerance:g} m and will use line-endpoint "
                f"coordinates."
            )
        self.iface.messageBar().pushSuccess(self.PLUGIN_NAME, msg)
        self._warn_features_without_geometry()
        self._show_route_review()

    def _run_reconstruction(self):
        """Reconstruct routes from pulled layer geometry.

        Used instead of the snapping-based generator when the current
        layers came from a Pull (``_pulled_loc_ids`` is non-empty).
        """
        from .core.import_builder import reconstruct_routes
        from .core.change_detector import compute_fingerprint

        self.cached_routes = reconstruct_routes(
            self.layer_mappings, snap_tolerance=self.snap_tolerance,
        )
        self._last_fingerprint = compute_fingerprint(self.layer_mappings)
        # Reconstruction itself is vertex-based; the tolerance only applies
        # to its snapping pass for newly added (non-pulled) features.
        self._routes_snap_tolerance = None
        # User explicitly regenerated — pull-time stop counts no longer apply
        self._pulled_multi_stop_counts = {}

        count = len(self.cached_routes)
        total_stops = sum(r.active_stop_count for r in self.cached_routes)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Reconstructed {count} route(s) with {total_stops} stop(s).",
        )
        self._warn_features_without_geometry()
        self._show_route_review()

    def _edited_route_ids(self):
        """Route (multiLoc) ids of the routes edited in Route Review."""
        from qgis.core import QgsProject, QgsVectorLayer
        out = set()
        for r in self.cached_routes:
            if not r.has_edits:
                continue
            layer = QgsProject.instance().mapLayer(r.line_layer_id)
            if not isinstance(layer, QgsVectorLayer) or layer.fields().indexOf("_multi_id") < 0:
                continue
            feat = layer.getFeature(r.line_feature_id)
            if feat.isValid() and feat["_multi_id"]:
                out.add(str(feat["_multi_id"]))
        return out

    def _warn_features_without_geometry(self):
        """Name mapped features that have no geometry: they cannot become
        routes or be pushed, and used to be left out without a word."""
        from .core.export_builder import features_without_position
        missing = features_without_position(self.layer_mappings)
        if not missing:
            return
        total = sum(len(names) for _layer, names in missing)
        detail = "; ".join(
            f"{layer}: {', '.join(names[:5])}"
            + (f" and {len(names) - 5} more" if len(names) > 5 else "")
            for layer, names in missing)
        self.iface.messageBar().pushWarning(
            self.PLUGIN_NAME,
            f"{total} feature(s) have no geometry and will not be pushed "
            f"({detail}). Rows added in the attribute table must be drawn "
            "on the map.",
        )

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
            snap_tolerance=self._routes_snap_tolerance,
            parent=self.iface.mainWindow(),
        )
        self._review_dlg.finished.connect(self._on_review_closed)
        self._review_dlg.show()
        self._review_dlg.raise_()
        self._review_dlg.activateWindow()

    def _on_review_closed(self):
        """Clean up reference when the review dialog is closed."""
        self._review_dlg = None

    def _require_saved_project(self, action: str) -> bool:
        """Block *action* unless the QGIS project has been saved to disk.

        The stamp cache lives beside the project file, so an unsaved project
        makes ``_get_stamp_cache_path()`` return None: nothing is written, no
        LOC IDs persist, and every subsequent push looks like a fresh create.
        Pull has the matching problem — its output lands in the user profile
        folder instead of beside the project.
        """
        from qgis.core import QgsProject

        if QgsProject.instance().absoluteFilePath():
            return True

        QMessageBox.critical(
            self.iface.mainWindow(),
            "Save the QGIS Project First",
            f"This project has not been saved, so {action} cannot continue.\n\n"
            "LOC record IDs are stored in a sidecar file next to the project "
            "(MyProject.qgz → MyProject_loc_stamps.json). Without a project "
            "file there is nowhere to write them, so LOC would treat every "
            "later push as brand-new records instead of updates.\n\n"
            "Save the project, then try again.",
        )
        return False

    def _on_push(self):
        """Build export payload, validate, show preview, then push."""
        if not self._require_saved_project("the push"):
            return

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

        # Pulled layers are temporary (memory) layers: QGIS does not save
        # their features, so a pulled project reopened after QGIS closed
        # holds them empty.  Pushed, every LOC they held reads as deleted
        # in QGIS and one "Yes" bins the location (probe, 15 Nov 2026).
        emptied = [
            layer.name() for layer in
            (project.mapLayer(lm.layer_id) for lm in self.layer_mappings
             if lm.enabled_for_export)
            if layer.providerType() == "memory"
            and layer.fields().indexOf("_loc_id") >= 0
            and layer.featureCount() == 0
            # Pulled in this session, so emptied by the user on purpose.
            and layer.id() not in self._session_pulled_layers
        ]
        if emptied:
            QMessageBox.critical(
                self.iface.mainWindow(), "Pulled Layers Are Empty",
                "These layers came from a pull and are empty:\n\n"
                + "\n".join(f"  - {n}" for n in emptied)
                + "\n\nPulled layers are temporary: QGIS does not save their "
                "features, so they are empty after QGIS is closed and the "
                "project reopened. Pushing them would delete their LOCs.\n\n"
                "Nothing was pushed. Pull again to work on this location, and "
                "to keep a pull between sessions, right-click each \"(LOC)\" "
                "layer and choose Make Permanent before closing QGIS.",
            )
            return

        # Only warn about missing routes if line layers are actually mapped
        has_line_mappings = any(
            lm.enabled_for_export
            and lm.category_id in self._categories_cache
            and self._categories_cache[lm.category_id].is_dual
            for lm in self.layer_mappings
        )
        if not self.cached_routes and has_line_mappings:
            reply = QMessageBox.question(
                self.iface.mainWindow(),
                "No Routes Generated",
                "Route generation has not been run.\n\n"
                "If your data contains line layers (cable routes), you "
                "should generate routes first — otherwise route data "
                "will not be included in the push.\n\n"
                "Continue pushing standalone point assets only?",
                MB_YES | MB_NO,
                MB_NO,
            )
            if reply != MB_YES:
                return

        if not self.auth.current_user:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME, "Not logged in.",
            )
            return

        # Fetch the location's live contents before previewing.  The push
        # is a full replacement, and only the server knows what is
        # currently in the location — anything added from the web or
        # mobile app since the last Pull included.
        if self._push_state_task is not None:
            return  # a push is already being prepared

        from qgis.core import QgsApplication
        from .core.tasks import FetchLocsTask

        self._push_state_task = FetchLocsTask(
            self.api, self.active_location.location_id,
            project_id=self.active_location.project.project_id,
        )
        self._push_state_task.taskCompleted.connect(self._on_push_state_fetched)
        self._push_state_task.taskTerminated.connect(self._on_push_state_failed)
        QgsApplication.taskManager().addTask(self._push_state_task)
        self.iface.messageBar().pushInfo(
            self.PLUGIN_NAME, "Checking current LOC contents...",
        )

    def _on_push_state_failed(self):
        """Live-state fetch failed — refuse to push blind."""
        task = self._push_state_task
        self._push_state_task = None
        msg = (task.error if task else None) or "Unknown error"
        QMessageBox.critical(
            self.iface.mainWindow(),
            "Push Cancelled",
            "Could not read the current contents of this location from "
            f"LOC:\n\n{msg}\n\n"
            "The push is a full replacement, so pushing without knowing "
            "what is on the server risks deleting assets. Check your "
            "connection and try again.",
        )

    def _on_push_state_fetched(self):
        """Build payload, reconcile against live server state, preview."""
        task = self._push_state_task
        self._push_state_task = None
        if task is None:
            return
        server_locs = task.locs_data or {}
        capture_mode = task.capture_mode

        from qgis.core import Qgis, QgsMessageLog
        from .core.export_builder import (
            audit_export_coverage, build_payload, log_payload_metrics,
            known_loc_ids, payload_summary, plan_deletions,
            server_loc_index, validate_payload,
        )
        from .ui.push_preview_dialog import PushPreviewDialog
        from .ui.reconcile_dialog import (
            confirm_deletions, confirm_dropped_layers,
        )

        # The same fetch carries the location's categories, so refresh the
        # cache the audit below is about to consult.  Left stale, a
        # category renamed or removed since the mapping dialog was last
        # opened reads as "missing" and its features are dropped.
        if task.categories:
            self._categories_cache = {
                c.category_id: c for c in task.categories
            }

        # Layers whose features cannot reach the payload at all — most
        # often a category the cache has no entry for, which is what LOCs
        # from the Uncategorised group pull down as.  Without this they
        # are dropped in silence and then binned by the sweep below.
        problems = audit_export_coverage(
            self.layer_mappings, self._categories_cache,
            routes=self.cached_routes or None,
        )
        if not confirm_dropped_layers(self.iface.mainWindow(), problems):
            return

        payload = build_payload(
            routes=self.cached_routes,
            layer_mappings=self.layer_mappings,
            categories=self._categories_cache,
            location=self.active_location,
            user_id=self.auth.current_user.user_id,
            organization_id=self.auth.current_user.organization.org_id,
        )

        # Disabled layers used to get their own warning here.  It fired on
        # every disabled layer whether or not it held features or had ever
        # reached LOC, and `audit_export_coverage` above now covers the
        # same ground precisely — while `confirm_deletions` below names the
        # assets actually at risk.  Three dialogs saying the same thing is
        # how the one that matters gets clicked through.

        # --- Instrumentation: log metrics before push ---
        metrics = log_payload_metrics(payload)
        QgsMessageLog.logMessage(metrics, "LOC Push", Qgis.MessageLevel.Info)

        # --- Build reference data for invariant checks ---
        # Only use in-memory pull counts (set on pull, cleared on regeneration).
        # When cleared, stop-count invariant is simply skipped — the other
        # three structural invariants still run.
        expected_counts = None
        counts_authoritative = False

        if self._pulled_multi_stop_counts:
            # A route changed in Route Review (Toggle Type, Remove) has a
            # different stop count on purpose; the check is for routes the
            # rebuild might have lost stops from.  It refused the push of
            # pull → Generate → Toggle Type (scenario run, 24 Nov 2026).
            edited = self._edited_route_ids()
            expected_counts = {mid: n for mid, n in self._pulled_multi_stop_counts.items()
                               if mid not in edited}
            counts_authoritative = True
            QgsMessageLog.logMessage(
                f"Stop count source: in-memory pull data "
                f"({len(expected_counts)} route(s))",
                "LOC Push", Qgis.MessageLevel.Info,
            )
        else:
            QgsMessageLog.logMessage(
                "Stop count source: none (skipping stop-count invariant)",
                "LOC Push", Qgis.MessageLevel.Info,
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
                MB_YES | MB_NO,
                MB_NO,
            )
            if reply != MB_YES:
                return

        # What this push removes from LOC, from the live snapshot and the
        # server's matching rules.  In a capture-mode project the server
        # deletes nothing, so only records this project knows and the
        # user removed are deleted — by the push task, which also checks
        # afterwards that every confirmed deletion really left LOC.
        # LOC's changes since the pull are kept: every value the map did
        # not change is sent as LOC holds it now, stops LOC added to a
        # route stay on it, and a value changed on both sides is the
        # user's call (Brandon, 10 Nov 2026).
        from qgis.core import QgsProject, QgsVectorLayer
        from .core.rebase import PulledBase, rebase_payload
        from .ui.reconcile_dialog import KEEP_THEIRS, confirm_conflicts
        project = QgsProject.instance()
        mapped = [project.mapLayer(lm.layer_id) for lm in self.layer_mappings]
        rebase = rebase_payload(
            payload,
            PulledBase.from_layers([l for l in mapped if isinstance(l, QgsVectorLayer)]),
            server_locs,
        )
        QgsMessageLog.logMessage(
            f"Merge with LOC: {len(rebase.kept_from_loc)} record(s) keep LOC's "
            f"changes, {len(rebase.stops_kept)} stop(s) added in LOC kept, "
            f"{len(rebase.stops_gone)} removed in LOC, {len(rebase.conflicts)} "
            f"conflict(s), {rebase.local_changes} change(s) on the map, "
            f"{rebase.without_base} record(s) with no pulled copy",
            "LOC Push", Qgis.MessageLevel.Info,
        )
        if rebase.conflicts:
            keep = confirm_conflicts(self.iface.mainWindow(), rebase.conflicts)
            if keep is None:
                return
            rebase.resolve(keep_theirs=keep == KEEP_THEIRS)

        location_id = self.active_location.location_id
        known = known_loc_ids(location_id)
        if self._pulled_loc_ids:
            known = (known or set()) | set(self._pulled_loc_ids)
        plan = plan_deletions(payload, server_locs, capture_mode, known)
        doomed = plan.doomed
        total_server = len(server_loc_index(server_locs))
        QgsMessageLog.logMessage(
            f"Project capture mode: {capture_mode}; "
            f"{len(doomed)} LOC(s) to delete, {len(plan.kept)} kept "
            "(captured in the field since the last pull)",
            "LOC Push", Qgis.MessageLevel.Info,
        )
        if doomed:
            QgsMessageLog.logMessage(
                "Push would delete "
                + ", ".join(f"{uid} ({ltype})" for uid, ltype in doomed),
                "LOC Push", Qgis.MessageLevel.Warning,
            )
        # The map is behind LOC: field captures it has never seen, or
        # records and routes changed in LOC since the pull.  The push keeps
        # them all; say so, and offer a pull only when that cannot replace
        # changes the user has not pushed yet.
        captures = ([(uid or "(no asset ID)", ltype) for _l, uid, ltype in plan.kept]
                    if plan.capture_mode and plan.known else [])
        if captures or rebase.loc_changes:
            from .ui.reconcile_dialog import (
                STALE_PULL, STALE_PUSH, confirm_stale_map,
            )
            answer = confirm_stale_map(
                self.iface.mainWindow(), captures,
                changed_in_loc=rebase.kept_from_loc,
                stops_added=rebase.stops_kept, stops_removed=rebase.stops_gone,
                local_changes=rebase.local_changes + len(plan.delete_ids),
            )
            if answer == STALE_PULL:
                self._place_field_captures(
                    server_locs, [lid for lid, _u, _t in plan.kept])
                return
            if answer != STALE_PUSH:
                return
        if not confirm_deletions(
            self.iface.mainWindow(), doomed, total_server,
            capture_mode=bool(capture_mode), kept=len(plan.kept),
            known=plan.known,
        ):
            return
        self._pending_delete_ids = plan.delete_ids
        self._pending_rebase = rebase

        summary = payload_summary(payload)
        # Routes that matched no structures push with line-endpoint
        # coordinates — surface this so the operator confirms knowingly.
        summary["routes_unmatched"] = sum(
            1 for r in self.cached_routes if r.is_unmatched
        )
        summary["will_be_deleted"] = len(doomed)

        dlg = PushPreviewDialog(
            api_client=self.api,
            payload=payload,
            local_summary=summary,
            parent=self.iface.mainWindow(),
        )
        dlg.push_confirmed.connect(self._execute_push)
        dlg.exec()

    def _place_field_captures(self, server_locs, loc_ids):
        """Pull now: add what was captured in the field to the layers
        mapped to its category.  The mapping and the reviewed routes stay
        as they are; nothing is pushed."""
        from .core.field_merge import place_field_captures
        report = place_field_captures(
            server_locs, loc_ids, self.layer_mappings,
            self._categories_cache, self.active_location.location_id,
        )
        if report.placed:
            # The layers changed, so the next Generate runs again; a new
            # structure on a cable becomes one of its stops.
            self._last_fingerprint = ""
        lines = []
        if report.placed:
            lines.append(f"Added {len(report.placed)} asset(s) captured in the "
                         "field to your layers:")
            lines += [f"  - {uid}  ({layer})" for uid, layer in report.placed[:15]]
            if len(report.placed) > 15:
                lines.append(f"  ... and {len(report.placed) - 15} more")
        if report.skipped:
            if lines:
                lines.append("")
            lines.append(f"{len(report.skipped)} asset(s) were not added. LOC "
                         "keeps them, and the push leaves them as they are:")
            lines += [f"  - {uid}: {why}" for uid, why in report.skipped[:15]]
            if len(report.skipped) > 15:
                lines.append(f"  ... and {len(report.skipped) - 15} more")
        lines += ["", "Review them on the map, then Generate and Push."
                  if report.placed else "Push when ready."]
        QMessageBox.information(self.iface.mainWindow(),
                                "Field Captures Added" if report.placed
                                else "No Field Captures Added", "\n".join(lines))

    def _execute_push(self, payload):
        """Execute the real push after user confirms in the preview."""
        if self._push_task is not None:
            return  # already running

        from qgis.core import QgsApplication
        from .core.tasks import PushTask

        self._push_task = PushTask(
            self.api, payload,
            delete_ids=self._pending_delete_ids,
        )
        self._pending_delete_ids = []
        self._push_rebase, self._pending_rebase = self._pending_rebase, None
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
            from .core.tasks import push_diagnostics
            detail_lines = push_diagnostics(task)

            # Distinguish true network failures from server-side 500s.
            # The server sometimes returns HTTP 500 (with a validation
            # message or "Operation timeout") but still commits the data.
            # Only treat as hard failure when we never got a response
            # (client-side timeout / connection error) or the status
            # indicates a proxy/gateway issue (502-504).  Decided by the
            # error type, not its wording ("timed out" vs "timeout").
            is_hard_failure = (
                getattr(task, "network_error", False)
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
                # Data may have been committed — stamp IDs to be safe
                from .core.export_builder import stamp_ids_after_push
                stamp_ids_after_push(task.payload)
                # Still open LOC so the user can verify
                self._open_loc_page()
            return

        # Stamp payload IDs onto layer features so subsequent pushes
        # are treated as updates rather than creates.
        from .core.export_builder import (
            payload_loc_ids, remember_loc_ids, stamp_ids_after_push,
        )
        stamp_ids_after_push(task.payload)
        # The pulled copies move forward to what the user pushed.
        rebase, self._push_rebase = self._push_rebase, None
        if rebase is not None and rebase.base_updates:
            from qgis.core import QgsProject, QgsVectorLayer
            from .core.rebase import apply_base_updates
            layers = [QgsProject.instance().mapLayer(lm.layer_id)
                      for lm in self.layer_mappings]
            apply_base_updates([l for l in layers if isinstance(l, QgsVectorLayer)],
                               rebase.base_updates)
        # Pulled routes take what was pushed: stops, vertices, ids.
        from .core.export_builder import refresh_pulled_routes
        refresh_pulled_routes(task.payload)
        for m in task.payload.get("multiLocs", []):
            if m.get("id") in self._pulled_multi_stop_counts:
                self._pulled_multi_stop_counts[m["id"]] = len(m.get("Stops") or [])
        remember_loc_ids(task.payload.get("location_id", ""),
                         payload_loc_ids(task.payload),
                         forget=task.deleted_ids)

        message = task.message or "Push complete."
        if task.deleted_ids:
            message += (f" {len(task.deleted_ids)} LOC(s) moved to the "
                        "recycle bin.")
        if task.not_deleted:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Some LOCs Were Not Deleted",
                f"The push succeeded, but {len(task.not_deleted)} of the "
                f"{len(task.delete_ids)} LOC(s) you deleted are still in "
                "LOC:\n\n"
                + "\n".join(f"  - {i}" for i in task.not_deleted[:15])
                + (f"\n  ... and {len(task.not_deleted) - 15} more"
                   if len(task.not_deleted) > 15 else "")
                + (f"\n\nServer answer: {task.delete_error}"
                   if task.delete_error else "")
                + "\n\nDelete them in LOC, or push again.",
            )
            self.iface.messageBar().pushWarning(self.PLUGIN_NAME, message)
        else:
            self.iface.messageBar().pushSuccess(self.PLUGIN_NAME, message)
        self._open_loc_page()

    def _open_loc_page(self):
        """Open the LOC web page for the active location."""
        import webbrowser
        from .services.config import get_base_url
        base = get_base_url()
        if self.active_location:
            loc_id = self.active_location.location_id
            webbrowser.open(f"{base}/locations/{loc_id}/locs")
        else:
            webbrowser.open(base)

    # ------------------------------------------------------------------
    # Pull from LOC
    # ------------------------------------------------------------------

    def _on_pull(self):
        """Open the pull dialog to import LOC data into QGIS layers."""
        if not self._require_saved_project("the pull"):
            return

        if not self.active_location:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Select a Location in the Workspace sidebar first.",
            )
            return

        if not self._confirm_full_pull():
            return

        from .ui.pull_dialog import PullDialog

        dlg = PullDialog(
            api_client=self.api,
            location_id=self.active_location.location_id,
            location_name=self.active_location.name,
            parent=self.iface.mainWindow(),
        )
        dlg.import_complete.connect(self._on_pull_complete)
        dlg.exec()

        # Cache categories fetched by the dialog
        if dlg._categories:
            self._categories_cache = {
                c.category_id: c for c in dlg._categories
            }

    def _confirm_full_pull(self) -> bool:
        """Before a pull over a mapping in use: a pull adds a full copy of
        the location as new "(LOC)" layers and moves the mapping onto
        them.  Over the user's own layers that silently cost Brandon his
        mapping (15 Nov 2026), so say so, point at Pull now for field
        captures, and keep the mapping as a preset to load back."""
        from qgis.core import QgsProject, QgsVectorLayer
        project = QgsProject.instance()
        live = [(lm, project.mapLayer(lm.layer_id)) for lm in self.layer_mappings]
        live = [(lm, l) for lm, l in live if isinstance(l, QgsVectorLayer)]
        if not live:
            return True
        own = [lm for lm, l in live if l.fields().indexOf("_loc_id") < 0]
        preset = ""
        if own:
            from datetime import datetime
            from .models.mapping import MappingPreset, save_preset
            preset = (f"{self.active_location.name} before pull "
                      f"{datetime.now():%Y-%m-%d %H:%M}")
            text = (
                "Category Mapping uses your own layers. Pull adds a full copy "
                "of this location as new \"(LOC)\" layers and moves the mapping "
                "onto them; your layers are not changed.\n\n"
                "To bring in only the assets captured in the field, onto your "
                "own layers, cancel and click Push to LOC: the Map Is Behind "
                "LOC prompt offers Pull now.\n\n"
                f"Your current mapping will be saved as the preset \"{preset}\", "
                "so you can load it back in Category Mapping.")
        else:
            text = (
                "This project already holds a pull of this location. Pulling "
                "again adds a second copy as new \"(LOC)\" layers and moves "
                "the mapping onto them; edits on the current layers that are "
                "not pushed stay on those layers and are no longer pushed.\n\n"
                "Remove the old \"(LOC)\" layers after the pull to avoid "
                "confusion.")
        box = QMessageBox(QMessageBox.Icon.Question, "Pull a Full Copy?", text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                          self.iface.mainWindow())
        box.button(QMessageBox.StandardButton.Yes).setText("Pull a Copy")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        if preset:
            save_preset(MappingPreset(name=preset, layer_mappings=list(self.layer_mappings),
                                      snap_tolerance=self.snap_tolerance))
        return True

    def _on_pull_complete(self, layers, mappings, pulled_ids,
                          multi_stop_counts=None):
        """Handle imported layers from the pull dialog."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        for layer in layers:
            project.addMapLayer(layer)
            self._session_pulled_layers.add(layer.id())

        self.layer_mappings = mappings
        self._pulled_loc_ids = pulled_ids
        from .core.field_merge import forget_field_captures
        forget_field_captures(project)
        # Remember what this project holds, so a capture-mode push can
        # tell a LOC deleted here from one captured in the field later.
        from .core.export_builder import remember_loc_ids
        if self.active_location is not None:
            remember_loc_ids(self.active_location.location_id, pulled_ids)
        self._pulled_multi_stop_counts = multi_stop_counts or {}
        from .core.export_builder import set_layer_mappings
        set_layer_mappings(mappings)

        # Auto-reconstruct routes from pulled layer geometry so the user
        # can review/push without running the snapping-based generator
        # (which fails on dense pulled data).
        from .core.import_builder import reconstruct_routes
        from .core.change_detector import compute_fingerprint

        self.cached_routes = reconstruct_routes(mappings, skip_snapping=True)
        self._last_fingerprint = compute_fingerprint(mappings)

        n_layers = len(layers)
        n_locs = len(pulled_ids)
        n_routes = len(self.cached_routes)
        self.iface.messageBar().pushSuccess(
            self.PLUGIN_NAME,
            f"Imported {n_locs} LOC(s) into {n_layers} layer(s) "
            f"— {n_routes} route(s) ready for review.",
        )

    # ------------------------------------------------------------------
    # Stamp sync — match existing QGIS features to server LOC records
    # ------------------------------------------------------------------

    def _on_sync_stamps(self):
        """Fetch server LOC data and match to existing QGIS features."""
        if not self.active_location:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Select a Location in the Workspace sidebar first.",
            )
            return

        if not self.layer_mappings:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                "Set up category mappings first (Map Categories button).",
            )
            return

        from qgis.core import QgsApplication
        from .core.tasks import FetchLocsTask

        self._sync_task = FetchLocsTask(
            self.api, self.active_location.location_id,
        )
        self._sync_task.taskCompleted.connect(self._on_sync_fetched)
        self._sync_task.taskTerminated.connect(self._on_sync_failed)
        QgsApplication.taskManager().addTask(self._sync_task)
        self.iface.messageBar().pushInfo(
            self.PLUGIN_NAME, "Syncing with LOC server...",
        )

    def _on_sync_fetched(self):
        """Handle completed sync fetch — match features to server data."""
        task = self._sync_task
        if task.locs_data is None:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME, "Sync failed: no data returned from server.",
            )
            return

        from .core.export_builder import sync_stamps_from_server
        from .ui.reconcile_dialog import show_sync_report

        report = sync_stamps_from_server(
            task.locs_data,
            self.active_location.location_id,
            self.layer_mappings,
        )
        # The matched server records are now held by this project, so a
        # capture-mode push may delete them once they are removed here.
        from .core.export_builder import remember_loc_ids, server_loc_index
        unmatched = set(report.unmatched_server)
        remember_loc_ids(self.active_location.location_id, [
            lid for lid, (uid, ltype) in server_loc_index(task.locs_data).items()
            if (uid, ltype) not in unmatched
            and (uid or "(no asset ID)", ltype) not in unmatched
        ])

        # A bare "matched N" reads as success even when N is 0 and every
        # server LOC is queued for deletion, so the residual gets a modal
        # rather than a message-bar line.
        show_sync_report(self.iface.mainWindow(), report)

        if report.ok:
            self.iface.messageBar().pushSuccess(
                self.PLUGIN_NAME,
                f"Sync complete: matched {report.matched} feature(s) "
                f"to {report.total_server} server LOC(s).",
            )
        else:
            self.iface.messageBar().pushWarning(
                self.PLUGIN_NAME,
                f"Sync matched {report.matched} of {report.total_server} "
                f"server LOC(s) — {len(report.unmatched_server)} unmatched.",
            )

    def _on_sync_failed(self):
        """Handle failed sync fetch."""
        task = self._sync_task
        msg = task.error or "Unknown error"
        self.iface.messageBar().pushCritical(
            self.PLUGIN_NAME, f"Sync failed: {msg}",
        )
