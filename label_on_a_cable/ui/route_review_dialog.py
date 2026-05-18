"""Route review & commit dialog.

Shows a master-detail pre-commit review interface:
  - Summary banner: routes/stops counts, change states, push readiness
  - Routes table: route details with change status and delta column
  - Stops table: stops for selected route with toolbar and checkboxes
  - Push to LOC / Cancel buttons

The dialog is modeless so the user can see the map canvas behind it.
Selected routes and stops are highlighted on the map.
"""

from typing import List, Optional

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsHighlight, QgsMapCanvas

from ..qt_compat import (
    NON_MODAL, VERTICAL, ALIGN_CENTER,
    HV_STRETCH, HV_RESIZE_TO_CONTENTS, HV_FIXED,
    AIV_SELECT_ROWS, AIV_SINGLE_SELECTION, AIV_NO_EDIT_TRIGGERS,
    FRAME_STYLED_PANEL, FRAME_NO_FRAME,
)
from ..models.route import Route, Stop, StopType


# Column indices -- routes table
_R_COL_NAME = 0
_R_COL_CAT = 1
_R_COL_ORIGIN = 2
_R_COL_DEST = 3
_R_COL_STOPS = 4
_R_COL_STATUS = 5
_R_COL_DELTA = 6
_R_HEADERS = ["Route", "LOC Category", "Origin", "Destination", "Stops", "Status", "+/\u2212"]

# Column indices -- stops table
_S_COL_NO = 0
_S_COL_LABEL = 1
_S_COL_STRUCT = 2
_S_COL_TYPE = 3
_S_COL_REMOVED = 4
_S_HEADERS = ["No.", "Label", "Structure", "Type", "Removed"]

# Highlight colours
_ROUTE_COLOR = QColor(50, 120, 255, 180)     # blue
_ROUTE_FILL = QColor(50, 120, 255, 40)
_STOP_COLOR = QColor(255, 60, 60, 200)       # red
_STOP_FILL = QColor(255, 60, 60, 80)

# Row background colours for stops
_BG_REMOVED = QColor(245, 245, 245)          # light gray
_BG_MODIFIED = QColor(255, 253, 231)         # light yellow

# Status text colours for routes
_STATUS_COLORS = {
    "New": QColor(46, 125, 50),               # green
    "Modified": QColor(230, 126, 34),         # orange
    "Contains removed stops": QColor(192, 57, 43),  # red
    "No changes": QColor(128, 128, 128),      # gray
}


class RouteReviewDialog(QDialog):
    """Modeless dialog for reviewing generated routes before push.

    Highlights the selected route / stop on the QGIS map canvas.
    Supports per-stop edits: rename, remove, toggle type.
    Emits ``push_requested`` when the user clicks Push to LOC.
    """

    def __init__(self, routes: List[Route], canvas: QgsMapCanvas, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Route Review & Commit")
        self.setMinimumSize(780, 520)
        self.resize(860, 600)

        # Allow interaction with the map while dialog is open
        self.setWindowModality(NON_MODAL)

        self._routes = routes
        self._canvas = canvas
        self._route_highlights: List[QgsHighlight] = []
        self._stop_highlights: List[QgsHighlight] = []
        self._closing = False

        self._build_ui()
        self._populate_routes()
        self._update_summary()

        # Auto-select first route
        if self._routes:
            self._route_table.selectRow(0)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # --- Change Summary Banner ---
        self._summary_frame = QFrame()
        self._summary_frame.setFrameShape(FRAME_STYLED_PANEL)
        self._summary_frame.setStyleSheet(
            "QFrame { background: #f0f4f8; border: 1px solid #d0d7de; "
            "border-radius: 6px; padding: 10px; }"
        )
        summary_layout = QVBoxLayout(self._summary_frame)
        summary_layout.setSpacing(2)
        summary_layout.setContentsMargins(12, 8, 12, 8)

        self._summary_counts = QLabel()
        self._summary_counts.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #24292f;"
        )
        summary_layout.addWidget(self._summary_counts)

        self._summary_changes = QLabel()
        self._summary_changes.setStyleSheet(
            "font-size: 12px; color: #57606a;"
        )
        summary_layout.addWidget(self._summary_changes)

        self._summary_state = QLabel()
        self._summary_state.setStyleSheet(
            "font-size: 12px; font-weight: bold;"
        )
        summary_layout.addWidget(self._summary_state)

        root.addWidget(self._summary_frame)

        # --- Splitter: routes on top, stops on bottom ---
        splitter = QSplitter(VERTICAL)

        # --- Routes section ---
        routes_widget = QWidget()
        routes_layout = QVBoxLayout(routes_widget)
        routes_layout.setContentsMargins(0, 4, 0, 0)
        routes_layout.setSpacing(4)

        routes_header = QLabel("Routes")
        routes_header.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #24292f;"
        )
        routes_layout.addWidget(routes_header)

        self._route_table = QTableWidget(0, len(_R_HEADERS))
        self._route_table.setHorizontalHeaderLabels(_R_HEADERS)
        self._route_table.setSelectionBehavior(AIV_SELECT_ROWS)
        self._route_table.setSelectionMode(AIV_SINGLE_SELECTION)
        self._route_table.setEditTriggers(AIV_NO_EDIT_TRIGGERS)
        self._route_table.horizontalHeader().setStretchLastSection(False)
        self._route_table.horizontalHeader().setSectionResizeMode(
            _R_COL_NAME, HV_STRETCH,
        )
        self._route_table.horizontalHeader().setSectionResizeMode(
            _R_COL_CAT, HV_RESIZE_TO_CONTENTS,
        )
        self._route_table.horizontalHeader().setSectionResizeMode(
            _R_COL_STATUS, HV_RESIZE_TO_CONTENTS,
        )
        self._route_table.horizontalHeader().setSectionResizeMode(
            _R_COL_DELTA, HV_FIXED,
        )
        self._route_table.setColumnWidth(_R_COL_DELTA, 50)
        self._route_table.verticalHeader().setVisible(False)
        self._route_table.verticalHeader().setDefaultSectionSize(30)
        self._route_table.setAlternatingRowColors(True)
        self._route_table.selectionModel().selectionChanged.connect(
            self._on_route_selected,
        )
        routes_layout.addWidget(self._route_table)
        splitter.addWidget(routes_widget)

        # --- Stops section ---
        stops_widget = QWidget()
        stops_layout = QVBoxLayout(stops_widget)
        stops_layout.setContentsMargins(0, 4, 0, 0)
        stops_layout.setSpacing(4)

        self._stops_header = QLabel("Stops")
        self._stops_header.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #24292f;"
        )
        stops_layout.addWidget(self._stops_header)

        # Stop toolbar (above table)
        toolbar = QFrame()
        toolbar.setFrameShape(FRAME_NO_FRAME)
        toolbar.setStyleSheet(
            "QFrame { background: #f6f8fa; border: 1px solid #d0d7de; "
            "border-radius: 4px; padding: 2px; }"
        )
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(6, 3, 6, 3)
        toolbar_layout.setSpacing(6)

        self._btn_rename = QPushButton("Rename")
        self._btn_rename.setToolTip(
            "Edit the display label for the selected stop"
        )
        self._btn_rename.setFixedHeight(26)
        self._btn_rename.clicked.connect(self._on_rename)
        self._btn_rename.setEnabled(False)
        toolbar_layout.addWidget(self._btn_rename)

        self._btn_toggle_type = QPushButton("Toggle Type")
        self._btn_toggle_type.setToolTip(
            "Switch between Passthrough and Ingress/Egress"
        )
        self._btn_toggle_type.setFixedHeight(26)
        self._btn_toggle_type.clicked.connect(self._on_toggle_type)
        self._btn_toggle_type.setEnabled(False)
        toolbar_layout.addWidget(self._btn_toggle_type)

        self._btn_remove = QPushButton("Remove")
        self._btn_remove.setToolTip("Remove or restore the selected stop")
        self._btn_remove.setFixedHeight(26)
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_remove.setEnabled(False)
        toolbar_layout.addWidget(self._btn_remove)

        toolbar_layout.addStretch()
        stops_layout.addWidget(toolbar)

        # Stop table
        self._stop_table = QTableWidget(0, len(_S_HEADERS))
        self._stop_table.setHorizontalHeaderLabels(_S_HEADERS)
        self._stop_table.setSelectionBehavior(AIV_SELECT_ROWS)
        self._stop_table.setSelectionMode(AIV_SINGLE_SELECTION)
        self._stop_table.setEditTriggers(AIV_NO_EDIT_TRIGGERS)
        self._stop_table.horizontalHeader().setStretchLastSection(True)
        self._stop_table.horizontalHeader().setSectionResizeMode(
            _S_COL_LABEL, HV_STRETCH,
        )
        self._stop_table.setColumnWidth(_S_COL_NO, 40)
        self._stop_table.setColumnWidth(_S_COL_REMOVED, 70)
        self._stop_table.verticalHeader().setVisible(False)
        self._stop_table.verticalHeader().setDefaultSectionSize(28)
        self._stop_table.horizontalHeaderItem(_S_COL_TYPE).setToolTip(
            "Passthrough = single label.\n"
            "Ingress/Egress = paired IN/OUT labels at the same structure."
        )
        self._stop_table.selectionModel().selectionChanged.connect(
            self._on_stop_selected,
        )
        stops_layout.addWidget(self._stop_table)
        splitter.addWidget(stops_widget)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter)

        # --- Bottom button ---
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        btn_ok = QPushButton("OK")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.close)
        btn_bar.addWidget(btn_ok)

        root.addLayout(btn_bar)

    # ------------------------------------------------------------------
    # Summary banner
    # ------------------------------------------------------------------

    def _update_summary(self):
        """Recompute and display the change summary banner."""
        total_routes = len(self._routes)
        total_stops = sum(r.active_stop_count for r in self._routes)

        self._summary_counts.setText(
            f"{total_routes} route{'s' if total_routes != 1 else ''}  "
            f"\u00b7  "
            f"{total_stops} intermediate stop{'s' if total_stops != 1 else ''}"
        )

        # Compute per-route change stats
        new_count = 0
        modified_count = 0
        removed_routes_count = 0
        for route in self._routes:
            state = self._route_change_state(route)
            if state == "New":
                new_count += 1
            elif state == "Modified":
                modified_count += 1
            elif state == "Contains removed stops":
                removed_routes_count += 1

        parts = []
        parts.append(f"{new_count} new")
        parts.append(f"{modified_count} modified")
        parts.append(f"{removed_routes_count} with removed stops")
        self._summary_changes.setText("  \u00b7  ".join(parts))

        # Overall state
        if total_routes == 0:
            self._summary_state.setText("No changes detected")
            self._summary_state.setStyleSheet(
                "font-size: 12px; font-weight: bold; color: #57606a;"
            )
        else:
            self._summary_state.setText("\u25cf  Ready to push")
            self._summary_state.setStyleSheet(
                "font-size: 12px; font-weight: bold; color: #2da44e;"
            )

    # ------------------------------------------------------------------
    # Route change state helpers
    # ------------------------------------------------------------------

    def _route_change_state(self, route: Route) -> str:
        """Determine the change state of a route for display."""
        removed_count = sum(
            1 for s in route.stops if s.removed and not s.is_endpoint
        )
        if removed_count > 0:
            return "Contains removed stops"

        if route.has_edits:
            return "Modified"

        # Check if route exists on the server — either pulled (layer field)
        # or previously pushed (sidecar stamp cache, scoped by location).
        if route.line_layer_id:
            project = QgsProject.instance()
            layer = project.mapLayer(route.line_layer_id)
            if isinstance(layer, QgsVectorLayer):
                has_stamp = False
                cached_stop_count = -1

                # Pulled memory layers have valid _loc_id as a field
                if "_loc_id" in layer.fields().names():
                    feat = layer.getFeature(route.line_feature_id)
                    if feat.isValid():
                        loc_id = feat.attribute("_loc_id")
                        if loc_id and len(str(loc_id)) == 36:
                            has_stamp = True

                # Sidecar stamp cache (for Shapefile layers after push)
                if not has_stamp:
                    from ..core.export_builder import _get_stamp_value
                    cached = _get_stamp_value(
                        layer, route.line_feature_id, "_loc_id")
                    if cached:
                        has_stamp = True

                if has_stamp:
                    # Compare stop count to detect structural changes
                    from ..core.export_builder import _get_stamp_value
                    stop_ids_raw = _get_stamp_value(
                        layer, route.line_feature_id, "_stop_ids")
                    if stop_ids_raw:
                        cached_stop_count = len([
                            s for s in stop_ids_raw.split(",")
                            if len(s) == 36
                        ])
                    else:
                        # No cached stops — route had 0 stops last push
                        cached_stop_count = 0

                    current_stops = len(route.intermediate_stops)
                    if cached_stop_count != current_stops:
                        return "Modified"
                    return "No changes"

        return "New"

    def _route_delta(self, route: Route) -> str:
        """Compute a change delta string for the route."""
        removed = sum(
            1 for s in route.stops if s.removed and not s.is_endpoint
        )
        if removed > 0:
            return f"\u2212{removed}"
        return ""

    # ------------------------------------------------------------------
    # Populate routes table
    # ------------------------------------------------------------------

    def _populate_routes(self):
        self._route_table.setRowCount(len(self._routes))
        for row, route in enumerate(self._routes):
            # Route name
            self._route_table.setItem(
                row, _R_COL_NAME,
                QTableWidgetItem(route.line_name or route.route_id[:8]),
            )

            # LOC Category
            self._route_table.setItem(
                row, _R_COL_CAT,
                QTableWidgetItem(route.category_name or ""),
            )

            # Origin
            self._route_table.setItem(
                row, _R_COL_ORIGIN, QTableWidgetItem(route.origin),
            )

            # Destination
            self._route_table.setItem(
                row, _R_COL_DEST, QTableWidgetItem(route.destination),
            )

            # Stops count (centered)
            stops_item = QTableWidgetItem(str(route.active_stop_count))
            stops_item.setTextAlignment(ALIGN_CENTER)
            self._route_table.setItem(row, _R_COL_STOPS, stops_item)

            # Status (coloured + bold)
            state = self._route_change_state(route)
            status_item = QTableWidgetItem(state)
            color = _STATUS_COLORS.get(state, QColor(128, 128, 128))
            status_item.setForeground(color)
            font = status_item.font()
            font.setBold(True)
            status_item.setFont(font)
            self._route_table.setItem(row, _R_COL_STATUS, status_item)

            # Delta column
            delta = self._route_delta(route)
            delta_item = QTableWidgetItem(delta)
            delta_item.setTextAlignment(ALIGN_CENTER)
            if delta:
                delta_item.setForeground(QColor(192, 57, 43))
            self._route_table.setItem(row, _R_COL_DELTA, delta_item)

    # ------------------------------------------------------------------
    # Route selection -> populate stops + highlight
    # ------------------------------------------------------------------

    def _on_route_selected(self):
        self._clear_stop_highlights()
        route = self._selected_route()
        if route is None:
            self._stop_table.setRowCount(0)
            self._stops_header.setText("Stops")
            self._clear_route_highlights()
            self._update_edit_buttons()
            return

        # Human-readable header: "Stops for Route X (Origin -> Dest)"
        route_name = route.line_name or route.route_id[:8]
        self._stops_header.setText(
            f"Stops for {route_name} "
            f"({route.origin} \u2192 {route.destination})"
        )
        self._populate_stops(route)
        self._highlight_route(route)
        self._update_edit_buttons()

    def _selected_route(self) -> Optional[Route]:
        rows = self._route_table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        if 0 <= idx < len(self._routes):
            return self._routes[idx]
        return None

    # ------------------------------------------------------------------
    # Populate stops table
    # ------------------------------------------------------------------

    def _populate_stops(self, route: Route):
        stops = route.stops
        self._stop_table.setRowCount(len(stops))

        for row, stop in enumerate(stops):
            is_removed = stop.removed
            is_modified = (
                stop.display_name != stop.original_name and not is_removed
            )

            # Determine row background
            if is_removed:
                bg = _BG_REMOVED
            elif is_modified:
                bg = _BG_MODIFIED
            else:
                bg = None

            # Strikethrough font for removed stops
            font = QFont()
            if is_removed:
                font.setStrikeOut(True)

            # Foreground colour
            fg = Qt.gray if is_removed else None

            # No. column -- blank for origin/destination
            no_text = str(stop.stop_number) if stop.stop_number > 0 else ""
            no_item = QTableWidgetItem(no_text)
            no_item.setTextAlignment(ALIGN_CENTER)
            if fg:
                no_item.setForeground(fg)
            if bg:
                no_item.setBackground(bg)
            no_item.setFont(font)
            self._stop_table.setItem(row, _S_COL_NO, no_item)

            # Label
            label_item = QTableWidgetItem(stop.display_name)
            if fg:
                label_item.setForeground(fg)
            if bg:
                label_item.setBackground(bg)
            label_item.setFont(font)
            self._stop_table.setItem(row, _S_COL_LABEL, label_item)

            # Structure
            struct_item = QTableWidgetItem(stop.structure_name)
            if fg:
                struct_item.setForeground(fg)
            if bg:
                struct_item.setBackground(bg)
            struct_item.setFont(font)
            self._stop_table.setItem(row, _S_COL_STRUCT, struct_item)

            # Type
            type_item = QTableWidgetItem(_stop_type_display(stop.stop_type))
            if fg:
                type_item.setForeground(fg)
            if bg:
                type_item.setBackground(bg)
            type_item.setFont(font)
            self._stop_table.setItem(row, _S_COL_TYPE, type_item)

            # Removed -- checkbox for intermediate stops, blank for endpoints
            if not stop.is_endpoint:
                container = QWidget()
                cb_layout = QHBoxLayout(container)
                cb_layout.setAlignment(ALIGN_CENTER)
                cb_layout.setContentsMargins(0, 0, 0, 0)
                cb = QCheckBox()
                cb.setChecked(stop.removed)
                cb.toggled.connect(
                    lambda checked, r=route, s=stop:
                        self._on_remove_checkbox(r, s, checked)
                )
                cb_layout.addWidget(cb)
                self._stop_table.setCellWidget(
                    row, _S_COL_REMOVED, container,
                )
            else:
                empty_item = QTableWidgetItem("")
                if bg:
                    empty_item.setBackground(bg)
                self._stop_table.setItem(row, _S_COL_REMOVED, empty_item)

    def _on_remove_checkbox(self, route: Route, stop: Stop, checked: bool):
        """Handle the removed checkbox toggle."""
        stop.removed = checked

        # Toggle partner for IN/OUT pairs
        if stop.stop_type in (StopType.INGRESS, StopType.EGRESS):
            pair_type = (
                StopType.EGRESS if stop.stop_type == StopType.INGRESS
                else StopType.INGRESS
            )
            for s in route.stops:
                if (s is not stop
                        and s.stop_number == stop.stop_number
                        and s.structure_name == stop.structure_name
                        and s.stop_type == pair_type):
                    s.removed = checked
                    break

        route.has_edits = True
        # Defer refresh to avoid destroying the checkbox during its signal
        if not self._closing:
            QTimer.singleShot(0, self._refresh_current)

    # ------------------------------------------------------------------
    # Stop selection -> highlight + update buttons
    # ------------------------------------------------------------------

    def _on_stop_selected(self):
        self._clear_stop_highlights()
        route = self._selected_route()
        if route is None:
            self._update_edit_buttons()
            return

        stop = self._selected_stop(route)
        if stop is not None:
            self._highlight_stop(stop)
        self._update_edit_buttons()

    def _selected_stop(self, route: Route) -> Optional[Stop]:
        rows = self._stop_table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        if 0 <= idx < len(route.stops):
            return route.stops[idx]
        return None

    def _selected_stop_index(self) -> int:
        """Return the row index of the selected stop, or -1."""
        rows = self._stop_table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    # ------------------------------------------------------------------
    # Edit button state
    # ------------------------------------------------------------------

    def _update_edit_buttons(self):
        """Enable/disable edit buttons based on the selected stop."""
        route = self._selected_route()
        stop = self._selected_stop(route) if route else None

        has_stop = stop is not None
        is_intermediate = has_stop and not stop.is_endpoint

        self._btn_rename.setEnabled(has_stop)
        self._btn_remove.setEnabled(is_intermediate)
        self._btn_toggle_type.setEnabled(is_intermediate)

        # Update Remove button text
        if stop and stop.removed:
            self._btn_remove.setText("Restore")
        else:
            self._btn_remove.setText("Remove")

    # ------------------------------------------------------------------
    # Edit actions
    # ------------------------------------------------------------------

    def _on_rename(self):
        """Rename the selected stop's display label."""
        route = self._selected_route()
        if route is None:
            return
        stop = self._selected_stop(route)
        if stop is None:
            return

        new_name, ok = QInputDialog.getText(
            self, "Rename Stop",
            "Display label:",
            text=stop.display_name,
        )
        if not ok or not new_name.strip():
            return

        stop.display_name = new_name.strip()
        route.has_edits = True
        self._refresh_current()

    def _on_remove(self):
        """Toggle the removed flag on the selected intermediate stop.

        For ingress/egress pairs, both stops are toggled together.
        """
        route = self._selected_route()
        if route is None:
            return
        stop = self._selected_stop(route)
        if stop is None or stop.is_endpoint:
            return

        new_state = not stop.removed
        stop.removed = new_state

        # If part of an ingress/egress pair, toggle the partner too
        if stop.stop_type in (StopType.INGRESS, StopType.EGRESS):
            pair_type = (
                StopType.EGRESS if stop.stop_type == StopType.INGRESS
                else StopType.INGRESS
            )
            for s in route.stops:
                if (s is not stop
                        and s.stop_number == stop.stop_number
                        and s.structure_name == stop.structure_name
                        and s.stop_type == pair_type):
                    s.removed = new_state
                    break

        route.has_edits = True
        self._refresh_current()

    def _on_toggle_type(self):
        """Toggle stop type: passthrough <-> ingress/egress.

        Passthrough -> inserts a paired _IN + _OUT (Ingress then Egress).
        Ingress or Egress -> collapses the pair back into one Passthrough.
        """
        route = self._selected_route()
        if route is None:
            return
        stop_idx = self._selected_stop_index()
        if stop_idx < 0 or stop_idx >= len(route.stops):
            return
        stop = route.stops[stop_idx]
        if stop.is_endpoint:
            return

        line_name = route.line_name
        struct = stop.structure_name

        if stop.stop_type == StopType.PASSTHROUGH:
            # Convert to Ingress + insert Egress immediately after
            stop.stop_type = StopType.INGRESS
            stop.display_name = (
                f"{line_name}_{struct}_IN" if line_name else f"{struct}_IN"
            )
            egress = Stop(
                original_name=(
                    f"{line_name}_{struct}_OUT" if line_name
                    else f"{struct}_OUT"
                ),
                structure_name=struct,
                stop_type=StopType.EGRESS,
                stop_number=stop.stop_number,
                point_layer_id=stop.point_layer_id,
                point_feature_id=stop.point_feature_id,
            )
            route.stops.insert(stop_idx + 1, egress)

        elif stop.stop_type in (StopType.INGRESS, StopType.EGRESS):
            # Find the paired stop and remove it, keep this one as Passthrough
            pair_type = (
                StopType.EGRESS if stop.stop_type == StopType.INGRESS
                else StopType.INGRESS
            )
            pair_idx = None
            for j, s in enumerate(route.stops):
                if (j != stop_idx
                        and s.stop_number == stop.stop_number
                        and s.structure_name == struct
                        and s.stop_type == pair_type):
                    pair_idx = j
                    break

            if pair_idx is not None:
                route.stops.pop(pair_idx)
                # Adjust stop_idx if the removed stop was before us
                if pair_idx < stop_idx:
                    stop_idx -= 1
                stop = route.stops[stop_idx]

            stop.stop_type = StopType.PASSTHROUGH
            stop.display_name = (
                f"{line_name}_{struct}" if line_name else struct
            )

        route.has_edits = True
        self._refresh_current()

    # ------------------------------------------------------------------
    # Refresh helpers
    # ------------------------------------------------------------------

    def _refresh_current(self):
        """Re-populate stops table and route row for the current selection."""
        if self._closing:
            return
        route = self._selected_route()
        if route is None:
            return

        # Remember stop selection
        stop_idx = self._selected_stop_index()

        # Refresh the route row in the routes table
        route_rows = self._route_table.selectionModel().selectedRows()
        if route_rows:
            r = route_rows[0].row()

            # Stops count
            stops_item = QTableWidgetItem(str(route.active_stop_count))
            stops_item.setTextAlignment(ALIGN_CENTER)
            self._route_table.setItem(r, _R_COL_STOPS, stops_item)

            # Status
            state = self._route_change_state(route)
            status_item = QTableWidgetItem(state)
            color = _STATUS_COLORS.get(state, QColor(128, 128, 128))
            status_item.setForeground(color)
            font = status_item.font()
            font.setBold(True)
            status_item.setFont(font)
            self._route_table.setItem(r, _R_COL_STATUS, status_item)

            # Delta
            delta = self._route_delta(route)
            delta_item = QTableWidgetItem(delta)
            delta_item.setTextAlignment(ALIGN_CENTER)
            if delta:
                delta_item.setForeground(QColor(192, 57, 43))
            self._route_table.setItem(r, _R_COL_DELTA, delta_item)

        # Refresh stops table
        self._populate_stops(route)

        # Restore stop selection
        if 0 <= stop_idx < len(route.stops):
            self._stop_table.selectRow(stop_idx)

        # Update summary banner
        self._update_summary()
        self._update_edit_buttons()

    # ------------------------------------------------------------------
    # Map highlighting
    # ------------------------------------------------------------------

    def _highlight_route(self, route: Route):
        """Highlight the line feature for this route on the map canvas."""
        self._clear_route_highlights()
        if not route.line_layer_id:
            return

        layer = QgsProject.instance().mapLayer(route.line_layer_id)
        if not isinstance(layer, QgsVectorLayer):
            return

        feat = layer.getFeature(route.line_feature_id)
        if not feat.isValid() or feat.geometry().isEmpty():
            return

        h = QgsHighlight(self._canvas, feat, layer)
        h.setColor(_ROUTE_COLOR)
        h.setFillColor(_ROUTE_FILL)
        h.setWidth(4)
        self._route_highlights.append(h)

    def _highlight_stop(self, stop: Stop):
        """Highlight the point feature for this stop on the map canvas."""
        self._clear_stop_highlights()
        if not stop.point_layer_id:
            return

        layer = QgsProject.instance().mapLayer(stop.point_layer_id)
        if not isinstance(layer, QgsVectorLayer):
            return

        feat = layer.getFeature(stop.point_feature_id)
        if not feat.isValid() or feat.geometry().isEmpty():
            return

        h = QgsHighlight(self._canvas, feat, layer)
        h.setColor(_STOP_COLOR)
        h.setFillColor(_STOP_FILL)
        h.setWidth(3)
        h.setMinWidth(8)
        self._stop_highlights.append(h)

    def _clear_route_highlights(self):
        for h in self._route_highlights:
            self._canvas.scene().removeItem(h)
        self._route_highlights.clear()

    def _clear_stop_highlights(self):
        for h in self._stop_highlights:
            self._canvas.scene().removeItem(h)
        self._stop_highlights.clear()

    def _clear_all_highlights(self):
        self._clear_route_highlights()
        self._clear_stop_highlights()

    # ------------------------------------------------------------------
    # Cleanup on close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._closing = True
        self._clear_all_highlights()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def refresh(self):
        """Re-populate both tables (call after external edits)."""
        selected_idx = None
        rows = self._route_table.selectionModel().selectedRows()
        if rows:
            selected_idx = rows[0].row()

        self._populate_routes()

        if selected_idx is not None and selected_idx < len(self._routes):
            self._route_table.selectRow(selected_idx)
        elif self._routes:
            self._route_table.selectRow(0)

        self._update_summary()


def _stop_type_display(st: StopType) -> str:
    """Human-readable stop type label."""
    return {
        StopType.ORIGIN: "Origin",
        StopType.DESTINATION: "Destination",
        StopType.PASSTHROUGH: "Passthrough",
        StopType.INGRESS: "Ingress (_IN)",
        StopType.EGRESS: "Egress (_OUT)",
    }.get(st, str(st.value))
